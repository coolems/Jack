// static/ui/conversation.js

/**
 * conversation.js - Conversation management: create, load, delete, edit & resend.
 * Uses COOLEMS shared state from app.js.
 */

let isSwitchingConversation = false;

    /**
     * (2026-09-08 multi-chat UI fix) Fetch the LIVE session state of one chat from the CLIENT
     * backend (ChatBus channel). This is what makes a chat switch remember whether that chat
     * is actively thinking/generating: the answer drives the generation indicator and the
     * send/stop button color for the chat being opened. Falls back to the socket pool's
     * frame-derived state when the endpoint is unavailable, then 'idle'.
     */
    async function getChatSessionStatus(convId) {
        if (!convId) return 'idle';
        try {
            const res = await fetch(`/api/conversations/${convId}/status`);
            if (res.ok) {
                const data = await res.json();
                if (data && typeof data.status === 'string') return data.status;
            }
        } catch (e) { /* fall through to pool state */ }
        if (typeof ChatSocketPool !== 'undefined' && ChatSocketPool.statuses[convId]) {
            return ChatSocketPool.statuses[convId];
        }
        return 'idle';
    }

    /** Baseline = assistant bubbles that belong to the DB history, not to a live turn. */
    function _countAssistantBubbles() {
        const c = document.getElementById('chatContainer');
        return c ? c.querySelectorAll('.message.assistant').length : 0;
    }



async function newChat() {
    if (isSwitchingConversation) {
        showNotification('Please wait, switching workspace...', 'warning');
        return;
    }

    try {
        const response = await fetch('/api/conversations', { 
            method: 'POST'
        });
        const data = await response.json();

        const oldConvId = COOLEMS.currentConversation;
        COOLEMS.currentConversation = data.id;

        // (2026-09-08 multi-chat UI fix) a brand-new chat is NEVER generating: reset the
        // per-chat generation state so the button shows blue (send) for it, even if the
        // previous chat was still thinking in the background.
        COOLEMS.isStreaming = false;
        COOLEMS.streamingBuffer = '';
        setButtonState(false);
        COOLEMS._inReplay = false;
        COOLEMS._baselineAssistant = 0;

        const container = document.getElementById('chatContainer');
        const welcome = document.getElementById('welcome');
        if (welcome) welcome.style.display = 'none';
        container.innerHTML = '<div class="welcome" style="display: none;"></div>';

        await loadConversations();

        // (2026-09-08 multi-chat) open the NEW chat's own socket. The previous chat's
        // socket is left untouched — it keeps streaming/generating in the background.
        ChatSocketPool.ensure(COOLEMS.currentConversation);

        showNotification('New workspace created', 'success');
    } catch (e) {
        showNotification('Failed to create new workspace', 'error');
        console.error('New workspace error:', e);
    }
}

async function deleteConversation(id) {
    if (isSwitchingConversation) {
        showNotification('Please wait, operation in progress...', 'warning');
        return;
    }

    try {
        const response = await fetch(`/api/conversations/${id}`, { method: 'DELETE' });
        if (response.ok) {
            const wasCurrent = (id === COOLEMS.currentConversation);

            if (wasCurrent) {
                COOLEMS.currentConversation = null;
                // (2026-09-08 multi-chat UI fix) no chat selected -> nothing can be generating.
                COOLEMS.isStreaming = false;
                setButtonState(false);
                COOLEMS._inReplay = false;
                COOLEMS._baselineAssistant = 0;
                // (2026-09-08 multi-chat) stop this chat's session first (if generating),
                // then close ONLY its socket. Other chats' sockets are never touched.
                fetch(`/api/stop/${id}`, { method: 'POST' }).catch(() => {});
                ChatSocketPool.close(id, true);
                const container = document.getElementById('chatContainer');
                container.innerHTML = `<div class="welcome" id="welcome"><h1>COOLEMS</h1><p>Your workspace</p></div>`;
            }
            await loadConversations();
            showNotification('Workspace deleted', 'success');
        } else {
            throw new Error();
        }
    } catch (e) {
        showNotification('Failed to delete workspace', 'error');
        console.error('Delete error:', e);
    }
}

