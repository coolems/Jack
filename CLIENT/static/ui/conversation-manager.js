// static/ui/conversation-manager.js

/**
 * ConversationManager - Enhanced sidebar conversation management.
 *
 * Provides:
 *   1. Color assignment (8-color palette) via expandable picker on right-click
 *   2. Inline rename via double-click on conversation title
 *   3. Sort controls: sort by creation time / color / name, ascending or descending
 *
 * Architecture:
 *   - Pure class, no global state pollution
 *   - Communicates with backend via fetch() to /api/conversations/*
 *   - Renders conversation items in #chatHistory
 *   - Sort controls rendered in sidebar-header (injected on init)
 *
 * Color Palette (8 colors):
 *   red, orange, yellow, green, blue, purple, pink, gray
 *
 * Usage:
 *   const manager = new ConversationManager();
 *   manager.init();
 *
 * Dependencies:
 *   - COOLEMS (from app.js)
 *   - loadConversation(), deleteConversation() (from conversation.js)
 */

class ConversationManager {



    /**
     * Create a ConversationManager instance.
     */
    constructor() {
        /** @type {{ by: string, order: string }} Current sort state */
        this.sortConfig = { ...ConversationManager.DEFAULT_SORT };

        /** @type {HTMLDivElement|null} Reference to #chatHistory */
        this.container = null;

        /** @type {HTMLDivElement|null} Reference to sort controls container */
        this.sortControls = null;

        /** @type {HTMLElement|null} Active color picker popup */
        this.activePicker = null;

        /** @type {string|null} Conversation ID currently being renamed */
        this.renamingId = null;
    }

    // ===== INITIALIZATION =====

    /**
     * Initialize the ConversationManager.
     * Injects sort controls into the sidebar header and binds events.
     */
    init() {
        this.container = document.getElementById('chatHistory');
        this._injectSortControls();
        this._bindSortEvents();
        log('[ConversationManager] Initialized');
    }

    /**
     * Inject sort controls into the sidebar header.
     * Creates a small toolbar with:
     *   - Sort-by dropdown (created / color / name)
     *   - Asc/Desc toggle button
     */
    _injectSortControls() {
        const sidebarHeader = document.querySelector('.sidebar-header');
        if (!sidebarHeader) {
            logError('[ConversationManager] sidebar-header not found');
            return;
        }

        const sortBar = document.createElement('div');
        sortBar.className = 'conversation-sort-bar';
        sortBar.innerHTML = `
            <select class="sort-select" id="convSortBy" title="Sort workspaces by">
                <option value="created">📅 Creation Time</option>
                <option value="color">🎨 Color</option>
                <option value="name">🔤 Name</option>
            </select>
            <button class="sort-order-btn" id="convSortOrder" title="Toggle ascending/descending">
                ↓ Desc
            </button>
        `;

        sidebarHeader.appendChild(sortBar);
        this.sortControls = sortBar;

        // Set initial values
        const sortBy = document.getElementById('convSortBy');
        const sortOrder = document.getElementById('convSortOrder');
        if (sortBy) sortBy.value = this.sortConfig.by;
        if (sortOrder) this._updateOrderButton(this.sortConfig.order);
    }

    /**
     * Bind sort control change events.
     */
    _bindSortEvents() {
        const sortBy = document.getElementById('convSortBy');
        const sortOrder = document.getElementById('convSortOrder');

        if (sortBy) {
            sortBy.addEventListener('change', () => {
                this.sortConfig.by = sortBy.value;
                this._reloadConversations();
            });
        }

        if (sortOrder) {
            sortOrder.addEventListener('click', () => {
                this.sortConfig.order = this.sortConfig.order === 'desc' ? 'asc' : 'desc';
                this._updateOrderButton(this.sortConfig.order);
                this._reloadConversations();
            });
        }
    }

    /**
     * Update the sort order button text/icon.
     * @param {'asc'|'desc'} order
     */
    _updateOrderButton(order) {
        const btn = document.getElementById('convSortOrder');
        if (!btn) return;
        if (order === 'asc') {
            btn.textContent = '↑ Asc';
        } else {
            btn.textContent = '↓ Desc';
        }
    }

    // ===== LOADING & RENDERING =====

