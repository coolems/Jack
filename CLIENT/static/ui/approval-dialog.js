/**
 * approval-dialog.js - UNIFIED consent dialog renderer (2026-07-18).
 *
 * ONE code path for ALL four consent card kinds. The backend
 * (CLIENT/app/chat_bus/consent_dialog.py, class ConsentDialog) emits a single
 * 'exec_approval_request' frame with a required `kind`; this file renders every
 * card from the kind -> config table below - different inputs, same code:
 *
 *   python    AI wants to run Python code on THIS machine        (code payload)
 *   url       browser tool to a blocked-but-consentable target: local file inside the
 *             working root, localhost or private/reserved IP (LAN / metadata / CGNAT)  (URL payload)
 *   file      sandboxed python_exec touching OUTSIDE working_root (op + path line)
 *   network   sandboxed python_exec connecting OUTSIDE the box    (socket call line)
 *
 * Header = kind icon + SHORT title (easy to read); the long context lives in the
 * description line. Buttons: Allow / "Allow until task done" (EVERY card) / Deny,
 * plus "Explain request" on EVERY card. Explain posts decision 'explain': nothing is
 * allowed; the AI answers in its required two-part format (what + why, then
 * `for continue plan <plan_name> — just say continue`) and re-issues the tool call,
 * which renders a fresh card.
 *
 * Auto-approve: per-kind timers from COOLEMS.runtimeSettings.approvals[kind]
 * ({mode:'manual'|'auto', seconds}) - fully detached per category (Settings > Runtime).
 * Manual = wait forever; auto = the countdown submits 'allow' itself at zero, exactly
 * like a manual click. Legacy frames without `kind` resolve via ApprovalDialog.resolveKind()
 * (no kind + title => url, else python) so old recordings keep rendering.
 */