async function loadConversation(id) {
    // Prevent switching while already switching
    if (isSwitchingConversation) {
        showNotification('Please wait, already switching workspace...', 'warning');
        return;
    }

    // CRITICAL FIX: If switching to the same conversation, do nothing
    if (id === COOLEMS.currentConversation) {
        log('Already in conversation:', id);
        return;
    }

    // Set switching flag
    isSwitchingConversation = true;

    const newConvId = id;
    const oldConvId = COOLEMS.currentConversation;
    // (2026-09-08 multi-chat) the previous chat's socket is NOT closed here: its session
    // keeps running in the background and this chat's own socket is opened below.
    // Update current conversation BEFORE loading (legacy contract kept for the pool alias)
    COOLEMS.currentConversation = newConvId;

    try {
        // Fetch conversation messages
        const response = await fetch(`/api/conversations/${newConvId}/messages`);

        if (!response.ok) {
            throw new Error(`HTTP ${response.status}: ${response.statusText}`);
        }

        const data = await response.json();

        // Per-chat working root (2026-08-29): restore the folder this chat was used with.
        // Legacy chats have working_root=null -> keep whatever is currently active (no-op).
        if (data.working_root && typeof currentWorkingRoot !== 'undefined'
                && data.working_root !== currentWorkingRoot) {
            try {
                const wrRes = await fetch('/api/working_root', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ working_root: data.working_root })
                });
                const wrData = await wrRes.json();
                if (wrData.success) {
                    currentWorkingRoot = wrData.working_root;
                    // 2026-08-29: working folder now lives in the header chip - refresh its display.
                    if (typeof updateWorkingRootChip === 'function') updateWorkingRootChip();
                    showNotification('Working folder restored for this workspace: ' + currentWorkingRoot, 'success');
                    // Refresh file tree to the restored folder (existing helpers)
                    expandedFolders.clear();
                    selectedItems.clear();
                    currentTreePath = '';
                    setTimeout(() => loadTree(''), 100);
                } else {
                    // Saved folder no longer exists / not allowed -> keep current working root
                    showNotification('Workspace folder unavailable (' + (wrData.message || 'error') + ') - keeping current working folder', 'warning');
                }
            } catch (e) {
                console.warn('Failed to restore workspace working root:', e);
            }
        }

        // COMPLETELY REBUILD UI from fresh data
        const container = document.getElementById('chatContainer');
        const welcome = document.getElementById('welcome');
        if (welcome) welcome.style.display = 'none';

        // Clear container
        container.innerHTML = '';

        // Re-add welcome element (hidden)
        if (!document.getElementById('welcome')) {
            const newWelcome = document.createElement('div');
            newWelcome.id = 'welcome';
            newWelcome.className = 'welcome';
            newWelcome.style.display = 'none';
            newWelcome.innerHTML = '<h1>COOLEMS</h1><p>AI Agent Interface. Powered by local LLM models.</p>';
            container.appendChild(newWelcome);
        }

        // Append all messages from server data
        for (const msg of data.messages) {
            // SECURITY fix 2026-10-05: appendMessage is async (media tokens are minted via header auth).
            await appendMessage(msg.role, msg.content, false, msg.media_urls, msg.file_contents);
        }

        log('Loaded', data.messages.length, 'messages for conversation', newConvId);

        // Refresh conversation list in sidebar
        await loadConversations();

        // (2026-09-08 multi-chat UI fix) RESTORE THIS CHAT'S GENERATION STATE instead of a
        // blind reset: if this chat is actively thinking/generating (or queued), the send
        // button must show its red STOP state and the generation indicator must be visible —
        // switching chats used to lose exactly that information. The baseline count tells
        // the replay guard which assistant bubbles already come from the DB history, so a
        // re-attached turn streams into ONE fresh bubble instead of duplicating it.
        COOLEMS.streamingBuffer = '';
        const _liveStatus = await getChatSessionStatus(newConvId);
        if (_liveStatus === 'generating' || _liveStatus === 'queued') {
            COOLEMS.isStreaming = true;
            setButtonState(true);                       // red stop button: this chat is busy
            if (typeof ChatSocketPool !== 'undefined') {
                ChatSocketPool.updateChatStatus(newConvId, _liveStatus);
            }
        } else {
            COOLEMS.isStreaming = false;
            setButtonState(false);                      // blue send button: free to prompt
            if (typeof ChatSocketPool !== 'undefined') {
                ChatSocketPool.updateChatStatus(newConvId, 'idle');
            }
        }
        COOLEMS._baselineAssistant = _countAssistantBubbles();

        // (2026-09-08 multi-chat) open this chat's own socket immediately — the server
        // replays any buffered background frames on connect, so nothing is lost.
        const _switchWs = ChatSocketPool.ensure(newConvId);
        // Replay mode only applies when THIS switch opens a fresh handshake: an already-OPEN
        // pooled socket delivers no replay (and therefore no replay_end marker), so the guard
        // must stay off or it would swallow this chat's live frames.
        COOLEMS._inReplay = !!(_switchWs && _switchWs.readyState === WebSocket.CONNECTING);
        isSwitchingConversation = false;

        log('Successfully loaded conversation:', newConvId);

    } catch (e) {
        logError('Failed to load conversation:', e);
        showNotification('Failed to load workspace: ' + (e.message || 'Unknown error'), 'error');

        // Revert on error - restore old conversation
        COOLEMS.currentConversation = oldConvId;

        // Reconnect to the old conversation's socket (it was never closed — ensure() reuses it)
        if (oldConvId) {
            ChatSocketPool.ensure(oldConvId);
        }

        isSwitchingConversation = false;
    }
}

