/**
 * approval-dialog.js - UNIFIED consent dialog renderer (2026-07-18).
 *
 * ONE code path for ALL four consent card kinds. The backend
 * (CLIENT/app/chat_bus/consent_dialog.py, class ConsentDialog) emits a single
 * 'exec_approval_request' frame with a required `kind`; this file renders every
 * card from the kind -> config table below - different inputs, same code:
 *
 *   python    AI wants to run Python code on THIS machine        (code payload)
 *   url       browser tool to a local target (SSRF-consentable)  (URL payload)
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
        statusRunAll: '\u26A1 Auto-open enabled: local file/localhost targets will open without asking until this task ends.',
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
    network: {
        icon: '\u26A0\uFE0F\u{1F30D}',        // ⚠️🌍 (warning kept for the risky one)
        title: 'Network access request',
        allowLabel: '&#10003; Allow connection',
        denyLabel: '&times; Block connection',
        runAllLabel: '&#9889; Allow until task done',
        statusAllow: '\u2705 Connection allowed - the script may reach that target now.',
        statusRunAll: '\u26A1 Allow-until-task-done enabled: network access requests will be approved without asking until this task ends.',
        statusDeny: '\u274C Connection denied - it stays blocked and the AI was told.'
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

    /** Render one consent card from the shared config table. Entry point for websocket.js. */
    appendCard(data) {
        const container = document.getElementById('chatContainer');
        if (!container) return;
        const welcome = document.getElementById('welcome');
        if (welcome && welcome.style.display !== 'none') welcome.style.display = 'none';

        // Replay/duplicate safety: never render the same request twice.
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
            if (card.dataset.answered === '1') { clearInterval(tickTimer); return; }
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
    },

    /** Post one decision (allow / deny / allow_category_until_done / explain) for a card. */
    submitDecision(data, clickedBtn) {
        const card = clickedBtn.closest('.exec-approval-card');
        if (!card || card.dataset.answered === '1') return;
        card.querySelectorAll('.exec-btn').forEach(b => { b.disabled = true; });
        const statusEl = card.querySelector('.exec-approval-status');
        const decision = clickedBtn.dataset.decision;
        const kind = this.resolveKind(data);
        const cfg = APPROVAL_DIALOG_CONFIG[kind];

        if (statusEl) { statusEl.className = 'exec-approval-status waiting'; statusEl.textContent = 'Sending decision...'; }

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
                let msg;
                if (decision === 'allow') msg = cfg.statusAllow;
                else if (decision === 'allow_category_until_done') msg = cfg.statusRunAll || '\u26A1 Allow-until-task-done enabled: consent requests will be approved without asking until this task ends.';
                else if (decision === 'explain') msg = '\uD83D\uDCAC Explaining to the AI - it will answer, then ask again with a fresh card.';
                else msg = cfg.statusDeny;
                if (statusEl) { statusEl.className = 'exec-approval-status done-' + decision; statusEl.textContent = msg; }
            })
            .catch(err => {
                card.dataset.answered = '1';
                if (statusEl) { statusEl.className = 'exec-approval-status done-error'; statusEl.textContent = '\u26A0 ' + err.message; }
            });
    }
};
