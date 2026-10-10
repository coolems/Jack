/**
 * chat-socket-pool.js - Per-chat WebSocket pool (2026-09-08 multi-chat, Phase 4).
 *
 * THE core UI piece of "keep every chat connected": instead of ONE global COOLEMS.ws that
 * was closed and reopened on every chat switch (which killed the previous chat's session),
 * each conversation owns its OWN WebSocket in a small map:
 *
 *     convId -> { ws, reconnectAttempts }
 *
 *  - Switching chats NEVER closes another chat's socket — its generation keeps streaming.
 *  - LRU cap: at most ChatSocketPool.maxOpen sockets stay open; the least-recently-used
 *    IDLE one is closed when a new chat opens (its frames keep buffering server-side).
 *    A chat that is generating/queued is NEVER evicted — closing it would kill the session.
 *  - Per-chat reconnect with backoff, scoped to that conversation only.
 *  - Background frames: while you are viewing chat B, frames arriving on chat A's socket
 *    are NOT rendered (they belong to A's view) — they only update A's status dot, so the
 *    sidebar shows what is happening in every chat at once.
 *  - Status dots + aggregate pill: updateChatStatus(convId, status) drives the sidebar
 *    dot (generating / queued / idle); the connection pill gains "N gen · M queued".
 *
 * COOLEMS.ws stays a LIVE ALIAS for the current conversation's socket so legacy call sites
 * (message.js send path, forceReconnect, beforeunload) keep working unchanged.
 */