async function loadConversations() {
    try {
        const mgr = window.ConversationMgr;
        const sortBy = mgr ? mgr.sortConfig.by : 'created';
        const sortOrder = mgr ? mgr.sortConfig.order : 'desc';
        const response = await fetch(`/api/conversations?sort_by=${sortBy}&sort_order=${sortOrder}`);

        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }

        const data = await response.json();

        // Update sidebar
        const historyEl = document.getElementById('chatHistory');
        if (!historyEl) return data;

        // Use ConversationManager for rendering (supports color, rename, sort)
        if (window.ConversationMgr) {
            if (data.sort_by) window.ConversationMgr.sortConfig.by = data.sort_by;
            if (data.sort_order) window.ConversationMgr.sortConfig.order = data.sort_order;
            window.ConversationMgr.renderConversations(data.conversations);
        } else {
            // Fallback: legacy inline rendering
            if (data.conversations.length === 0) {
                historyEl.innerHTML = '<div style="padding: 20px; text-align: center; color: var(--text-muted); font-size: 13px;">No workspaces yet<br><br>Click "New Workspace" to start</div>';
            } else {
                historyEl.innerHTML = data.conversations.map(conv => `
                    <div class="chat-history-item ${conv.id === COOLEMS.currentConversation ? 'active' : ''}"
                         onclick="loadConversation('${conv.id}')">
                        <svg class="chat-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path>
                        </svg>
                        ${typeof ChatSocketPool !== 'undefined'
                            ? `<span class="chat-status-dot status-${ChatSocketPool.statuses[conv.id] || 'idle'}"></span>`
                            : ''}
                        <span style="flex:1;overflow:hidden;text-overflow:ellipsis;">${escapeHtml(conv.title)}</span>
                        <button class="chat-delete-btn" onclick="event.stopPropagation();deleteConversation('${conv.id}')">
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <polyline points="3 6 5 6 21 6"></polyline>
                                <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
                            </svg>
                        </button>
                    </div>
                `).join('');
            }
        }

        // Update welcome message
        const container = document.getElementById('chatContainer');
        const welcome = document.getElementById('welcome');

        if (data.conversations.length === 0) {
            if (welcome) {
                welcome.style.display = 'block';
            } else if (container && container.children.length === 0) {
                container.innerHTML = `<div class="welcome" id="welcome">
                    <h1>COOLEMS</h1>
                    <p>Start a new workspace to begin</p>
                </div>`;
            }
        } else if (welcome) {
            welcome.style.display = 'none';
        }

        return data;
    } catch (e) {
        logError('Failed to load conversations:', e);
        showNotification('Failed to load workspaces', 'error');
        return null;
    }
}

/**
 * EDIT AND RESEND - COMPLETELY FIXED
 */