/** Single source of truth for every card variant - add/expand kinds HERE only. */
const APPROVAL_DIALOG_CONFIG = {
    python: {
        icon: '\u{1F40D}',                    // 🐍
        title: 'Python execute request',
        allowLabel: '&#10003; Allow this run',
        denyLabel: '&times; Do not run',
        runAllLabel: '&#9889; Allow until task done',  // same wording on every card
        statusAllow: '\u2705 Allowed - the code is running now.',
        statusRunAll: '\u26A1 Allow-until-task-done enabled: consent requests will be approved without asking until this task ends.',
        statusDeny: '\u274C Not run - you decided not to execute this code.'
    },
    url: {
        icon: '\u{1F310}',                    // 🌐
        title: 'URL open request',
        allowLabel: '&#10003; Open it',
        denyLabel: '&times; Do not open',
        runAllLabel: '&#9889; Allow until task done',
        statusAllow: '\u2705 Allowed - opening this URL in the browser now.',
        statusRunAll: '\u26A1 Auto-open enabled: consentable URL targets (local files, localhost and private/LAN addresses) will open without asking until this task ends.',
        statusDeny: '\u274C Not opened - you decided not to open this target. The AI was told and will continue differently.'
    },
    file: {
        icon: '\u{1F4C1}',                    // 📁
        title: 'File access request',
        allowLabel: '&#10003; Allow access',
        denyLabel: '&times; Deny access',
        runAllLabel: '&#9889; Allow until task done',
        statusAllow: '\u2705 Access allowed - the script may touch that path for this run.',
        statusRunAll: '\u26A1 Allow-until-task-done enabled: file access requests will be approved without asking until this task ends.',
        statusDeny: '\u274C Access denied - that path stayed blocked and the AI was told.'
    },
    subprocess: {
        icon: '\u{1F5A8}\uFE0F',               // 🔨 (spawning an external program)
        title: 'External program request',
        allowLabel: '&#10003; Allow this spawn',
        denyLabel: '&times; Block spawn',
        runAllLabel: '&#9889; Allow until task done',
        statusAllow: '\u2705 Spawn allowed - the program is running now (outside the sandbox).',
        statusRunAll: '\u26A1 Allow-until-task-done enabled: external-program requests will be approved without asking until this task ends.',
        statusDeny: '\u274C Spawn blocked - the program did not run and the AI was told.'
    },
    network: {
        icon: '\u26A0\uFE0F\u{1F30D}',        // ⚠️🌍 (warning kept for the risky one)
        title: 'Network access request',
        allowLabel: '&#10003; Allow connection',
        denyLabel: '&times; Block connection',
        runAllLabel: '&#9889; Allow until task done',
        statusAllow: '\u2705 Connection allowed - the script may reach that target now.',
        statusRunAll: '\u26A1 Allow-until-task-done enabled: network access requests will be approved without asking until this task ends.',
        statusDeny: '\u274C Connection denied - it stays blocked and the AI was told.'
    },
    desktop: {
        icon: '\u{1F5A5}\uFE0F',              // 🖥️ (this machine's desktop / an app on it)
        title: 'Desktop access request',
        allowLabel: '&#10003; Allow this app',
        denyLabel: '&times; Block this app',
        runAllLabel: '&#9889; Allow until task done',
        statusAllow: '\u2705 Allowed - the AI may read/drive that app now (nothing is launched, activated or closed).',
        statusRunAll: '\u26A1 Allow-until-task-done enabled: desktop access requests will be approved without asking until this task ends.',
        statusDeny: '\u274C Blocked - that app was not read or touched, and the AI was told not to work around it.'
    },
    /**
     * (2026-12 ask_user_question) NOT a permission gate - the payload is a QUESTION plus
     * options, and the answer is free text. Rendered by renderQuestionCard() below, so
     * only its header/status wording lives here. No auto-approve countdown EVER: the
     * backend pins this kind to 'manual' (config.CONSENT_NEVER_AUTO_KINDS) because an
     * auto-answered question would invent an answer the user never gave.
     *
     * Shape: calm green accent, options one line under another (first = the AI's starred
     * recommendation), free-text input last, no extra buttons. Only real answers are
     * option lines; an unanswered card auto-closes only on the Settings > Runtime
     * budget (0 = default = waits forever).
     */
    question: {
        icon: '\u{1F4AC}',                    // 💬 (the AI asking the human)
        title: 'Question from the AI',
        allowLabel: '&#10003; Send answer',
        denyLabel: '&times; Skip - AI decides',
        runAllLabel: '',                      // no extra button on a question
        statusAllow: '\u2705 Answered - the AI has your answer.',
        statusRunAll: '\u26A1 The AI may decide questions of this kind itself for the rest of this task.',
        statusDeny: '\u274C Skipped - the AI was told to state the assumption it is making.'
    }
};

/**
 * Is this option line just a placeholder for the free-text input ("Something else -
 * I'll type it", "Other", "Custom answer")? Never rendered as an option - the card's
 * own input at the end already IS that choice. Two rules, so a REAL option survives:
 *   * it STARTS with a filler phrase ("something else", "other", "none of the above",
 *     "custom", "type your own", ...) - words after a dash or colon are allowed;
 *   * or it is ONLY a filler word ("Other", "Other...", "N/A", "Free text").
 * "Other files in .temp" or "Other - a CSV cleaner" are real answers and stay.
 */
