// static/ui/conversation-manager.js

/** Workspace tree, sidebar sorting, and workspace actions. */
class ConversationManager extends WorkspaceSidebarControls {
    /**
     * Create a ConversationManager instance.
     */
    constructor() {
        super();
        /** @type {{ by: string, order: string }} Current sort state */
        this.sortConfig = { ...ConversationManager.DEFAULT_SORT };

        /** @type {HTMLDivElement|null} Reference to #chatHistory */
        this.container = null;

        this.byId = new Map();
        this.children = new Map();
        this.parentById = new Map();
        this.rows = new Map();
        this.collapsed = new Set();
        this.branches = new Map();
        this.selectedId = null;
        this.dragId = null;
        this.dropTarget = null;
        this.moving = false;
    }

    // ===== INITIALIZATION =====

    /**
     * Initialize the ConversationManager.
     * Injects sort controls into the sidebar header and binds events.
     */
    init() {
        this.container = document.getElementById('chatHistory');
        this.path = document.getElementById('workspacePath');
        this._bindDropTarget(this.path, null);
        document.addEventListener('dragend', () => this._endDrag());
        document.addEventListener('click', (event) => {
            if (this.menu && !this.menu.contains(event.target)) this._closeMenu();
            if (this.activePicker && !this.activePicker.contains(event.target)) this._closeColorPicker();
        });
        document.addEventListener('keydown', (event) => {
            if (event.key === 'Escape') {
                this._closeMenu(true);
                this._closeColorPicker();
            }
        });
        this._injectSortControls();
        this._bindSortEvents();
        log('[ConversationManager] Initialized');
    }

    renderConversations(conversations) {
        this._closeMenu();
        this._closeColorPicker();
        this._endDrag();
        this.container.replaceChildren();
        this.byId = new Map(conversations.map(conv => [conv.id, conv]));
        for (const id of this.collapsed) if (!this.byId.has(id)) this.collapsed.delete(id);
        this.children.clear();
        this.rows.clear();
        this.branches.clear();
        this.parentById = new Map(conversations.map(conv => [
            conv.id, this.byId.has(conv.parent_id) ? conv.parent_id : null,
        ]));
        // Cut one display link per cycle, so every row still reaches Root.
        const checked = new Set();
        for (const { id } of conversations) {
            const path = new Set();
            for (let node = id; node !== null && !checked.has(node); node = this.parentById.get(node)) {
                if (path.has(node)) {
                    this.parentById.set(node, null);
                    break;
                }
                path.add(node);
            }
            for (const node of path) checked.add(node);
        }
        for (const conv of conversations) {
            const parentId = this.parentById.get(conv.id);
            if (!this.children.has(parentId)) this.children.set(parentId, []);
            this.children.get(parentId).push(conv);
        }
        this._renderBranch(null, this.container, 1);
        if (!conversations.length) {
            const empty = document.createElement('p');
            empty.className = 'workspace-empty';
            empty.textContent = 'No workspaces yet. Click New Workspace to start.';
            this.container.append(empty);
        }
        this.select(this.selectedId);
    }