    /**
     * Reload conversations from server with current sort config.
     * Delegates rendering to renderConversations().
     */
    async _reloadConversations() {


        const url = `/api/conversations?sort_by=${this.sortConfig.by}&sort_order=${this.sortConfig.order}`;

        try {
            const response = await fetch(url);
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            const data = await response.json();
            this.renderConversations(data.conversations);
        } catch (e) {
            logError('[ConversationManager] Failed to reload:', e);
        }
    }

    /**
     * Render conversation items in the sidebar.
     *
     * @param {Array} conversations - Array of conversation objects from API
     */
    renderConversations(conversations) {
        if (!this.container) return;

        if (conversations.length === 0) {
            this.container.innerHTML = '<div style="padding: 20px; text-align: center; color: var(--text-muted); font-size: 13px;">No workspaces yet<br><br>Click "New Workspace" to start</div>';
            return;
        }

        this.container.innerHTML = conversations.map(conv => {
            const isActive = conv.id === COOLEMS.currentConversation;
            const colorInfo = this._getColorInfo(conv.color);
            const colorDot = colorInfo
                ? `<span class="chat-color-dot" style="background:${colorInfo.hex};" title="${colorInfo.label}"></span>`
                : `<span class="chat-color-dot chat-color-dot-none" title="No color"></span>`;

            return `
                <div class="chat-history-item ${isActive ? 'active' : ''}"
                     data-conv-id="${conv.id}"
                     data-conv-color="${conv.color || ''}"
                     onclick="loadConversation('${conv.id}')"
                     ondblclick="ConversationMgr.startRename('${conv.id}', this)"
                     oncontextmenu="ConversationMgr.showColorPicker(event, '${conv.id}', this); return false;">
                    ${colorDot}
                    ${typeof ChatSocketPool !== 'undefined'
                        ? `<span class="chat-status-dot status-${ChatSocketPool.statuses[conv.id] || 'idle'}"></span>`
                        : ''}
                    <svg class="chat-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path>
                    </svg>
                    <span class="chat-title" style="flex:1;overflow:hidden;text-overflow:ellipsis;">${this._escapeHtml(conv.title)}</span>
                    ${conv.agent_mode ? '<span style="color:var(--accent);font-size:10px;">AGENT</span>' : ''}
                    <button class="chat-delete-btn" onclick="event.stopPropagation();deleteConversation('${conv.id}')">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <polyline points="3 6 5 6 21 6"></polyline>
                            <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
                        </svg>
                    </button>
                </div>
            `;
        }).join('');
    }

