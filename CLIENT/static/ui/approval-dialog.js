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
    }
};

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

        const codeHtml = escapeHtml(String(data.code || ''));
        const card = document.createElement('div');
        card.className = 'message assistant';

        const inner = document.createElement('div');
        inner.className = 'avatar';
        inner.textContent = 'AI';

        const body = document.createElement('div');
        body.className = 'message-content exec-approval-card';
        if (data.request_id) body.setAttribute('data-request-id', data.request_id);
        body.dataset.kind = kind;
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

    /** Generic per-kind auto-approve timer - reads THAT kind's saved mode/seconds. */
    async startAutoApproveCountdown(card, data) {
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
            // (2026-11-15) A manual click that FAILED to deliver re-enables the buttons so the
            // user can retry. The countdown must never fire 'allow' over that intent.
            if (card._userDecided) { clearInterval(tickTimer); return; }
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
        // A late ack for a card that is already gone must not touch it again.
        if (card.dataset.requestId) delete ApprovalDialog._acks[card.dataset.requestId];
        card.querySelectorAll('.exec-btn').forEach(b => { b.disabled = true; });
        const mount = card.closest('.cycle-card-mount');
        const target = mount || card.closest('.message') || card;
        // The countdown is owned by the message WRAPPER (startAutoApproveCountdown receives it),
        // so clear it on every element involved - wrapper, card and mount.
        for (const el of [target, card, card.closest('.message')]) {
            if (el && el._autoTimer) { clearInterval(el._autoTimer); el._autoTimer = null; }
        }
        target.remove();
    },

    /**
     * Deliver one decision (allow / deny / allow_category_until_done / explain) for a card.
     *
     * (2026-11-15 consent answer channel) PRIMARY transport = the chat's OWN WebSocket frame
     * 'exec_approval_decision' — the same socket that delivered this card, so the answer
     * survives a page reload, a workspace switch and a background workspace. The HTTP POST
     * is kept as a fallback for when that socket is not open. The old design had ONLY the
     * POST: a rejected fetch ("⚠ Failed to fetch") lost the click, and the card was already
     * marked answered with its buttons disabled — the user was left with a dead card and the
     * turn stayed parked on the pending future.
     *
     * The card is marked answered ONLY on a confirmed delivery. On failure the buttons are
     * re-enabled and the status line asks for a retry.
     */
    submitDecision(data, clickedBtn) {
        const card = clickedBtn.closest('.exec-approval-card');
        if (!card || card.dataset.answered === '1') return;
        const decision = clickedBtn.dataset.decision;
        const statusEl = card.querySelector('.exec-approval-status');
        const convId = data.conv_id || COOLEMS.currentConversation;
        card._userDecided = true;              // stops the auto-approve countdown for good
        card.querySelectorAll('.exec-btn').forEach(b => { b.disabled = true; });
        if (statusEl) { statusEl.className = 'exec-approval-status pending'; statusEl.textContent = '\u23F3 Sending your decision...'; }

        const retry = (msg) => {
            delete card.dataset.answered;       // the user may click again
            // _userDecided stays true: once the user touched a button, the auto-approve
            // countdown must never fire 'allow' over their intent - even after a failed send.
            card.querySelectorAll('.exec-btn').forEach(b => { b.disabled = false; });
            if (statusEl) {
                statusEl.className = 'exec-approval-status done-error';
                statusEl.textContent = '\u26A0 ' + msg + ' - click a button again.';
            }
        };

        const viaHttp = () => fetch('/api/exec-approval/' + encodeURIComponent(convId) + '/' + encodeURIComponent(data.request_id), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ decision: decision })
        })
            .then(r => r.json().catch(() => ({})).then(d => ({ ok: r.ok, d })))
            .then(({ ok, d }) => {
                if (!ok) {
                    const msg = (d && d.message) || ('HTTP ' + (d && d.detail ? JSON.stringify(d.detail) : ''));
                    // "already answered or expired" = this card's future is gone: withdraw it
                    // instead of inviting a click that can never succeed. Anything else (400,
                    // a 5xx) is a real failure the user can retry.
                    if (/already answered|expired|No pending/i.test(msg)) this.cancelCard(data.request_id);
                    else retry(msg);
                    return;
                }
                card.dataset.answered = '1';    // the backend answered - the card is done
                this.dismissCard(card, decision);
            })
            .catch(err => {
                // (2026-11-15) the old build marked the card answered here, so a rejected
                // fetch ("Failed to fetch") left a dead card and a lost click. Retry instead.
                retry(err && err.message ? err.message : 'Could not deliver your decision');
            });

        const ws = this._socketFor(convId);
        if (!ws) return viaHttp();             // no live socket for this chat: HTTP fallback

        let settled = false;
        const ackTimer = setTimeout(() => {
            if (settled) return;
            settled = true;
            void viaHttp();                    // no ack: the socket is gone or too old - fall back
        }, 4000);

        // The ack is correlated by request_id, so two cards on screen never cross-settle.
        ApprovalDialog._acks[data.request_id] = (ok, message) => {
            if (settled) return;
            settled = true;
            clearTimeout(ackTimer);
            if (ok) {
                card.dataset.answered = '1';
                this.dismissCard(card, decision);
            } else if (/already answered|expired|No pending/i.test(message || '')) {
                this.cancelCard(data.request_id);   // its future is gone - withdraw the card
            } else {
                retry(message || 'The backend refused this decision');
            }
        };

        try {
            ws.send(JSON.stringify({ type: 'exec_approval_decision', request_id: data.request_id, decision: decision }));
        } catch (e) {
            settled = true;
            clearTimeout(ackTimer);
            delete ApprovalDialog._acks[data.request_id];
            return viaHttp();
        }
        return undefined;
    },

    /** The pooled socket of a conversation, or null when it is not open. */
    _socketFor(convId) {
        const entry = (typeof ChatSocketPool !== 'undefined' && convId)
            ? ChatSocketPool.sockets[convId] : null;
        const ws = entry ? entry.ws : (typeof COOLEMS !== 'undefined' ? COOLEMS.ws : null);
        return (ws && ws.readyState === WebSocket.OPEN) ? ws : null;
    },

    /** One card's ack arrived (called by websocket.js). Settles the card or asks for a retry. */
    handleAck(data) {
        const rid = data && data.request_id;
        if (!rid) return false;
        const cb = ApprovalDialog._acks[rid];
        if (!cb) return false;
        delete ApprovalDialog._acks[rid];
        cb(!!data.ok, data.message || '');
        return true;
    },

    /**
     * The backend withdrew a card (turn ended / stopped while it was unanswered).
     * A card whose future is gone can never be answered, so it must leave the screen and
     * settle as 'unanswered' in its turn block instead of inviting a click that does nothing.
     */
    cancelCard(requestId) {
        if (!requestId) return;
        delete ApprovalDialog._acks[requestId];
        const sel = '.exec-approval-card[data-request-id="' + (typeof CSS !== 'undefined' && CSS.escape ? CSS.escape(requestId) : requestId) + '"]';
        for (const card of Array.from(document.querySelectorAll(sel))) {
            card.dataset.answered = '1';
            card.dataset.decision = 'unanswered';
            card.querySelectorAll('.exec-btn').forEach(b => { b.disabled = true; });
            const mount = card.closest('.cycle-card-mount');
            const target = mount || card.closest('.message') || card;
            for (const el of [target, card, card.closest('.message')]) {
                if (el && el._autoTimer) { clearInterval(el._autoTimer); el._autoTimer = null; }
            }
            const statusEl = card.querySelector('.exec-approval-status');
            if (statusEl) {
                statusEl.className = 'exec-approval-status done-error';
                statusEl.textContent = '\u26A0 This request is closed - the turn ended before you answered.';
            }
            target.remove();
        }
        for (const mount of Array.from(document.querySelectorAll('.cycle-card-mount'))) {
            if (mount.dataset.requestId === requestId && !mount.querySelector('.exec-approval-card')) mount.remove();
        }
    }
};

/** request_id -> ack callback, set while a decision waits for its 'exec_approval_ack'. */
ApprovalDialog._acks = {};
