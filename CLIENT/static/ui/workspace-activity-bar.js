// static/ui/workspace-activity-bar.js

/**
 * workspace-activity-bar.js - the sidebar's activity / warning bar (2026-11-28).
 *
 * ONE row between the "New Workspace" header and the workspace list. It answers the question
 * you lose the moment several workspaces run in parallel: "is anything still working, and is
 * anything waiting for me?"
 *
 *   idle       nothing operating          -> same background as the rest of the sidebar
 *   running    at least one generating/queued -> light blue; click jumps to the NEXT one
 *   finished   finished & not opened yet  -> green; click cycles them. Opening one marks it
 *                                            seen, so it leaves the cycle after a single pass.
 *   attention  a workspace parked on a decision card -> orange, which OVERRIDES the blue
 *              (a blue underline stays while tasks still run); click cycles the workspaces
 *              that need the user.
 *
 * Click priority is attention > running > finished. The other non-empty group is shown as a
 * small chip on the right that cycles ONLY itself - that is how running stays reachable while
 * a dialog is pending.
 *
 * Navigation reuses the sidebar's own path (ConversationMgr.markSeen + loadConversation), so
 * the bar and the list below can never disagree about which workspace is open.
 *
 * State sources: ChatSocketPool.statuses (live frames), ChatSocketPool.attention
 * ('dialog' | 'done'), the per-workspace unread_finished flag in ConversationMgr.byId, and
 * GET /api/conversations/live for workspaces this browser has no socket for yet (page reload).
 */

/**
 * Pure resolver - no DOM, no globals (unit-tested in node).
 *
 * @param {{order: string[], statuses?: object, attention?: object, finished?: object,
 *          serverLive?: object, serverAttention?: object}} input
 *   order           workspace ids in sidebar display order
 *   statuses        id -> 'idle' | 'queued' | 'generating' (live frames)
 *   attention       id -> 'dialog' | 'done' (live markers)
 *   finished        id -> true (persisted "finished & unread" flag)
 *   serverLive      id -> status reported by /api/conversations/live (no socket yet)
 *   serverAttention id -> true for a server-reported dialog the user has not visited
 * @returns {{primary: string, groups: {attention: string[], running: string[],
 *            finished: string[]}}}
 */
function resolveWorkspaceActivity(input) {
    const src = input || {};
    const order = src.order || [];
    const statuses = src.statuses || {};
    const attention = src.attention || {};
    const finished = src.finished || {};
    const serverLive = src.serverLive || {};
    const serverAttention = src.serverAttention || {};

    const running = [];
    const dialogs = [];
    const done = [];
    const seen = new Set();
    for (const id of order) {
        if (!id || seen.has(id)) continue;
        seen.add(id);
        const status = statuses[id] || serverLive[id] || 'idle';
        const isRunning = status === 'generating' || status === 'queued';
        const needsUser = attention[id] === 'dialog' || !!serverAttention[id];
        if (isRunning) running.push(id);
        if (needsUser) dialogs.push(id);
        // A workspace with a live task is NOT "finished", and neither is one parked on a
        // decision card (its task is waiting, not done) - the running/dialog fact wins.
        if (!isRunning && !needsUser && (attention[id] === 'done' || finished[id])) done.push(id);
    }

    const primary = dialogs.length ? 'attention'
        : (running.length ? 'running' : (done.length ? 'finished' : 'idle'));
    return { primary, groups: { attention: dialogs, running, finished: done } };
}

/**
 * Next id to open inside one group. Works from the CURRENT workspace when it is in the group
 * (so repeated clicks walk the whole list), otherwise continues after the previously visited
 * one (which matters for groups that shrink as they are visited, e.g. finished/attention).
 */
function nextInCycle(list, currentId, lastId) {
    if (!Array.isArray(list) || !list.length) return null;
    const at = list.indexOf(currentId);
    if (at >= 0) return list[(at + 1) % list.length];
    const last = list.indexOf(lastId);
    return list[(last + 1) % list.length];   // last missing -> index 0
}

