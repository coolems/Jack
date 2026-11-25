/**
 * chat-socket-pool.js - Per-chat WebSocket pool (2026-09-08 multi-chat, Phase 4).
 *
 * THE core UI piece of "keep every chat connected": instead of ONE global COOLEMS.ws that
 * was closed and reopened on every chat switch (which killed the previous chat's session),
 * each conversation owns its OWN WebSocket in a small map:
 *
 *     convId -> { ws, reconnecting }
 *
 *  - Switching chats NEVER closes another chat's socket — its generation keeps streaming.
 *  - LRU cap: at most ChatSocketPool.maxOpen sockets stay open; the least-recently-used
 *    IDLE one is closed when a new chat opens (its frames keep buffering server-side).
 *    A chat that is generating/queued is NEVER evicted — closing it would kill the session.
 *  - Per-chat reconnect on a fixed 3 s cadence, scoped to that conversation only.
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
    sockets: {},          // convId -> { ws, reconnecting }
    statuses: {},         // convId -> 'idle' | 'queued' | 'generating' (drives dots + eviction safety)
    // (2026-10-23 live steering) convId -> bool: whether that chat's current/last turn is
    // agentic and therefore accepts a mid-run steering message. Reported by chat_status.
    agentModes: {},
    order: [],            // LRU order: least recently used at index 0
    maxOpen: 10,          // Idle eviction target; live sockets are never evicted.
    // (2026-06-17 idle close) a BACKGROUND chat whose turn just ended gets this much grace
    // before its socket is closed. Any activity (new frames, switch-back) cancels the timer.
    idleCloseMs: 5 * 60 * 1000,
    idleTimers: {},       // convId -> timeout handle

    // ===== Sidebar attention markers (2026-10-09) ==================================
    // Orthogonal to session status: a chat can be 'generating' AND waiting on a dialog.
    //   'dialog' - a background workspace has a pending decision card (python_exec approval,
    //              URL consent): BLINKS until answered or the user visits that workspace.
    //   'done'   - a background workspace just finished its task: green flash for ~30 s.
    attention: {},        // convId -> 'dialog' | 'done'

    // (2026-11-15 consent-card lifetime) convId -> Set of request_ids whose card is still
    // unanswered. A chat parked on a consent card is LIVE WORK: the turn is blocked on that
    // decision, so its socket must never be LRU-evicted (and its stashed view neither) —
    // otherwise the card the user still has to answer loses its transport.
    pendingCards: {},

    /** True when this conversation still has an unanswered consent card on screen. */
    hasPendingCard(convId) {
        const s = this.pendingCards[convId];
        return !!(s && s.size);
    },

    _addPendingCard(convId, requestId) {
        if (!convId) return;
        let s = this.pendingCards[convId];
        if (!s) { s = new Set(); this.pendingCards[convId] = s; }
        s.add(requestId || '');
    },

    _removePendingCard(convId, requestId) {
        const s = this.pendingCards[convId];
        if (!s) return;
        s.delete(requestId || '');
        if (!s.size) delete this.pendingCards[convId];
    },

    /** Open (or reuse) the socket for one conversation. NEVER closes other chats' sockets. */
    ensure(convId) {
        if (!convId) return null;

        const existing = this.sockets[convId];
        if (existing && (existing.ws.readyState === WebSocket.OPEN ||
                         existing.ws.readyState === WebSocket.CONNECTING)) {
            this._touch(convId);
            this._cancelIdleTimer(convId);   // user switched back — the socket is needed again
            // Legacy alias: keep COOLEMS.ws pointing at the CURRENT chat's socket even when
            // the pooled socket is reused (a switch-back must not leave it on another chat).
            if (COOLEMS.currentConversation === convId) {
                COOLEMS.ws = existing.ws;
                const open = existing.ws.readyState === WebSocket.OPEN;
                updateConnectionStatus(open ? 'connected' : 'connecting', open ? 'Connected' : 'Connecting...');
            }
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
        this._cancelIdleTimer(convId);

        // Null every handler BEFORE closing: _onClose() also guards on sockets[convId],
        // so a late close event can never trigger a reconnect for a deliberate close.
        try {
            entry.ws.onopen = null;
            entry.ws.onmessage = null;
            entry.ws.onerror = null;
            entry.ws.onclose = null;
        } catch (e) { /* already gone */ }

        try { entry.ws.close(); } catch (e) { logError('[ChatSocketPool] close error:', e); }

        // (2026-11-15) this chat's transport is gone deliberately (delete / LRU / unload), so
        // its pending-card bookkeeping goes with it — nothing can be answered on it any more.
        delete this.pendingCards[convId];

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

    // ===== Idle-close countdown (2026-06-17) ========================================
    // A BACKGROUND chat whose turn just finished (status -> 'idle') is parked: its socket
    // is closed after idleCloseMs UNLESS something re-activates it first (a new frame, or
    // the user switching back). The current chat is never timed out.

    /** Arm the countdown for one background chat (no-op if it is the current chat). */
    _startIdleTimer(convId) {
        this._cancelIdleTimer(convId);
        if (convId === COOLEMS.currentConversation) return;
        this.idleTimers[convId] = setTimeout(() => {
            delete this.idleTimers[convId];
            // Re-check at fire time: the chat may have come back to life in the meantime.
            const st = this.statuses[convId];
            if (convId === COOLEMS.currentConversation) return;
            if (st === 'generating' || st === 'queued' || pendingChatSends.has(convId)) return;
            if (this.hasPendingCard(convId)) return;
            log(`[ChatSocketPool] Idle close: chat ${convId} quiet for ${this.idleCloseMs / 1000}s, closing socket`);
            this.close(convId, true);
        }, this.idleCloseMs);
    },

    /** Cancel the countdown (activity on this chat — frame arrived or user switched back). */
    _cancelIdleTimer(convId) {
        if (this.idleTimers[convId]) {
            clearTimeout(this.idleTimers[convId]);
            delete this.idleTimers[convId];
        }
    },

    /**
     * Call when a chat goes to the BACKGROUND (workspace switch). If it is already idle its
     * countdown starts NOW; if it is still generating, _setStatus() arms it later at the
     * done -> idle transition. No-op while the chat is still current.
     */
    parkIdle(convId) {
        if (!convId || convId === COOLEMS.currentConversation) return;
        this._cancelIdleTimer(convId);
        if ((this.statuses[convId] || 'idle') === 'idle') this._startIdleTimer(convId);
    },

    /** Oldest socket that is safe to evict: not current, NOT generating/queued, no pending card. */
    _findEvictable() {
        for (const id of this.order) {
            if (!this.sockets[id]) continue;
            if (id === COOLEMS.currentConversation) continue;
            const st = this.statuses[id];
            if (st === 'generating' || st === 'queued' || pendingChatSends.has(id)) continue;   // never kill a live session
            // (2026-11-15) a chat waiting on a consent card is live work: closing this socket
            // would leave the user's card with no transport to deliver the answer on.
            if (this.hasPendingCard(id)) continue;
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
            if (COOLEMS.currentConversation === convId) updateConnectionStatus('error', 'Error');
            return null;
        }

        const entry = { ws, reconnecting: false };
        this.sockets[convId] = entry;
        this._touch(convId);

        // Keep the legacy global alias pointing at the CURRENT chat's socket.
        if (COOLEMS.currentConversation === convId) {
            COOLEMS.ws = ws;
            updateConnectionStatus('connecting', 'Connecting...');
        }

        ws.onopen = () => {
            if (this.sockets[convId] !== entry) return;
            entry.reconnecting = false;
            if (COOLEMS.currentConversation === convId) {
                updateConnectionStatus('connected', 'Connected');
            }
            this._updatePill();
        };

        ws.onmessage = (e) => {
            if (this.sockets[convId] !== entry) return;
            let msg;
            try { msg = JSON.parse(e.data); }
            catch (err) { logError('[ChatSocketPool] Parse error:', err); return; }

            if (msg.type === 'workspace_title_changed') {
                // Read the committed title even if this workspace is in the background.
                void loadConversations();
                return;
            }

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
            // (2026-11-24 mix fix) pass the OWNING conversation: the handler now keeps all
            // streaming state per-conv instead of in globals shared by every workspace.
            handleWebSocketMessage(msg, convId);
        };

        ws.onerror = () => {
            if (this.sockets[convId] === entry && COOLEMS.currentConversation === convId) {
                // No toast: the header pill is the single source of truth for transport
                // health. onclose follows immediately and starts the (indefinite)
                // per-chat reconnect loop.
                updateConnectionStatus('connecting', 'Reconnecting...');
            }
        };

        // Unexpected close -> per-chat reconnect on a fixed 3 s cadence (scoped to THIS chat).
        ws.onclose = (e) => this._onClose(convId, entry, e);

        this._updatePill();
        return ws;
    },

    _onClose(convId, entry, e) {
        if (this.sockets[convId] !== entry) return;

        log(`[ChatSocketPool] Chat ${convId} socket closed. Code: ${e.code}, wasClean: ${e.wasClean}`);

        // A deliberate local close removes the entry first. Clean server shutdowns
        // still need a reconnect, except when the server explicitly rejected the client.
        const rejected = [1008, 4001, 4003].includes(e.code);
        if (COOLEMS.currentConversation === convId) {
            updateConnectionStatus(rejected ? 'disconnected' : 'connecting',
                                   rejected ? 'Disconnected' : 'Reconnecting...');
            setButtonState(false);
        }
        if (rejected) {
            delete this.sockets[convId];
            this.order = this.order.filter(id => id !== convId);
            if (COOLEMS.ws === entry.ws) COOLEMS.ws = null;
            this._updatePill();
            return;
        }

        if (entry.reconnecting) return;
        entry.reconnecting = true;
        setTimeout(() => {
            if (this.sockets[convId] !== entry) return;
            // Keep retrying if construction fails, but never resurrect a replaced entry.
            if (!this._create(convId)) {
                entry.reconnecting = false;
                this._onClose(convId, entry, e);
            }
        }, 3000);
    },

    // ===== Status tracking (dots + eviction safety) ==================================

    /** Map one frame to a chat status and refresh its sidebar dot (+ attention badge). */
    _trackStatus(convId, msg) {
        const t = msg && msg.type;

        // (2026-11-15 consent-card lifetime) track unanswered cards for EVERY chat, visible
        // or not: eviction safety depends on this, and frames for a hidden workspace arrive
        // here too (they are queued for render, never skipped).
        if (t === 'exec_approval_request' || t === 'cycle_consent_pending') {
            this._addPendingCard(convId, msg.request_id);
        } else if (t === 'cycle_consent' || t === 'consent_cancelled' ||
                   (t === 'cycle_question' && msg.pending === false)) {
            this._removePendingCard(convId, msg.request_id);
        } else if (t === 'done' || t === 'error' || t === 'cancelled' ||
                   t === 'provider_error' || t === 'execution_fail' || t === 'cycle_end') {
            // The turn is over: no card of it can be answered any more.
            delete this.pendingCards[convId];
        }

        // (2026-10-09 attention markers) A BACKGROUND workspace needs the user's eye:
        //  * a decision card appeared -> blinking 'dialog' badge until answered/visited;
        //  * a real turn just ended   -> persistent green 'done' glow until visited;
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
                        t === 'tool_start' || t === 'tool_end' || t === 'cycle_consent' ||
                        t === 'consent_cancelled') &&
                        this.attention[convId] === 'dialog') {
                this.clearAttention(convId);   // the dialog was answered - turn resumed
            }
        }

        // (2026-11-24 persistent "finished" flag) when the CURRENT workspace's turn ends while
        // the user is watching, clear the finished-unread flag immediately - they have seen it.
        // (Background workspaces are handled above: setAttention('done') glows them until a click.)
        if (convId === COOLEMS.currentConversation &&
            (t === 'done' || t === 'error' || t === 'cancelled' ||
             t === 'provider_error' || t === 'execution_fail')) {
            if (typeof ConversationMgr !== 'undefined' && typeof ConversationMgr.markSeen === 'function') {
                ConversationMgr.markSeen(convId);
            }
        }

        // (2026-09-08 multi-chat UI fix) AUTHORITATIVE frame: the server sends it right after
        // replaying a chat's buffered tail on re-attach. It carries the channel's real state,
        // so switching back to a generating/queued chat restores its dot even when no frames
        // are currently flowing (and clears it when the turn finished while away).
        if (t === 'chat_status') {
            const st2 = ['idle', 'queued', 'generating'].includes(msg.status) ? msg.status : 'idle';
            this._setStatus(convId, st2);
            // (2026-06-17 idle close) re-attach safety net: _setStatus only arms the countdown
            // on a TRANSITION into idle, so a reconnect whose replay says "still idle" (status
            // unchanged) would leave the socket open with no timer. parkIdle re-arms it for any
            // background chat and no-ops for the current one.
            if (st2 === 'idle') this.parkIdle(convId);
            // (2026-10-23 live steering) remember whether this chat's turn can be steered.
            if (msg.agent_mode !== undefined) this.agentModes[convId] = !!msg.agent_mode;
            return;
        }

        let st = this.statuses[convId] || 'idle';
        if (t === 'queue_status') st = 'queued';
        else if (t === 'queue_start' || t === 'content' || t === 'thinking' || t === 'tool_start' ||
                 t === 'tool_end' || t === 'image_generated' || t === 'recovery_warning' ||
                 t === 'token_stats') st = 'generating';   // queue_start = the turn actually began
        else if (t === 'done' || t === 'error' || t === 'cancelled' ||
                 t === 'provider_error' || t === 'ollama_error' || t === 'execution_fail') st = 'idle';
        else if (t === 'cycle_end') {
            // (2026-11-24 root-cause fix) cycle_end settles the loop-history block RIGHT BEFORE
            // 'done'. It must NOT flip a COMPLETED turn to idle early, or the background 'done'
            // attention guard (prev === 'generating'/'queued') would miss the finish and the
            // workspace would never glow. A STOPPED cycle is terminal: flip to idle now so the
            // 'done' that follows does NOT glow (stop/cancel never glows).
            const stopped = !!(msg.payload && msg.payload.final && msg.payload.final.stopped);
            if (stopped) st = 'idle';
        }
        // system frames keep the current state.

        this._setStatus(convId, st);
    },

    /** Set a chat's status, refresh its dot, and manage the idle-close countdown. */
    _setStatus(convId, st) {
        if (st === this.statuses[convId]) return;
        this.statuses[convId] = st;
        this._setDot(convId, st);
        // Transition INTO idle (turn just ended) arms the countdown; any active state cancels it.
        if (st === 'idle') this._startIdleTimer(convId);
        else this._cancelIdleTimer(convId);
        // (2026-11-28 activity bar) every status change repaints the sidebar activity bar.
        if (typeof refreshWorkspaceActivityBar === 'function') refreshWorkspaceActivityBar();
    },

    /** Update one chat's sidebar status dot directly (also used by the stop button). */
    updateChatStatus(convId, status) {
        const known = ['idle', 'queued', 'generating'];
        if (!status || !known.includes(status)) return;   // unknown — ignore
        this._setStatus(convId, status);
    },

    _setDot(convId, status) {
        const item = document.querySelector(`.chat-history-item[data-conv-id="${convId}"]`);
        // Global activity must update even when the sidebar has no row for this chat.
        this._updatePill();
        if (!item) return;
        let dot = item.querySelector('.chat-status-dot');
        if (!dot) {
            dot = document.createElement('span');
            const anchor = item.firstChild;
            item.insertBefore(dot, anchor ? anchor.nextSibling : null);
        }
        dot.className = `chat-status-dot status-${status}`;
    },

    // ===== Attention badge API (2026-10-09) =========================================

    /** Set a workspace's attention state ('dialog' | 'done') and update its sidebar badge. */
    setAttention(convId, kind) {
        if (!convId || !kind) return;
        // A fresh dialog always wins over an old completion glow (and does not re-arm it).
        if (this.attention[convId] === 'done' && kind !== 'dialog') return;
        this.attention[convId] = kind;
        // (2026-11-24) the 'done' marker is now PERSISTENT: it stays until the user visits the
        // workspace (markSeen -> clearAttention), so there is no expiry timer. The badge and the
        // .workspace-finished glow live until clearAttention() removes them.
        this._setAttentionBadge(convId, kind);
        // (2026-11-28 activity bar) a dialog/completion marker repaints the sidebar bar too.
        if (typeof refreshWorkspaceActivityBar === 'function') refreshWorkspaceActivityBar();
    },

    /** Clear a workspace's attention state (visited / answered). */
    clearAttention(convId) {
        if (!this.attention[convId]) return;
        delete this.attention[convId];
        this._setAttentionBadge(convId, null);
        // (2026-11-28 activity bar) the marker is gone -> the bar drops this workspace.
        if (typeof refreshWorkspaceActivityBar === 'function') refreshWorkspaceActivityBar();
    },

    /** Update one workspace's sidebar attention badge + finished glow element only. */
    _setAttentionBadge(convId, kind) {
        const item = document.querySelector(`.chat-history-item[data-conv-id="${convId}"]`);
        if (!item) return;   // sidebar not rendered yet — badge is added on next render

        let badge = item.querySelector('.chat-attention-badge');
        if (kind === null) {
            if (badge) badge.remove();
            item.classList.remove('workspace-finished');
            return;
        }
        if (!badge) {
            badge = document.createElement('span');
            const dot = item.querySelector('.chat-status-dot');
            const anchor = dot ? dot.nextSibling : null;
            item.insertBefore(badge, anchor);
        }
        badge.className = `chat-attention-badge attention-${kind}`;
        // Only a finished turn glows; a pending dialog is a blink, not a completion.
        item.classList.toggle('workspace-finished', kind === 'done');
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
        const text = COOLEMS._lastConnectionMessage || 'Disconnected';
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
        this.idleTimers = {};
        if (typeof WorkspaceViews !== 'undefined') WorkspaceViews.clearAll();
        // Clean slate: drop in-memory attention state (the DB flag persists and re-glows on reload).
        this.attention = {};
        this.pendingCards = {};
        COOLEMS.ws = null;
        if (typeof refreshWorkspaceActivityBar === 'function') refreshWorkspaceActivityBar();
    },
};

// ===== Backwards-compatible shims over the old single-socket API =====================

/**
 * connectWebSocket(convId) — pool-aware: get/reuse THIS chat's socket. NEVER closes any
 * other chat's socket (that was exactly the bug this refactor fixes).
 */
function connectWebSocket(convId) {
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