async function editAndResend(messageIndex, newContent) {
    if (!COOLEMS.currentConversation) {
        showNotification('No workspace selected', 'error');
        return;
    }

    if (COOLEMS.isStreaming) {
        showNotification('Please wait for current response to finish before editing', 'warning');
        return;
    }

    if (!newContent || newContent.trim() === '') {
        showNotification('Message cannot be empty', 'warning');
        return;
    }

    if (isSwitchingConversation) {
        showNotification('Please wait, workspace is loading...', 'warning');
        return;
    }

    log('[EDIT] Starting edit at index:', messageIndex, 'new content:', newContent.substring(0, 50));

    // Clear any UI state from previous streaming
    COOLEMS.streamingBuffer = '';
    COOLEMS.userScrolledUp = false;
    COOLEMS.autoScrollEnabled = true;
    setButtonState(false);

    try {
        // STEP 1: Truncate conversation on server
        const truncateResponse = await fetch(
            `/api/conversations/${COOLEMS.currentConversation}/messages/truncate`,
            {
                method: 'DELETE',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ message_index: messageIndex })
            }
        );

        if (!truncateResponse.ok) {
            const errorText = await truncateResponse.text();
            throw new Error(`Server truncate failed: ${truncateResponse.status} ${errorText}`);
        }

        const truncateData = await truncateResponse.json();
        log('[EDIT] Truncated:', truncateData.deleted_count, 'messages deleted,', 
            truncateData.remaining_count, 'messages remain');

        // STEP 2: The socket STAYS OPEN (multi-chat pool) — no close/reconnect dance needed.
        // Any in-flight session for this chat is stopped so the rebuild starts clean.
        fetch(`/api/stop/${COOLEMS.currentConversation}`, { method: 'POST' }).catch(() => {});

        // STEP 3: Reload entire conversation from server
        log('[EDIT] Reloading conversation from server...');

        const response = await fetch(`/api/conversations/${COOLEMS.currentConversation}/messages`);
        if (!response.ok) {
            throw new Error(`Failed to load workspace: ${response.status}`);
        }
        const data = await response.json();

        // STEP 4: COMPLETELY REBUILD UI from fresh data
        const container = document.getElementById('chatContainer');
        const welcome = document.getElementById('welcome');
        if (welcome) welcome.style.display = 'none';

        container.innerHTML = '';

        if (!document.getElementById('welcome')) {
            const newWelcome = document.createElement('div');
            newWelcome.id = 'welcome';
            newWelcome.className = 'welcome';
            newWelcome.style.display = 'none';
            newWelcome.innerHTML = '<h1>COOLEMS</h1><p>AI Agent Interface. Powered by local LLM models.</p>';
            container.appendChild(newWelcome);
        }

        for (const msg of data.messages) {
            // SECURITY fix 2026-10-05: appendMessage is async (media tokens are minted via header auth).
            await appendMessage(msg.role, msg.content, false, msg.media_urls, msg.file_contents);
        }

        log('[EDIT] UI rebuilt with', data.messages.length, 'messages');

        // (2026-09-08 multi-chat UI fix) the rebuild is DB-authoritative: leave replay mode
        // and reset the baseline so the incoming live stream renders into one fresh bubble.
        COOLEMS._inReplay = false;
        COOLEMS._baselineAssistant = _countAssistantBubbles();

        // STEP 5: Append the edited user message as a NEW message
        await appendMessage('user', newContent, false);
        log('[EDIT] Appended edited message to UI');

        // STEP 6: Make sure this chat's pooled socket is open (it was never closed — ensure() reuses it)
        ChatSocketPool.ensure(COOLEMS.currentConversation);
        const _editWs = () => {
            const e = ChatSocketPool.sockets[COOLEMS.currentConversation];
            return e ? e.ws : null;
        };

        let retries = 0;
        const maxRetries = 20;

        while ((!_editWs() || _editWs().readyState !== WebSocket.OPEN) && retries < maxRetries) {
            log('[EDIT] WebSocket not open, waiting...', retries + 1, '/', maxRetries);
            await new Promise(resolve => setTimeout(resolve, 100));
            retries++;
        }

        if (!_editWs() || _editWs().readyState !== WebSocket.OPEN) {
            throw new Error('WebSocket not connected after waiting');
        }

        // STEP 7: Send the edited message to AI over this chat's own socket
        COOLEMS.isStreaming = true;
        COOLEMS.streamingBuffer = '';
        setButtonState(true);

        const modelSelector = document.getElementById('modelSelector');

        const payload = {
            message: newContent,
            model: modelSelector ? modelSelector.value : null,
            agent_mode: COOLEMS.agentMode,
            media_files: [],
            enable_thinking: COOLEMS.enableThinking
        };

        _editWs().send(JSON.stringify(payload));
        log('[EDIT] Message sent to AI');
        showNotification('Message edited. AI responding from this point.', 'success');

        } catch (error) {
            console.error('[EDIT] Failed:', error);
            showNotification('Failed to edit message: ' + error.message, 'error');
            COOLEMS.isStreaming = false;
            setButtonState(false);

            log('[EDIT] Attempting to recover by reloading conversation');
            try {
                // (2026-09-08 multi-chat) the socket stays open — just reload the view.
                await loadConversation(COOLEMS.currentConversation);
            } catch (reloadError) {
                console.error('[EDIT] Recovery failed:', reloadError);
                showNotification('Please refresh the page to continue', 'error');
            }
        }
    }

// Helper function to check if a conversation switch is in progress
window.isConversationSwitching = function() {
    return isSwitchingConversation;
};

// Helper function to get current conversation ID
window.getCurrentConversationId = function() {
    return COOLEMS.currentConversation;
};

// Export for global access
window.getChatSessionStatus = getChatSessionStatus;
window._countAssistantBubbles = _countAssistantBubbles;
window.editAndResend = editAndResend;
window.loadConversation = loadConversation;
window.loadConversations = loadConversations;
window.deleteConversation = deleteConversation;
window.newChat = newChat;