    _renderBranch(parentId, container, depth) {
        for (const conv of this.children.get(parentId) || []) {
            const hasChildren = this.children.has(conv.id);
            const row = document.createElement('div');
            row.className = 'chat-history-item';
            row.dataset.convId = conv.id;
            row.dataset.convColor = conv.color || '';
            row.style.setProperty('--workspace-indent', `${(depth - 1) * 14}px`);
            row.tabIndex = 0;
            row.draggable = true;
            this.rows.set(conv.id, row);

            // (2026-11-24) persistent "finished & unread" glow: the row stays highlighted
            // until the user opens it (markSeen). Driven by the server flag, and live by the
            // non-expiring 'done' attention marker when the turn ended this session.
            if (conv.unread_finished || ChatSocketPool.attention[conv.id] === 'done') {
                row.classList.add('workspace-finished');
            }

            const toggle = document.createElement(hasChildren ? 'button' : 'span');
            toggle.className = 'workspace-toggle';
            row.append(toggle);

            const status = document.createElement('span');
            status.className = `chat-status-dot status-${ChatSocketPool.statuses[conv.id] || 'idle'}`;
            row.append(status);
            // (2026-11-25 audit) a finished-but-unread workspace must show the green 'done'
            // badge after a reload too, not just the glow - the DB flag drives BOTH, so the
            // post-restart row looks identical to a live background completion. A live 'dialog'
            // always wins over the persisted 'done' flag.
            const attention = ChatSocketPool.attention[conv.id] || (conv.unread_finished ? 'done' : null);
            if (attention) {
                const badge = document.createElement('span');
                badge.className = `chat-attention-badge attention-${attention}`;
                badge.title = attention === 'dialog' ? 'Waiting for your decision' : 'Task finished in the background';
                row.append(badge);
            }

            const icon = document.createElement('span');
            icon.className = 'workspace-icon';
            icon.textContent = hasChildren ? '📁' : '◻';
            icon.setAttribute('aria-hidden', 'true');
            const title = document.createElement('span');
            title.className = 'chat-title';
            title.textContent = conv.title;
            title.title = conv.title;
            const color = this._getColorInfo(conv.color);
            if (color) title.style.color = color.hex;
            row.append(icon, title);
            row.addEventListener('click', (event) => {
                if (!event.target.closest('button, input')) {
                    this.markSeen(conv.id);
                    loadConversation(conv.id);
                }
            });
            title.addEventListener('dblclick', () => this.startRename(conv.id, row));
            row.addEventListener('contextmenu', (event) => {
                if (!event.target.closest('input')) this._showMenu(event, row, conv.id, depth, hasChildren);
            });
            row.addEventListener('keydown', (event) => {
                if (event.target !== row) return;
                if (event.key === 'Enter') {
                    this.markSeen(conv.id);
                    loadConversation(conv.id);
                }
                if (event.key === 'ContextMenu' || (event.shiftKey && event.key === 'F10')) {
                    this._showMenu(event, row, conv.id, depth, hasChildren);
                }
            });
            row.addEventListener('dragstart', (event) => {
                if (event.target !== row || this.moving || row.querySelector('input')) {
                    event.preventDefault();
                    return;
                }
                this._closeMenu();
                this._closeColorPicker();
                this.dragId = conv.id;
                event.dataTransfer.setData('application/x-workspace-id', conv.id);
                event.dataTransfer.effectAllowed = 'move';
                row.classList.add('workspace-dragging');
            });
            this._bindDropTarget(row, conv.id);
            container.append(row);

            if (hasChildren) {
                const group = document.createElement('div');
                toggle.type = 'button';
                this.branches.set(conv.id, { group, toggle });
                toggle.addEventListener('click', () => {
                    if (this.collapsed.has(conv.id)) this.collapsed.delete(conv.id);
                    else this.collapsed.add(conv.id);
                    this._syncBranch(conv.id);
                });
                this._syncBranch(conv.id);
                this._renderBranch(conv.id, group, depth + 1);
                container.append(group);
            }
        }
    }

    _syncBranch(id) {
        const { group, toggle } = this.branches.get(id);
        group.hidden = this.collapsed.has(id);
        toggle.textContent = group.hidden ? '▸' : '▾';
        toggle.setAttribute('aria-expanded', String(!group.hidden));
        toggle.setAttribute('aria-label', `${group.hidden ? 'Expand' : 'Collapse'} ${this.byId.get(id).title}`);
    }

    select(id) {
        this.rows.get(this.selectedId)?.classList.remove('active');
        this.selectedId = id;
        this.rows.get(id)?.classList.add('active');
        const path = [];
        for (let node = this.byId.get(id); node; node = this.byId.get(this.parentById.get(node.id))) {
            path.unshift(node);
        }
        for (const ancestor of path.slice(0, -1)) {
            if (this.collapsed.delete(ancestor.id)) this._syncBranch(ancestor.id);
        }
        this.path.textContent = ['Root', ...path.map(node => node.title)].join(' / ');
        this.path.title = `${this.path.textContent} — drop here to move to Root`;
    }

    /**
     * Mark a workspace as seen: clear its finished-unread flag locally and on the server,
     * and drop the sidebar glow/badge. Only an explicit user action (row click / Enter) or
     * a live terminal frame for the CURRENT workspace reaches this, so the glow is never
     * cleared by background auto-restores.
     */
    markSeen(id) {
        if (!id) return;
        const conv = this.byId.get(id);
        if (conv) conv.unread_finished = 0;
        this.rows.get(id)?.classList.remove('workspace-finished');
        ChatSocketPool.clearAttention(id);
        // Fire-and-forget server clear; a failed POST never breaks the UI and the next
        // reload re-syncs from the DB flag.
        fetch(`/api/conversations/${encodeURIComponent(id)}/seen`, { method: 'POST' })
            .catch(() => { /* local clear already happened */ });
    }