// ===== Pool state =====
const ChatSocketPool = {
    sockets: {},          // convId -> { ws, reconnectAttempts }
    statuses: {},         // convId -> 'idle' | 'queued' | 'generating' (drives dots + eviction safety)
    order: [],            // LRU order: least recently used at index 0
    maxOpen: 6,           // CLIENT_MAX_OPEN_CHAT_WS default; refined from config when available

    // ===== Sidebar attention markers (2026-10-09) ==================================
    // Orthogonal to session status: a chat can be 'generating' AND waiting on a dialog.
    //   'dialog' - a background workspace has a pending decision card (python_exec approval,
    //              URL consent): BLINKS until answered or the user visits that workspace.
    //   'done'   - a background workspace just finished its task: green flash for ~30 s.
    attention: {},        // convId -> 'dialog' | 'done'
    _attentionTimers: {}, // convId -> setTimeout handle for the temporary 'done' flash

    /** Open (or reuse) the socket for one conversation. NEVER closes other chats' sockets. */
    ensure(convId) {
        if (!convId) return null;

        const existing = this.sockets[convId];
        if (existing && (existing.ws.readyState === WebSocket.OPEN ||
                         existing.ws.readyState === WebSocket.CONNECTING)) {
            this._touch(convId);
            // Legacy alias: keep COOLEMS.ws pointing at the CURRENT chat's socket even when
            // the pooled socket is reused (a switch-back must not leave it on another chat).
            if (COOLEMS.currentConversation === convId) COOLEMS.ws = existing.ws;
            return existing.ws;
        }

        // LRU eviction: only sockets that are safe to close (idle, not the current chat).
        while (Object.keys(this.sockets).length >= this.maxOpen) {
            const victim = this._findEvictable();
            if (!victim) break;
            log(`[ChatSocketPool] LRU closing idle chat ${victim} (cap=${this.maxOpen})`);
            this.close(victim, true);
        }

        return this._create(convId);
    },

    /** Close one conversation's socket. *silent* = expected lifecycle (delete/LRU). */
    close(convId, silent) {
        const entry = this.sockets[convId];
        if (!entry) return;
        delete this.sockets[convId];
        this.order = this.order.filter(id => id !== convId);

        // Null every handler BEFORE closing: _onClose() also guards on sockets[convId],
        // so a late close event can never trigger a reconnect for a deliberate close.
        try {
            entry.ws.onopen = null;
            entry.ws.onmessage = null;
            entry.ws.onerror = null;
            entry.ws.onclose = null;
        } catch (e) { /* already gone */ }

        try { entry.ws.close(); } catch (e) { logError('[ChatSocketPool] close error:', e); }

        if (COOLEMS.currentConversation === convId && !this.sockets[convId]) {
            COOLEMS.ws = null;   // keep the legacy alias honest
        }
        this._updatePill();
    },

    /** True when this conversation currently has a live (or connecting) socket. */
    isOpen(convId) {
        const e = this.sockets[convId];
        return !!(e && (e.ws.readyState === WebSocket.OPEN || e.ws.readyState === WebSocket.CONNECTING));
    },

    _touch(convId) {
        this.order = this.order.filter(id => id !== convId);
        this.order.push(convId);   // most recently used at the end
    },

    /** Oldest socket that is safe to evict: not current, and NOT generating/queued. */
    _findEvictable() {
        for (const id of this.order) {
            if (!this.sockets[id]) continue;
            if (id === COOLEMS.currentConversation) continue;
            const st = this.statuses[id];
            if (st === 'generating' || st === 'queued') continue;   // never kill a live session
            return id;
        }
        return null;
    },

    _create(convId) {
        const wsUrl = getWebSocketUrl(`/ws/chat/${convId}`);
        log(`[ChatSocketPool] Opening socket for chat ${convId} (${Object.keys(this.sockets).length + 1} open)`);

        let ws;
        try {
            ws = new WebSocket(wsUrl);
        } catch (e) {
            logError('[ChatSocketPool] WS create failed:', e);
            updateConnectionStatus('error', 'Error');
            return null;
        }

        const entry = { ws, reconnectAttempts: 0, reconnecting: false };
        this.sockets[convId] = entry;
        this._touch(convId);

        // Keep the legacy global alias pointing at the CURRENT chat's socket.
        if (COOLEMS.currentConversation === convId) COOLEMS.ws = ws;

        ws.onopen = () => {
            entry.reconnectAttempts = 0;
            entry.reconnecting = false;
            updateConnectionStatus('connected', 'Connected');
            this._updatePill();
        };

        ws.onmessage = (e) => {
            let msg;
            try { msg = JSON.parse(e.data); }
            catch (err) { logError('[ChatSocketPool] Parse error:', err); return; }

            // Track status for EVERY frame of this chat (drives the sidebar dot + attention).
            this._trackStatus(convId, msg);

            // Suspended convs are mid-switch: the outgoing workspace is still "current" while its
            // container is being rebuilt for the incoming one - queue its frames too, or they would
            // render into the wrong DOM (2026-10-09 switch-race fix).
            if (convId !== COOLEMS.currentConversation ||
                (typeof WorkspaceViews !== 'undefined' && WorkspaceViews.isSuspended(convId))) {
                // Background chat: its frames must NOT render into the active view — they
                // belong to that chat's own screen. The socket is still open, so the server
                // delivered them LIVE and did not buffer them; queueing here is what keeps an
                // approval card / streaming bubble from being lost while you are away. On a
                // fast-path restore loadConversation() flushes this queue through
                // handleWebSocketMessage(); if no stash exists, the DB reload supersedes it.
                WorkspaceViews.queueFrame(convId, msg);
                return;
            }
            handleWebSocketMessage(msg);
        };

        ws.onerror = () => {
            if (COOLEMS.currentConversation === convId) {
                updateConnectionStatus('error', 'Error');
                showNotification('Connection error', 'error');
            }
        };

        // Unexpected close -> per-chat reconnect with backoff (scoped to THIS chat).
        ws.onclose = (e) => this._onClose(convId, e);

        this._updatePill();
        return ws;
    },

    _onClose(convId, e) {
        const entry = this.sockets[convId];
        if (!entry || !this.sockets[convId]) return;   // deliberate close() removed it first

        log(`[ChatSocketPool] Chat ${convId} socket closed. Code: ${e.code}, wasClean: ${e.wasClean}`);

        if (COOLEMS.currentConversation === convId) {
            updateConnectionStatus('', 'Disconnected');
            setButtonState(false);
        }

        // Normal closure from a deliberate close — nothing to do.
        if (e.code === 1000 || e.code === 1001) return;

        // Per-chat reconnect with backoff. The in-flight flag lives on the ENTRY, not in a
        // global: two background sockets closing near-simultaneously must never block each
        // other's reconnect (the last shared-state hazard of the old single-socket design).
        if (!e.wasClean && !entry.reconnecting && entry.reconnectAttempts < COOLEMS.MAX_RECONNECT_ATTEMPTS) {
            entry.reconnecting = true;
            entry.reconnectAttempts++;
            const delay = 2000 * entry.reconnectAttempts;
            updateConnectionStatus('connecting', `Reconnecting (${entry.reconnectAttempts}/${COOLEMS.MAX_RECONNECT_ATTEMPTS})...`);
            setTimeout(() => {
                // Only reconnect if the socket was not replaced in the meantime.
                const cur = this.sockets[convId];
                if (cur && cur.ws === entry.ws) {
                    delete this.sockets[convId];
                    this._touch(convId);
                    this._create(convId);
                } else {
                    entry.reconnecting = false;   // replaced or deliberately closed in the meantime
                }
            }, delay);
        } else if (!e.wasClean && entry.reconnectAttempts >= COOLEMS.MAX_RECONNECT_ATTEMPTS) {
            showNotification('Connection lost. Please refresh the page.', 'error');
            updateConnectionStatus('error', 'Failed');
        }
    },

    // ===== Status tracking (dots + eviction safety) ==================================

    /** Map one frame to a chat status and refresh its sidebar dot (+ attention badge). */
    _trackStatus(convId, msg) {
        const t = msg && msg.type;

        // (2026-10-09 attention markers) A BACKGROUND workspace needs the user's eye:
        //  * a decision card appeared -> blinking 'dialog' badge until answered/visited;
        //  * a real turn just ended   -> temporary green 'done' flash (~30 s);
        //  * generating frames resume after a dialog -> clear it (answered or auto-fired).
        if (convId !== COOLEMS.currentConversation) {
            const prev = this.statuses[convId] || 'idle';
            if (t === 'exec_approval_request') {
                this.setAttention(convId, 'dialog');
            } else if ((t === 'done' || t === 'error' || t === 'cancelled' ||
                        t === 'provider_error' || t === 'execution_fail') &&
                       (prev === 'generating' || prev === 'queued')) {
                this.setAttention(convId, 'done');
            } else if ((t === 'queue_start' || t === 'content' || t === 'thinking' ||
                        t === 'tool_start' || t === 'tool_end') &&
                       this.attention[convId] === 'dialog') {
                this.clearAttention(convId);   // the dialog was answered - turn resumed
            }
        }

        // (2026-09-08 multi-chat UI fix) AUTHORITATIVE frame: the server sends it right after
        // replaying a chat's buffered tail on re-attach. It carries the channel's real state,
        // so switching back to a generating/queued chat restores its dot even when no frames
        // are currently flowing (and clears it when the turn finished while away).
        if (t === 'chat_status') {
            const st2 = ['idle', 'queued', 'generating'].includes(msg.status) ? msg.status : 'idle';
            if (st2 !== this.statuses[convId]) {
                this.statuses[convId] = st2;
                this._setDot(convId, st2);
            }
            return;
        }

        let st = this.statuses[convId] || 'idle';
        if (t === 'queue_status') st = 'queued';
        else if (t === 'queue_start' || t === 'content' || t === 'thinking' || t === 'tool_start' ||
                 t === 'tool_end' || t === 'image_generated' || t === 'recovery_warning' ||
                 t === 'token_stats') st = 'generating';   // queue_start = the turn actually began
        else if (t === 'done' || t === 'error' || t === 'cancelled' ||
                 t === 'provider_error' || t === 'execution_fail') st = 'idle';
        // system frames keep the current state.

        if (st !== this.statuses[convId]) {
            this.statuses[convId] = st;
            this._setDot(convId, st);
        }
    },

    /** Update one chat's sidebar status dot directly (also used by the stop button). */
    updateChatStatus(convId, status) {
        const known = ['idle', 'queued', 'generating'];
        if (!status || !known.includes(status)) return;   // unknown — ignore
        this.statuses[convId] = status;
        this._setDot(convId, status);
    },

    _setDot(convId, status) {
        const item = document.querySelector(`.chat-history-item[data-conv-id="${convId}"]`);
        if (!item) return;   // sidebar not rendered yet — dot is added on next render via class
        let dot = item.querySelector('.chat-status-dot');
        if (!dot) {
            dot = document.createElement('span');
            const anchor = item.querySelector('.chat-color-dot') || item.firstChild;
            item.insertBefore(dot, anchor ? anchor.nextSibling : null);
        }
        dot.className = `chat-status-dot status-${status}`;
        this._updatePill();
    },

    // ===== Attention badge API (2026-10-09) =========================================

    /** Set a workspace's attention state ('dialog' | 'done') and update its sidebar badge. */
    setAttention(convId, kind) {
        if (!convId || !kind) return;
        // A fresh dialog always wins over an old completion flash (and does not re-arm its timer).
        if (this.attention[convId] === 'done' && kind !== 'dialog') return;
        this.attention[convId] = kind;
        // The 'done' flash expires on its own so the sidebar stays quiet. Scheduled at STATE level (not in
        // _setAttentionBadge): when a turn ends while the sidebar item is not rendered yet, the badge will
        // appear on the next render but must still expire by itself.
        const timer = this._attentionTimers[convId];
        if (timer) { clearTimeout(timer); delete this._attentionTimers[convId]; }
        if (kind === 'done') {
            this._attentionTimers[convId] = setTimeout(() => this.clearAttention(convId), 30000);
        }

        this._setAttentionBadge(convId, kind);
    },

    /** Clear a workspace's attention state (visited / answered / expired). */
    clearAttention(convId) {
        if (!this.attention[convId]) return;
        delete this.attention[convId];
        const timer = this._attentionTimers[convId];
        if (timer) { clearTimeout(timer); delete this._attentionTimers[convId]; }
        this._setAttentionBadge(convId, null);
    },

    /** Update one workspace's sidebar attention badge element only (timers live in set/clearAttention). */
    _setAttentionBadge(convId, kind) {
        const item = document.querySelector(`.chat-history-item[data-conv-id="${convId}"]`);
        if (!item) return;   // sidebar not rendered yet — badge is added on next render

        let badge = item.querySelector('.chat-attention-badge');
        if (kind === null) {
            if (badge) badge.remove();
            return;
        }
        if (!badge) {
            badge = document.createElement('span');
            const dot = item.querySelector('.chat-status-dot') || item.querySelector('.chat-color-dot');
            const anchor = dot ? dot.nextSibling : null;
            item.insertBefore(badge, anchor);
        }
        badge.className = `chat-attention-badge attention-${kind}`;
    },

    /** Aggregate pill: appends "· N gen · M queued" to the connection status text. */
    _updatePill() {
        const el = document.getElementById('connectionStatus');
        if (!el) return;
        let generating = 0, queued = 0;
        for (const st of Object.values(this.statuses)) {
            if (st === 'generating') generating++;
            else if (st === 'queued') queued++;
        }

        const span = el.querySelector('span');
        if (!span) return;
        // strip a previous multi-chat suffix before appending the new one
        let text = span.textContent.replace(/\s*·\s*(\d+(?: gen)?(?: · \d+ queued)?|(\d+ (?:chats|workspaces) live))$/, '');
        let suffix = '';
        if (generating || queued) {
            suffix = ` · ${generating} gen`;
            if (queued) suffix += ` · ${queued} queued`;
        } else if (Object.keys(this.sockets).length > 1) {
            suffix = ` · ${Object.keys(this.sockets).length} workspaces live`;
        }
        span.textContent = text + suffix;
    },

    /** Close everything (page unload / hard reset). */
    closeAll() {
        for (const id of Object.keys(this.sockets)) this.close(id, true);
        if (typeof WorkspaceViews !== 'undefined') WorkspaceViews.clearAll();
        // Clean slate: drop attention state + pending done-flash timers with the rest.
        for (const id of Object.keys(this._attentionTimers)) {
            clearTimeout(this._attentionTimers[id]); delete this._attentionTimers[id];
        }
        this.attention = {};
        COOLEMS.ws = null;
    },
};