const QUESTION_FILLER_START_RE =
    /^\s*(?:[-*\u00b7\u2022]\s*)?(?:something else|some other(?:\s+(?:thing|option|answer))?|anything else|any other(?:\s+(?:one|option|answer))?|none of the (?:above|two|three|list)|custom(?:\s+answer)?|your own(?:\s+answer)?|type (?:your own|my own|it|something|your answer)|write (?:your own|my own|it)|fill in|free text)\b\s*(?:[-:!.,]?\s*(?:i(?:'ll| w+ill)?(?: will)? (?:type|write|say|enter)[\s\S]*|you(?:r)? (?:type|own)[\s\S]*|specify|specify (?:your own|it|something)|else)?)?\s*$/i;
const QUESTION_FILLER_ONLY_RE =
    /^\s*(?:[-*\u00b7\u2022]\s*)?(?:other(?:\s+thing)?|n\/a|none|custom|free text|other\.\.\.?|-+)[\s.!]*$/i;
/** "Other - specify" / "Other - I'll type it": the line offers nothing but the input. */
const QUESTION_FILLER_OTHER_RE =
    /^\s*(?:[-*\u00b7\u2022]\s*)?other\s*[-:!.,]\s*(?:specify|specify (?:your own|it|something)|i(?:'ll| w+ill)?(?: will)? (?:type|write|say|enter)[\s\S]*|you(?:r)? (?:type|own)[\s\S]*|else)[\s.!]*$/i;
/** "I'll type it" / "I will write my own" - the whole line is just an intent to type. */
const QUESTION_FILLER_ME_RE =
    /^\s*(?:[-*\u00b7\u2022]\s*)?i(?:'ll| w+ill)?(?: will)? (?:type|write|say|enter) (?:my own|my answer|it|something)[\s.!]*$/i;

function _isPlaceholderOption(opt) {
    const s = String(opt || '');
    return QUESTION_FILLER_START_RE.test(s) || QUESTION_FILLER_ONLY_RE.test(s)
        || QUESTION_FILLER_OTHER_RE.test(s) || QUESTION_FILLER_ME_RE.test(s);
}

const ApprovalDialog = {
    /** Normalize a frame's kind marker (legacy-safe, mirrors backend resolve_kind()). */
    resolveKind(data) {
        const k = String((data && data.kind) || '').trim().toLowerCase();
        if (APPROVAL_DIALOG_CONFIG[k]) return k;
        if (!k && typeof data.title === 'string' && data.title.length > 0) return 'url'; // legacy URL-consent frame
        return 'python';
    },

    /** Render one consent card from the shared config table. Entry point for websocket.js.
     *
     * `mount` (2026-11-08 loop-history UI): when given, the card renders INSIDE that
     * element - the live strip of the current cycle block - instead of the chat stream.
     * The card is transient there: once answered, websocket.js receives `cycle_consent`
     * and CycleBlock.settleCard() removes it, leaving a decision row in the block body.
     * Dedupe is scoped to the mount, so two workspaces can never collide on a request_id.
     */
    appendCard(data, mount) {
        const container = mount || document.getElementById('chatContainer');
        if (!container) return;
        const welcome = document.getElementById('welcome');
        if (welcome && welcome.style.display !== 'none') welcome.style.display = 'none';

        // Replay/duplicate safety: never render the same request twice (in THIS mount).
        if (data.request_id && container.querySelector('.exec-approval-card[data-request-id="' + data.request_id + '"]')) {
            return;
        }

        const kind = this.resolveKind(data);
        const cfg = APPROVAL_DIALOG_CONFIG[kind];

        // Header: icon + SHORT title. Legacy frames (no description) keep a context line.
        const headerHtml = '<span class="exec-approval-icon">' + cfg.icon + '</span> ' + escapeHtml(cfg.title);
        const descText = (typeof data.description === 'string' && data.description.length > 0)
            ? data.description
            : '';
        const descHtml = descText
            ? '<div class="exec-approval-desc">' + escapeHtml(descText) + '</div>'
            : '';

        const card = document.createElement('div');
        card.className = 'message assistant';
        // A question card is not a consent card: its own class gets the green treatment.
        if (kind === 'question') card.classList.add('question-card-wrap');

        const inner = document.createElement('div');
        inner.className = 'avatar';
        inner.textContent = 'AI';

        const body = document.createElement('div');
        body.className = 'message-content exec-approval-card';
        if (data.request_id) body.setAttribute('data-request-id', data.request_id);
        body.dataset.kind = kind;
        if (kind === 'question') body.classList.add('exec-question-card');

        // (2026-12 ask_user_question) A QUESTION card is NOT a permission gate: the payload
        // is {question, options, context} JSON and the user answers with an option or free
        // text. No auto-approve countdown ever runs for this kind - the backend pins it to
        // 'manual' (config.CONSENT_NEVER_AUTO_KINDS), because auto-answering would invent an
        // answer the user never gave.
        if (kind === 'question') {
            body.innerHTML = this._questionCardBody(data, headerHtml, descHtml);
            card.appendChild(inner);
            card.appendChild(body);
            container.appendChild(card);
            this._wireQuestionCard(card, data);
            this._startQuestionDismissTimer(card, data);
            if (COOLEMS.autoScrollEnabled && !COOLEMS.userScrolledUp) scrollToBottom();
            return;
        }

        const codeHtml = escapeHtml(String(data.code || ''));
        body.innerHTML =
            '<div class="exec-approval-header">' + headerHtml + '</div>' +
            descHtml +
            '<pre class="exec-approval-code"><code>' + codeHtml + '</code></pre>' +
            '<span class="exec-approval-status pending">Waiting for your decision (no time limit - answer whenever you are ready)</span>' +
            '<div class="exec-approval-actions">' +
                '<button type="button" class="exec-btn exec-btn-allow" data-decision="allow">' + cfg.allowLabel + '</button>' +
                (cfg.runAllLabel ? '<button type="button" class="exec-btn exec-btn-runall" data-decision="allow_category_until_done">' + cfg.runAllLabel + '</button>' : '') +
                '<button type="button" class="exec-btn exec-btn-deny" data-decision="deny">' + cfg.denyLabel + '</button>' +
                // (2026-07-18) Explain request: on EVERY card. Nothing is allowed - the AI
                // explains what this request is + why it matters, then re-asks with a fresh card.
                '<button type="button" class="exec-btn exec-btn-explain" data-decision="explain">&#128172; Explain request</button>' +
            '</div>';

        body.querySelectorAll('.exec-btn').forEach(btn => {
            btn.addEventListener('click', () => this.submitDecision(data, btn));
        });

        card.appendChild(inner);
        card.appendChild(body);
        container.appendChild(card);

        // Per-kind auto-approve countdown (manual mode = no timer at all).
        void this.startAutoApproveCountdown(card, data).catch(e => console.error('[APPROVAL-DIALOG] auto-approve countdown failed:', e));

        if (COOLEMS.autoScrollEnabled && !COOLEMS.userScrolledUp) scrollToBottom();
    },

    /** Body of an ask_user_question card:
     *    header -> context -> question -> options one line under another (first starred
     *    as the AI's recommendation) -> free-text input at the END -> one status line.
     *    No other buttons. Only real answers become option lines, and the star always
     *    lands on the first real one. Payload is JSON from the tool. */
    _questionCardBody(data, headerHtml, descHtml) {
        let q = { question: String(data.code || ''), options: [], context: '' };
        try {
            const parsed = JSON.parse(String(data.code || ''));
            if (parsed && typeof parsed === 'object') {
                q = {
                    question: String(parsed.question || ''),
                    options: Array.isArray(parsed.options) ? parsed.options.map(String) : [],
                    context: String(parsed.context || ''),
                };
            }
        } catch (e) { /* payload was plain text - use it as the question */ }

        const contextHtml = q.context
            ? '<div class="exec-approval-desc">' + escapeHtml(q.context) + '</div>'
            : (descHtml || '');

        // One line per option; the first real one is the AI's recommendation (starred,
        // tinted, labelled). Placeholder lines are dropped - clicking one would send
        // text the user never chose.
        const realOptions = q.options.filter(function (opt) { return !_isPlaceholderOption(opt); });
        const optionHtml = realOptions.map(function (opt, i) {
            const rec = i === 0;
            return '<button type="button" class="exec-question-option' + (rec ? ' recommended' : '') + '" '
                 + 'data-answer="' + escapeHtml(opt) + '">'
                 + '<span class="exec-question-option-mark">' + (rec ? '\u2605' : '\u00b7') + '</span>'
                 + '<span class="exec-question-option-text">' + escapeHtml(opt) + '</span>'
                 + (rec ? '<span class="exec-question-option-tag">recommended</span>' : '')
                 + '</button>';
        }).join('');
        const listHtml = realOptions.length
            ? '<div class="exec-question-options" role="list">' + optionHtml + '</div>'
            : '';

        // The free-text line is always there. An empty submit sends the starred option.
        const placeholder = realOptions.length
            ? 'Type your own answer\u2026 (empty = \u2605 ' + escapeHtml(realOptions[0]) + ')'
            : 'Type your answer\u2026';

        return '<div class="exec-approval-header">' + headerHtml + '</div>' +
            contextHtml +
            '<div class="exec-approval-question">' + escapeHtml(q.question) + '</div>' +
            listHtml +
            '<div class="exec-approval-answer-row">' +
                '<input type="text" class="exec-approval-answer-input" placeholder="' + placeholder + '" />' +
            '</div>' +
            '<span class="exec-approval-status pending">Waiting for your answer - pick a line or type your own.</span>';
    },

    /** Wire one question card: option lines + the free-text input post to /api/question.
     *  An EMPTY free-text answer submits the starred option, so Enter on an empty field
     *  is a real decision. With no options, an empty field does nothing. A blur alone is
     *  NOT a decision - only Enter (or the button) submits. */
    _wireQuestionCard(card, data) {
        const input = card.querySelector('.exec-approval-answer-input');
        const optionBtns = Array.from(card.querySelectorAll('.exec-question-option'));
        optionBtns.forEach(btn => {
            btn.addEventListener('click', () => this.submitAnswer(data, btn.dataset.answer, btn));
        });
        const recommended = optionBtns.length ? optionBtns[0].dataset.answer : '';
        const submitTyped = () => {
            const text = (input && input.value) ? input.value.trim() : '';
            if (!text) {
                if (recommended) { this.submitAnswer(data, recommended, optionBtns[0]); return; }
                if (input) input.focus();
                return;
            }
            this.submitAnswer(data, text, input || card);
        };
        if (input) {
            input.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') { e.preventDefault(); submitTyped(); }
            });
            input.focus();
        }
    },

    /**
     * Local countdown for one question card, with the budget from Settings > Runtime
     * (0 = no timer). The backend expires the ask with the SAME number and sends
     * `cycle_question`, which removes the card; this timer is the UI half - on expiry
     * with no click the card is dismissed as UNANSWERED and the AI is told so.
     */
    _startQuestionDismissTimer(card, data) {
        const statusEl = card.querySelector('.exec-approval-status');
        // The frame carries the budget live when the card was sent; the live setting
        // wins for a re-rendered card (cycle-shell passes it too).
        let secs = parseInt((data && data.dismiss_sec), 10);
        if (!Number.isFinite(secs)) {
            const rt = (typeof COOLEMS !== 'undefined' && COOLEMS.runtimeSettings)
                ? COOLEMS.runtimeSettings : null;
            secs = rt ? parseInt(rt.questionCardSeconds, 10) : 0;
        }
        if (!Number.isFinite(secs) || secs <= 0) {
            if (statusEl) statusEl.textContent =
                '\u270D Waiting for your answer - pick a line or type your own (no time limit).';
            return;   // 0 = no timer
        }
        let remaining = secs;
        const tick = setInterval(() => {
            if (card.dataset.answered === '1' || !card.isConnected) { clearInterval(tick); return; }
            remaining -= 1;
            if (remaining > 0) {
                if (statusEl) statusEl.textContent =
                    '\u23F3 Waiting for your answer - this card closes by itself in ' + remaining + 's.';
                return;
            }
            clearInterval(tick);
            this._dismissUnanswered(card, data);
        }, 1000);
        card._autoTimer = tick;
    },

    /** Dismiss one question card WITHOUT an answer: tell the backend it went unanswered
     *  (the waiting tool call returns 'unanswered' - never an invented answer), then
     *  remove the card. A card the backend already settled is simply removed. */
    _dismissUnanswered(card, data) {
        if (!card || card.dataset.answered === '1') return;
        card.dataset.answered = '1';
        card.dataset.decision = 'unanswered';
        card.querySelectorAll('.exec-btn, .exec-question-option').forEach(b => { b.disabled = true; });
        const statusEl = card.querySelector('.exec-approval-status');
        if (statusEl) {
            statusEl.className = 'exec-approval-status done-deny';
            statusEl.textContent = '\u23F1 No answer was given - the AI was told the question is unanswered.';
        }
        const convId = data.conv_id || COOLEMS.currentConversation;
        fetch('/api/question/' + encodeURIComponent(convId) + '/' + encodeURIComponent(data.request_id), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ answer: 'unanswered' })
        })
            .catch(() => {})
            .finally(() => this.dismissCard(card, 'unanswered'));
    },

    /** Post a free-text or option ANSWER for one question card. */
    submitAnswer(data, answer, clickedEl) {
        const card = clickedEl.closest ? (clickedEl.closest('.exec-approval-card') || clickedEl) : clickedEl;
        if (!card || card.dataset.answered === '1') return;
        card.querySelectorAll('.exec-btn, .exec-question-option').forEach(b => { b.disabled = true; });
        const statusEl = card.querySelector('.exec-approval-status');
        const convId = data.conv_id || COOLEMS.currentConversation;
        fetch('/api/question/' + encodeURIComponent(convId) + '/' + encodeURIComponent(data.request_id), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ answer: answer })
        })
            .then(response => response.json().catch(() => ({})).then(d => ({ ok: response.ok, d })))
            .then(({ ok, d }) => {
                card.dataset.answered = '1';
                if (!ok) throw new Error((d && d.message) || ('HTTP ' + (d && d.detail ? JSON.stringify(d.detail) : '')));
                this.dismissCard(card, 'answered');
            })
            .catch(err => {
                card.dataset.answered = '1';
                if (statusEl) { statusEl.className = 'exec-approval-status done-error'; statusEl.textContent = '\u26A0 ' + err.message; }
            });
    },

    /** Generic per-kind auto-approve timer - reads THAT kind's saved mode/seconds. */
    async startAutoApproveCountdown(card, data) {
        // (2026-12) A question card is never auto-answered - an invented answer is worse
        // than a waiting card. Belt and braces with the backend's manual pin.
        if (this.resolveKind(data) === 'question') return;
        // Saved settings first (one /api/settings sync per page session; instant afterwards).
        if (typeof ensureRuntimeSettingsLoaded === 'function') {
            try { await ensureRuntimeSettingsLoaded(); } catch (e) { /* keep current values */ }
        }
        const rt = (typeof COOLEMS !== 'undefined' && COOLEMS.runtimeSettings) ? COOLEMS.runtimeSettings : null;
        const kind = this.resolveKind(data);
        // approvals[kind] is the detached per-category setting (2026-07-18). Fall back to
        // the legacy top-level mode/seconds for python so an unsynced client behaves as before.
        let mode = 'manual', seconds = 5;
        if (rt && rt.approvals && rt.approvals[kind]) {
            mode = rt.approvals[kind].mode;
            seconds = rt.approvals[kind].seconds;
        } else if (rt) {
            mode = kind === 'python' ? rt.mode : 'manual';   // legacy shape: python only
            seconds = rt.seconds;
        }
        if (mode !== 'auto') return;                          // manual: wait forever

        let s = parseInt(seconds, 10);
        if (!Number.isFinite(s)) s = 5;
        s = Math.min(3600, Math.max(1, s));

        const statusEl = card.querySelector('.exec-approval-status');
        if (statusEl) {
            statusEl.className = 'exec-approval-status pending';
            statusEl.textContent = '\u23F3 Auto-approve in ' + s + 's - click a button to decide sooner.';
        }

        let remaining = s;
        const tickTimer = setInterval(() => {
            // The card is gone (answered, auto-approved, or the block was cleared) - stop touching it.
            if (card.dataset.answered === '1' || !card.isConnected) { clearInterval(tickTimer); return; }
            const allowBtn = card.querySelector('.exec-btn[data-decision="allow"]');
            // Any button click disables all buttons immediately - once that happens the user
            // has decided and the countdown must stop touching the dialog (no status overwrite).
            if (!allowBtn || allowBtn.disabled) { clearInterval(tickTimer); return; }
            remaining -= 1;
            if (remaining > 0) {
                if (statusEl) statusEl.textContent = '\u23F3 Auto-approve in ' + remaining + 's - click a button to decide sooner.';
            } else {
                clearInterval(tickTimer);
                this.submitDecision(data, allowBtn);   // identical to the user clicking Allow
            }
        }, 1000);
        // A card removed some other way (backend decision frame, turn settle, workspace clear)
        // must not leave a ticking timer behind - it would fire submitDecision on a dead node.
        card._autoTimer = tickTimer;
    },

    /**
     * Remove one answered card from the DOM (user rule 2026-11-08): a consent dialog must
     * DISAPPEAR after ANY button click AND after the auto-approve timer fires. The card is
     * transient by design - the decision survives as a row of the turn block (cycle_consent),
     * so nothing is lost when the card goes away.
     *
     * Two shapes are handled, because a card lives either inside a block's live strip or, when
     * no block owned the frame, as a plain chat bubble:
     *   .cycle-card-mount > .message > .exec-approval-card   -> the whole mount goes
     *   .message > .exec-approval-card                       -> the whole bubble goes (the
     *     old removal only looked for mounts, so a chat-level card lingered forever)
     */
    dismissCard(card, decision) {
        if (!card) return;
        card.dataset.answered = '1';
        card.dataset.decision = decision || 'answered';
        card.querySelectorAll('.exec-btn, .exec-question-option').forEach(b => { b.disabled = true; });
        const mount = card.closest('.cycle-card-mount');
        const target = mount || card.closest('.message') || card;
        // The countdown is owned by the message WRAPPER (startAutoApproveCountdown receives it),
        // so clear it on every element involved - wrapper, card and mount.
        for (const el of [target, card, card.closest('.message')]) {
            if (el && el._autoTimer) { clearInterval(el._autoTimer); el._autoTimer = null; }
        }
        target.remove();
    },

    /** Post one decision (allow / deny / allow_category_until_done / explain) for a card. */
    submitDecision(data, clickedBtn) {
        const card = clickedBtn.closest('.exec-approval-card');
        if (!card || card.dataset.answered === '1') return;
        card.querySelectorAll('.exec-btn').forEach(b => { b.disabled = true; });
        const statusEl = card.querySelector('.exec-approval-status');
        const decision = clickedBtn.dataset.decision;
        const convId = data.conv_id || COOLEMS.currentConversation;
        fetch('/api/exec-approval/' + encodeURIComponent(convId) + '/' + encodeURIComponent(data.request_id), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ decision: decision })
        })
            .then(response => response.json().catch(() => ({})).then(d => ({ ok: response.ok, d })))
            .then(({ ok, d }) => {
                card.dataset.answered = '1';
                if (!ok) throw new Error((d && d.message) || ('HTTP ' + (d && d.detail ? JSON.stringify(d.detail) : '')));
                // The decision reached the backend - the dialog is done, so it disappears.
                // (A failed POST keeps the card on screen with the error, so the user can retry.)
                this.dismissCard(card, decision);
            })
            .catch(err => {
                card.dataset.answered = '1';
                if (statusEl) { statusEl.className = 'exec-approval-status done-error'; statusEl.textContent = '\u26A0 ' + err.message; }
            });
    }
};