    _closeMenu(restoreFocus = false) {
        if (!this.menu) return;
        this.menu.remove();
        this.menu = null;
        if (restoreFocus) this.menuRow.focus();
        this.menuRow = null;
    }

    _showMenu(event, row, id, depth, hasChildren) {
        event.preventDefault();
        this._closeMenu();
        this._closeColorPicker();
        const menu = document.createElement('div');
        menu.className = 'workspace-context-menu';
        const actions = [
            ['Create workspace', depth >= ConversationManager.MAX_DEPTH, 'Maximum depth is three levels', () => newChat(id)],
            ['Copy workspace ID', false, '', async () => {
                try {
                    await copyTextToClipboard(id);
                    showNotification('Workspace ID copied', 'success');
                } catch (e) {
                    showNotification('Failed to copy workspace ID', 'error');
                }
            }],
            ['Set workspace color', false, '', (clickEvent) => this.showColorPicker(clickEvent, id, row)],
            ['Delete', hasChildren, 'Delete or move child workspaces first', () => deleteConversation(id)],
        ];
        for (const [label, disabled, reason, action] of actions) {
            const button = document.createElement('button');
            button.type = 'button';
            button.textContent = label;
            button.disabled = disabled;
            if (disabled) button.title = reason;
            button.addEventListener('click', (clickEvent) => {
                this._closeMenu(true);
                action(clickEvent);
            });
            menu.append(button);
        }
        document.body.append(menu);
        const rect = row.getBoundingClientRect();
        menu.style.left = `${Math.max(0, Math.min(event.clientX || rect.left, window.innerWidth - menu.offsetWidth))}px`;
        menu.style.top = `${Math.max(0, Math.min(event.clientY || rect.bottom, window.innerHeight - menu.offsetHeight))}px`;
        this.menu = menu;
        this.menuRow = row;
        menu.querySelector('button:not(:disabled)')?.focus();
    }

    _canDrop(parentId) {
        if (!this.dragId || this.moving || this.byId.get(this.dragId)?.parent_id === parentId) return false;
        let depth = 0;
        for (let id = parentId; id !== null; id = this.parentById.get(id) ?? null) {
            if (id === this.dragId) return false;
            depth++;
        }
        const height = id => (this.children.get(id) || []).reduce(
            (max, child) => Math.max(max, 1 + height(child.id)), 1
        );
        return depth + height(this.dragId) <= ConversationManager.MAX_DEPTH;
    }

    _bindDropTarget(element, parentId) {
        element.addEventListener('dragover', (event) => {
            if (!this.dragId) return;
            event.stopPropagation();
            if (!this._canDrop(parentId)) {
                event.dataTransfer.dropEffect = 'none';
                return;
            }
            event.preventDefault();
            event.dataTransfer.dropEffect = 'move';
            this.dropTarget?.classList.remove('workspace-drop-target');
            this.dropTarget = element;
            element.classList.add('workspace-drop-target');
        });
        element.addEventListener('dragleave', (event) => {
            if (!element.contains(event.relatedTarget)) element.classList.remove('workspace-drop-target');
        });
        element.addEventListener('drop', (event) => {
            if (!this.dragId) return;
            event.preventDefault();
            event.stopPropagation();
            const id = this.dragId;
            const allowed = this._canDrop(parentId);
            this._endDrag();
            if (allowed) this._moveWorkspace(id, parentId);
        });
    }

    _endDrag() {
        this.rows.get(this.dragId)?.classList.remove('workspace-dragging');
        this.dropTarget?.classList.remove('workspace-drop-target');
        this.dragId = this.dropTarget = null;
    }

    async _moveWorkspace(id, parentId) {
        this.moving = true;
        try {
            const response = await fetch(`/api/conversations/${encodeURIComponent(id)}/update`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ parent_id: parentId }),
            });
            if (!response.ok) throw new Error((await response.json()).detail || `HTTP ${response.status}`);
            if (parentId !== null) this.collapsed.delete(parentId);
            await this._reloadConversations();
        } catch (e) {
            showNotification(`Failed to move workspace: ${e.message}`, 'error');
        } finally {
            this.moving = false;
        }
    }
}

ConversationManager.MAX_DEPTH = 3;
ConversationManager.DEFAULT_SORT = { by: "created", order: "desc" };


// ===== GLOBAL INSTANCE =====
// Used by workspace creation, selection, and startup.
window.ConversationMgr = new ConversationManager();
window.ConversationManager = ConversationManager;