// ===== Backwards-compatible shims over the old single-socket API =====================

/**
 * connectWebSocket(convId) — pool-aware: get/reuse THIS chat's socket. NEVER closes any
 * other chat's socket (that was exactly the bug this refactor fixes).
 */
function connectWebSocket(convId) {
    updateConnectionStatus('connecting', 'Connecting...');
    ChatSocketPool.ensure(convId);
}

/** createNewWebSocket kept as a shim for legacy callers. */
function createNewWebSocket(convId) {
    return ChatSocketPool.ensure(convId);
}

// ===== Status dot CSS (injected once; keeps styles.css untouched by this file) ========
(function injectStatusDotStyles() {
    const style = document.createElement('style');
    style.textContent = `
        .chat-status-dot {
            width: 8px; height: 8px; border-radius: 50%;
            background: var(--text-muted, #666); flex-shrink: 0; opacity: 0.45;
        }
        .chat-status-dot.status-queued { background: #e0a800; opacity: 1; }
        .chat-status-dot.status-generating {
            background: #2ecc71; opacity: 1;
            animation: chat-dot-pulse 1.2s ease-in-out infinite;
        }
        @keyframes chat-dot-pulse {
            0%, 100% { transform: scale(1);    box-shadow: 0 0 0 0 rgba(46, 204, 113, 0.5); }
            50%      { transform: scale(1.25); box-shadow: 0 0 0 4px rgba(46, 204, 113, 0); }
        }

        /* (2026-10-09) attention badges: a background workspace that needs the user's eye. */
        .chat-attention-badge {
            width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; margin-left: 3px;
        }
        /* pending decision (python_exec approval / URL consent) — blinks until answered/visited */
        .chat-attention-badge.attention-dialog {
            background: #ff9f1a;
            animation: chat-attention-blink 1s ease-in-out infinite;
        }
        @keyframes chat-attention-blink {
            0%, 100% { opacity: 1;    box-shadow: 0 0 0 0 rgba(255, 159, 26, 0.7); }
            50%      { opacity: 0.25; box-shadow: 0 0 0 4px rgba(255, 159, 26, 0); }
        }
        /* task finished in the background — green flash, expires after ~30 s (JS timeout) */
        .chat-attention-badge.attention-done {
            background: #2ecc71;
            animation: chat-attention-flash 1.5s ease-in-out infinite;
        }
        @keyframes chat-attention-flash {
            0%, 100% { opacity: 1;   box-shadow: 0 0 0 0 rgba(46, 204, 113, 0.7); }
            50%      { opacity: 0.4; box-shadow: 0 0 0 3px rgba(46, 204, 113, 0); }
        }
    `;
    document.head.appendChild(style);
})();
