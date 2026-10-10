/**
 * workspace-view.js - Per-workspace in-memory view stash + background frame queue
 * (2026-10-09 multi-chat UI fix).
 *
 * WHY THIS EXISTS: switching workspaces used to wipe #chatContainer and rebuild it from the
 * DB. Anything that only exists on screen — a pending python_exec approval card, an in-flight
 * streaming bubble, tool-call rows — was destroyed. And frames arriving for a HIDDEN chat were
 * discarded by the socket pool even though its socket was still open (the server delivered them
 * live and did NOT buffer them), so an approval question that appeared while you were in another
 * workspace was lost from the UI forever.
 *
 * This module fixes both halves:
 *   1. STASH — when you leave a workspace, its REAL #chatContainer DOM node is detached (not
 *      copied) and parked here with its streaming state. Detached nodes keep their listeners,
 *      dataset and running countdown timers intact, so an approval card keeps ticking while the
 *      workspace is hidden and comes back EXACTLY as it was — buttons still pushable.
 *   2. FRAME QUEUE — frames that arrive for a non-visible workspace (socket open, you are away)
 *      are queued here in order instead of being discarded. When you return they are flushed
 *      through the normal handleWebSocketMessage() path before any server replay can start.
 *
 * The two streams never overlap: the SERVER buffers frames only while NO UI socket is attached;
 * this queue holds frames from when the socket WAS attached but hidden. On a fast-path restore
 * (stash exists) no fresh handshake happens, so there is no server replay at all — the JS queue
 * IS the continuation of the stashed view.
 *
 * Eviction: LRU cap 8; never evict the current workspace or one that is generating/queued — a
 * pending dialog can never be evicted out from under a live turn (same rule as the socket pool).
 */