// ===== The bar =====

/** The sidebar manager, whichever way it is reachable (window property in the page). */
function sidebarManager() {
    if (typeof window !== 'undefined' && window.ConversationMgr) return window.ConversationMgr;
    if (typeof ConversationMgr !== 'undefined') return ConversationMgr;
    return null;
}

const WorkspaceActivityBar = {
    GROUPS: ['attention', 'running', 'finished'],
    POLL_MS: 4000,

    el: null,
    dotEl: null,
    labelEl: null,
    chipEl: null,
    posEl: null,

    state: null,
    lastJump: {},            // group -> convId of the last workspace the bar opened
    serverLive: {},          // id -> 'queued' | 'generating' (from /api/conversations/live)
    serverAttention: {},     // id -> true: server-reported dialog the user has not visited
    acked: {},               // id -> true: visited while the server still reports its dialog
    serverClaimed: {},       // id -> true: WE painted its pool status (no real socket owns it)
    pollTimer: null,
    mounted: false,

    /** Build the bar's DOM once and wire its click/keyboard cycling. */
    mount() {
        if (this.mounted) return true;
        const el = document.getElementById('workspaceActivityBar');
        if (!el) return false;
        this.el = el;
        el.replaceChildren();

        this.dotEl = document.createElement('span');
        this.dotEl.className = 'wab-dot';
        this.labelEl = document.createElement('span');
        this.labelEl.className = 'wab-label';
        this.chipEl = document.createElement('button');
        this.chipEl.type = 'button';
        this.chipEl.className = 'wab-chip';
        this.chipEl.hidden = true;
        this.posEl = document.createElement('span');
        this.posEl.className = 'wab-pos';
        this.posEl.hidden = true;
        el.append(this.dotEl, this.labelEl, this.chipEl, this.posEl);

        el.addEventListener('click', (event) => {
            if (event.target === this.chipEl) return;      // the chip cycles its own group
            this.cycle();
        });
        el.addEventListener('keydown', (event) => {
            if (event.target === this.chipEl) return;
            if (event.key !== 'Enter' && event.key !== ' ') return;
            event.preventDefault();
            this.cycle();
        });
        this.chipEl.addEventListener('click', (event) => {
            event.stopPropagation();
            if (this.chipEl.dataset.group) this.cycle(this.chipEl.dataset.group);
        });

        document.addEventListener('visibilitychange', () => {
            if (document.hidden) this.stopPolling();
            else {
                this.pollLive();
                this.startPolling();
            }
        });

        this.mounted = true;
        this.pollLive();
        this.startPolling();
        this.refresh();
        return true;
    },

    /** Recompute the state from every live source and repaint. Called from several hooks. */
    refresh() {
        if (!this.mounted || !this.el) return;
        this.state = this.resolve();
        this.paint(this.state);
    },

    /** Gather the sources into one state object (see resolveWorkspaceActivity). */
    resolve() {
        const mgr = sidebarManager();
        const order = mgr && mgr.byId ? Array.from(mgr.byId.keys()) : [];
        const pool = typeof ChatSocketPool !== 'undefined' ? ChatSocketPool : null;
        const finished = {};
        if (mgr && mgr.byId) {
            for (const [id, conv] of mgr.byId) if (conv && conv.unread_finished) finished[id] = true;
        }
        // A server-reported dialog counts only until the user has visited that workspace.
        const serverAttention = {};
        for (const id of Object.keys(this.serverAttention)) {
            if (!this.acked[id]) serverAttention[id] = true;
        }
        return resolveWorkspaceActivity({
            order,
            statuses: pool ? pool.statuses : {},
            attention: pool ? pool.attention : {},
            finished,
            serverLive: this.serverLive,
            serverAttention,
        });
    },

    /** Paint one resolved state: colour (data-state), label, secondary chip, position. */
    paint(state) {
        const el = this.el;
        if (!el || !state) return;
        const running = state.groups.running.length;
        const attention = state.groups.attention.length;
        const finished = state.groups.finished.length;

        el.dataset.state = state.primary;
        el.dataset.hasRunning = (state.primary === 'attention' && running) ? '1' : '0';

        let label;
        let title;
        if (state.primary === 'attention') {
            label = attention === 1 ? '1 needs you' : `${attention} need you`;
            title = `${attention} workspace(s) waiting for your decision`
                + (running ? ` · ${running} still running` : '')
                + ' - click to jump to the next one.';
        } else if (state.primary === 'running') {
            label = running === 1 ? '1 running' : `${running} running`;
            title = `${running} workspace(s) working right now - click to jump to the next one.`
                + ' Do not close the window while a task runs.';
        } else if (state.primary === 'finished') {
            label = finished === 1 ? '1 finished' : `${finished} finished`;
            title = `${finished} workspace(s) finished and not opened yet - click to cycle them.`
                + ' Each one is marked as seen, so it leaves this bar after one visit.';
        } else {
            label = 'No workspace running';
            title = 'Nothing is running in any workspace.';
        }
        this.labelEl.textContent = label;
        el.title = title;
        el.setAttribute('aria-label', `${label}. ${title}`);

        const other = this.GROUPS.find(group => group !== state.primary && state.groups[group].length);
        if (other) {
            const n = state.groups[other].length;
            this.chipEl.hidden = false;
            this.chipEl.dataset.group = other;
            this.chipEl.className = `wab-chip wab-chip-${other}`;
            this.chipEl.textContent = other === 'attention' ? `! ${n}` : (other === 'running' ? `~ ${n}` : `+ ${n}`);
            this.chipEl.title = other === 'attention' ? `Cycle ${n} workspace(s) that need you`
                : (other === 'running' ? `Cycle ${n} running workspace(s)`
                    : `Cycle ${n} finished workspace(s)`);
        } else {
            this.chipEl.hidden = true;
            this.chipEl.dataset.group = '';
        }

        const list = state.groups[state.primary] || [];
        const current = (typeof COOLEMS !== 'undefined') ? COOLEMS.currentConversation : null;
        const at = list.indexOf(current);
        this.posEl.hidden = state.primary === 'idle' || list.length < 2;
        if (!this.posEl.hidden) this.posEl.textContent = `${at >= 0 ? at + 1 : 0}/${list.length}`;
    },

    /**
     * Open the next workspace of *group* (default: the primary/highest-priority group).
     * A group that shrinks as it is visited (finished, attention) keeps walking forward
     * because nextInCycle() continues after the last workspace the bar opened.
     */
    cycle(group) {
        const state = this.state || this.resolve();
        const target = group || state.primary;
        if (!target || target === 'idle') {
            this.pollLive();      // nothing to cycle: re-check the server instead
            return;
        }
        const list = state.groups[target] || [];
        if (!list.length) return;
        const current = (typeof COOLEMS !== 'undefined') ? COOLEMS.currentConversation : null;
        const nextId = nextInCycle(list, current, this.lastJump[target]);
        if (!nextId) return;
        this.lastJump[target] = nextId;
        this.jump(nextId);
    },

    /** Open one workspace exactly like a sidebar row click (markSeen + loadConversation). */
    jump(convId) {
        if (!convId) return;
        this.acknowledge(convId);
        const mgr = sidebarManager();
        if (mgr && typeof mgr.markSeen === 'function') {
            mgr.markSeen(convId);                  // visiting = seen (finished glow / dialog mark)
        }
        const current = (typeof COOLEMS !== 'undefined') ? COOLEMS.currentConversation : null;
        if (current === convId) return;            // already on screen - nothing to switch
        if (typeof loadConversation === 'function') {
            Promise.resolve(loadConversation(convId)).catch(() => { /* handled inside */ });
        }
    },

    /** The user has this workspace on screen: stop reporting its server-side dialog. */
    acknowledge(convId) {
        if (!convId) return;
        if (this.serverAttention[convId]) this.acked[convId] = true;
        this.refresh();
    },

    /** Ask the server which workspaces are still live (covers reloads / LRU-closed sockets). */
    async pollLive() {
        try {
            const response = await fetch('/api/conversations/live');
            if (!response.ok) return;
            const data = await response.json();
            const live = {};
            const attention = {};
            for (const item of (data && data.live) || []) {
                if (!item || !item.id) continue;
                live[item.id] = item.status === 'queued' ? 'queued' : 'generating';
                if (item.needs_attention) attention[item.id] = true;
            }
            // An acknowledgement lasts only while the server keeps reporting that dialog.
            for (const id of Object.keys(this.acked)) {
                if (!attention[id]) delete this.acked[id];
            }
            for (const id of Object.keys(attention)) {
                if (this.acked[id]) delete attention[id];
            }
            this.reconcileServerLive(live);
            this.serverLive = live;
            this.serverAttention = attention;
            this.paintServerStatuses();
            this.refresh();
        } catch (e) {
            /* offline / backend restarting - keep the last known state */
        }
    },

    /** Paint the sidebar dot of workspaces this browser has no socket for yet. */
    paintServerStatuses() {
        if (typeof ChatSocketPool === 'undefined' || !ChatSocketPool.statuses) return;
        if (typeof ChatSocketPool.updateChatStatus !== 'function') return;
        for (const id of Object.keys(this.serverLive)) {
            if (ChatSocketPool.statuses[id] !== undefined) continue;   // live frames win
            ChatSocketPool.updateChatStatus(id, this.serverLive[id]);
            // We wrote this fallback: remember it so reconcileServerLive can clear it once
            // the server stops reporting the workspace as live (its socket was never open).
            this.serverClaimed[id] = true;
        }
    },

    /** Drop fallback statuses WE set for a workspace the server no longer reports live.
     *
     * Without this, a workspace whose socket was never opened (page reload, LRU-closed)
     * would keep the 'generating'/'queued' we painted into ChatSocketPool.statuses forever
     * after its task finished, because nothing else owns that entry. A real open socket
     * still owns its own status, so ids with one are left alone.
     */
    reconcileServerLive(live) {
        for (const id of Object.keys(this.serverClaimed)) {
            if (live[id] !== undefined) continue;                 // still live - keep
            if (typeof ChatSocketPool !== 'undefined' && ChatSocketPool.sockets
                    && ChatSocketPool.sockets[id]) continue;      // a real socket owns it now
            delete this.serverClaimed[id];
            if (typeof ChatSocketPool !== 'undefined' && typeof ChatSocketPool.updateChatStatus === 'function') {
                ChatSocketPool.updateChatStatus(id, 'idle');
            }
        }
    },

    startPolling() {
        this.stopPolling();
        if (document.hidden) return;
        this.pollTimer = setInterval(() => {
            if (!document.hidden) this.pollLive();
        }, this.POLL_MS);
    },

    stopPolling() {
        if (this.pollTimer) {
            clearInterval(this.pollTimer);
            this.pollTimer = null;
        }
    },
};

// Refresh hook for the other modules (socket pool, sidebar). A no-op when the bar is absent.
function refreshWorkspaceActivityBar() {
    if (typeof WorkspaceActivityBar !== 'undefined' && WorkspaceActivityBar.mounted) {
        WorkspaceActivityBar.refresh();
    }
}

// ===== Bootstrap (browser only - the test harness loads this file for its pure functions) =====
if (typeof document !== 'undefined') {
    if (typeof window !== 'undefined') window.WorkspaceActivityBar = WorkspaceActivityBar;
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', () => WorkspaceActivityBar.mount());
    } else {
        WorkspaceActivityBar.mount();
    }
}
if (typeof module !== 'undefined' && module.exports) {
    module.exports = { resolveWorkspaceActivity, nextInCycle, WorkspaceActivityBar };
}