    /**
     * Show the color picker popup for a conversation item.
     * Right-click context menu with 8 color swatches + "No Color" option.
     *
     * @param {MouseEvent} event
     * @param {string} convId
     * @param {HTMLElement} itemEl
     */
    showColorPicker(event, convId, itemEl) {
        event.preventDefault();
        event.stopPropagation();

        // Close any existing picker
        this._closeColorPicker();

        const picker = document.createElement('div');
        picker.className = 'color-picker-popup';

        const currentColor = itemEl.getAttribute('data-conv-color') || '';

        // "No Color" option
        const noColorBtn = document.createElement('button');
        noColorBtn.className = `color-swatch ${currentColor === '' ? 'selected' : ''}`;
        noColorBtn.style.background = 'var(--bg-tertiary)';
        noColorBtn.style.border = '1px solid var(--border)';
        noColorBtn.title = 'No Color';
        noColorBtn.innerHTML = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="var(--text-muted)" stroke-width="3"><line x1="6" y1="6" x2="18" y2="18"></line></svg>`;
        noColorBtn.addEventListener('click', () => {
            this._setConversationColor(convId, null);
            this._closeColorPicker();
        });
        picker.appendChild(noColorBtn);

        // 8 color swatches
        for (const color of ConversationManager.COLORS) {
            const swatch = document.createElement('button');
            swatch.className = `color-swatch ${currentColor === color.name ? 'selected' : ''}`;
            swatch.style.background = color.hex;
            swatch.title = color.label;
            if (currentColor === color.name) {
                swatch.innerHTML = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="3"><polyline points="20 6 9 17 4 12"></polyline></svg>`;
            }
            swatch.addEventListener('click', () => {
                this._setConversationColor(convId, color.name);
                this._closeColorPicker();
            });
            picker.appendChild(swatch);
        }

        picker.appendChild(document.createTextNode(''));

        // Position near the item
        const rect = itemEl.getBoundingClientRect();
        picker.style.position = 'fixed';
        picker.style.left = `${rect.right + 4}px`;
        picker.style.top = `${rect.top}px`;
        picker.style.zIndex = '9999';

        document.body.appendChild(picker);
        this.activePicker = picker;

        // Close on outside click
        const closeHandler = (e) => {
            if (!picker.contains(e.target)) {
                this._closeColorPicker();
                document.removeEventListener('click', closeHandler);
            }
        };
        setTimeout(() => document.addEventListener('click', closeHandler), 0);
    }

    /**
     * Close the active color picker popup.
     */
    _closeColorPicker() {
        if (this.activePicker) {
            this.activePicker.remove();
            this.activePicker = null;
        }
    }

    /**
     * Set a conversation's color via API.
     * @param {string} convId
     * @param {string|null} colorName
     */
    async _setConversationColor(convId, colorName) {
        try {
            const response = await fetch(`/api/conversations/${convId}/update`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ color: colorName }),
            });
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            // Reload to refresh list
            this._reloadConversations();
        } catch (e) {
            logError('[ConversationManager] Failed to set color:', e);
            showNotification('Failed to set color', 'error');
        }
    }

    /**
     * Start inline rename for a conversation item.
     * Double-click triggers an input field overlay.
     *
     * @param {string} convId
     * @param {HTMLElement} itemEl
     */
    startRename(convId, itemEl) {
        const titleSpan = itemEl.querySelector('.chat-title');
        if (!titleSpan) return;

        const currentTitle = titleSpan.textContent;
        const input = document.createElement('input');
        input.type = 'text';
        input.value = currentTitle;
        input.className = 'chat-rename-input';
        input.style.cssText = `
            flex: 1; overflow: hidden; background: var(--bg-tertiary);
            border: 1px solid var(--accent); border-radius: 4px;
            color: var(--text-primary); font-size: 14px; padding: 2px 4px;
            outline: none; width: 120px;
        `;

        titleSpan.replaceWith(input);
        input.focus();
        input.select();

        const finishRename = async () => {
            const newTitle = input.value.trim() || 'New Workspace';
            if (newTitle !== currentTitle) {
                try {
                    await fetch(`/api/conversations/${convId}/update`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ title: newTitle }),
                    });
                } catch (e) {
                    logError('[ConversationManager] Failed to rename:', e);
                }
            }
            this._reloadConversations();
        };

        input.addEventListener('blur', finishRename);
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') input.blur();
            if (e.key === 'Escape') {
                input.value = currentTitle;
                input.blur();
            }
        });
    }

    // ===== HELPERS =====

    /**
     * Get color info object by name.
     * @param {string|null} colorName
     * @returns {{name: string, hex: string, label: string}|null}
     */
    _getColorInfo(colorName) {
        if (!colorName) return null;
        return ConversationManager.COLORS.find(c => c.name === colorName) || null;
    }

    /**
     * Escape HTML for safe rendering.
     * @param {string} text
     * @returns {string}
     */
    _escapeHtml(text) {
        if (typeof escapeHtml === 'function') return escapeHtml(text);
        const d = document.createElement('div');
        d.textContent = text;
        return d.innerHTML;
    }
}

// Predefined 8-color palette with CSS variables and hex values.
// (ES6-safe: assigned on the class object instead of ES2022 static class fields, so this
// file parses as a classic script in every browser target.) Each entry has:
//   - name:   key used in DB and API
//   - hex:    hex color for rendering
//   - label:  human-readable label
ConversationManager.COLORS = [
    { name: "red",    hex: "#ef4444", label: "Red" },
    { name: "orange", hex: "#f97316", label: "Orange" },
    { name: "yellow", hex: "#eab308", label: "Yellow" },
    { name: "green",  hex: "#22c55e", label: "Green" },
    { name: "blue",   hex: "#3b82f6", label: "Blue" },
    { name: "purple", hex: "#a855f7", label: "Purple" },
    { name: "pink",   hex: "#ec4899", label: "Pink" },
    { name: "gray",   hex: "#6b7280", label: "Gray" }
];

// Sort configuration defaults.
ConversationManager.DEFAULT_SORT = { by: "created", order: "desc" };


// ===== GLOBAL INSTANCE =====
// Exposed as window.ConversationMgr for inline event handlers in rendered HTML.
window.ConversationMgr = new ConversationManager();
window.ConversationManager = ConversationManager;