const WorkspaceViews = {
    maxStashed: 8,        // stashed DOM trees kept in memory (LRU)
    maxQueuedFrames: 500, // per-workspace background frame queue cap (drop-oldest)

    views: {},            // convId -> { node, streamingBuffer, baselineAssistant, scrollPos, midStream }
    order: [],            // LRU order for stashed views: least recently used at index 0
    queues: {},           // convId -> [frame, frame, ...] (background frames awaiting restore)

    // (2026-10-09 switch-race fix) convIds whose frames must be QUEUED instead of rendered
    // while a workspace switch is in flight. The outgoing workspace's view is stashed
    // (detached), so its live frames cannot render into the container that is being rebuilt
    // for the incoming one - but during the rebuild window it is still COOLEMS.currentConversation,
    // and the normal "not current -> queue" routing would let them through. Cleared when the
    // switch completes or reverts; after that, ordinary background routing applies again.
    suspended: {},

    /** True when a live view is parked for this workspace. */
    hasView(convId) { return !!this.views[convId]; },

    // ===== Switch suspension (2026-10-09 switch-race fix) ==========================

    /** Mark a conv as suspended: its frames queue instead of rendering during the switch. */
    suspend(convId) { if (convId) this.suspended[convId] = true; },

    /** Clear suspension - the switch completed or reverted to this workspace. */
    resume(convId) { delete this.suspended[convId]; },

    isSuspended(convId) { return !!this.suspended[convId]; },

    /** Peek the stashed state without touching LRU order (used by the fast-path eligibility check). */
    peekState(convId) { return this.views[convId] || null; },

    _touch(convId) {
        this.order = this.order.filter(id => id !== convId);
        this.order.push(convId);   // most recently used at the end
    },

    /** Detach a workspace's live #chatContainer node and park it. No-op when already stashed. */
    stash(convId) {
        if (!convId || this.views[convId]) return;
        const container = document.getElementById('chatContainer');
        if (!container) return;

        // Remember the in-flight streaming state of THIS view so a fast restore can continue it.
        let midStream = false;
        for (const el of container.querySelectorAll('[data-streaming]')) { midStream = true; break; }
        const lastMsg = container.lastElementChild;
        if (lastMsg && lastMsg.classList.contains('assistant') && lastMsg.dataset.streaming) midStream = true;

        // Detach the REAL node — listeners, dataset and countdown intervals stay attached to it.
        const node = document.createElement('div');   // holder keeps the subtree out of layout
        while (container.firstChild) node.appendChild(container.firstChild);

        this.views[convId] = {
            node: node,
            streamingBuffer: COOLEMS.streamingBuffer || '',
            baselineAssistant: COOLEMS._baselineAssistant || 0,
            scrollPos: container.scrollTop || 0,
            midStream: midStream,
            // The folder this workspace was using — re-activated on fast-path restore so a
            // background switch can never leave the header chip on another workspace's path.
            workingRoot: (typeof currentWorkingRoot !== 'undefined') ? currentWorkingRoot : '',
        };
        this._touch(convId);

        // LRU eviction: never the current workspace, never a live (generating/queued) one.
        while (this.order.length > this.maxStashed) {
            const victim = this._findEvictable();
            if (!victim) break;
            log(`[WorkspaceViews] Evicted stashed view for ${victim} (cap=${this.maxStashed})`);
            this.drop(victim, false);
        }
    },

    /** Oldest stash that is safe to evict: not current, and NOT generating/queued. */
    _findEvictable() {
        for (const id of this.order) {
            if (!this.views[id]) continue;
            if (id === COOLEMS.currentConversation) continue;
            const st = (typeof ChatSocketPool !== 'undefined') ? (ChatSocketPool.statuses[id] || 'idle') : 'idle';
            if (st === 'generating' || st === 'queued') continue;   // never kill a live view
            return id;
        }
        return null;
    },

    /** Reattach the stashed node into #chatContainer and restore its streaming state. */
    restore(convId) {
        const v = this.views[convId];
        if (!v) return false;

        const container = document.getElementById('chatContainer');
        if (!container) return false;

        // ADOPT the stashed children directly into #chatContainer: its DIRECT children must be
        // the message nodes themselves — handleWebSocketMessage() streams via lastElementChild,
        // so wrapping them in a holder div would break every continuation. Moving nodes keeps
        // their listeners, dataset and running countdown timers intact.
        while (container.firstChild) container.removeChild(container.firstChild);
        while (v.node.firstChild) container.appendChild(v.node.firstChild);
        container.scrollTop = v.scrollPos || 0;

        COOLEMS.streamingBuffer = v.streamingBuffer;
        COOLEMS._baselineAssistant = v.baselineAssistant;
        // The stashed view was NOT rebuilt from the DB: replayed/queued frames are pure
        // continuation of what is already on screen, so the replay guard must stay OFF.
        COOLEMS._inReplay = false;

        delete this.views[convId];
        return true;
    },

    /** Drop a workspace's stash (and optionally its frame queue). */
    drop(convId, alsoFrames) {
        if (this.views[convId]) {
            delete this.views[convId];
            this.order = this.order.filter(id => id !== convId);
        }
        if (alsoFrames) this.dropFrames(convId);
    },

    // ===== Background frame queue ====================================================

    /** Queue one background frame for a hidden workspace (bounded, drop-oldest). */
    queueFrame(convId, msg) {
        if (!convId) return;
        let q = this.queues[convId];
        if (!q) { q = []; this.queues[convId] = q; }
        q.push(msg);
        while (q.length > this.maxQueuedFrames) q.shift();   // drop-oldest, same as the server buffer
    },

    /** True when a hidden workspace has queued frames awaiting restore. */
    hasQueuedFrames(convId) { return !!(this.queues[convId] && this.queues[convId].length); },

    /** Flush queued frames in order through the normal message handler; returns count flushed. */
    flushFrames(convId) {
        const q = this.queues[convId];
        if (!q || !q.length) return 0;
        delete this.queues[convId];
        for (const msg of q) handleWebSocketMessage(msg);
        return q.length;
    },

    /** Discard queued frames without rendering (e.g. a mid-stream view that goes to the DB path). */
    dropFrames(convId) {
        if (this.queues[convId]) delete this.queues[convId];
    },

    /** Drop everything (page unload / hard reset). */
    clearAll() {
        for (const id of Object.keys(this.views)) this.drop(id, true);
        // Queues can also exist WITHOUT a stashed view (a background chat that never had its
        // DOM parked) - the views loop above would miss them, so reset the map outright.
        this.queues = {};
        this.order = [];
        this.suspended = {};
    },
};
