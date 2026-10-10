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

    let oldConvId = null, data = null;
    try {
        isSwitchingConversation = true;      // (2026-10-09 audit fix) was missing - a second click raced the stash/swap
        const response = await fetch('/api/conversations', { 
            method: 'POST'
        });
        data = await response.json();

        oldConvId = COOLEMS.currentConversation;
        // (2026-10-09 per-workspace views) park the outgoing workspace's live view — it keeps
        // running in the background and must come back exactly as it was.
        if (oldConvId) WorkspaceViews.stash(oldConvId);

        // (2026-10-09 switch-race fix) suspend the outgoing view's frame routing while its
        // container is being replaced by the new workspace's - cleared once the swap completes.
        if (oldConvId) WorkspaceViews.suspend(oldConvId);
        // The NEW workspace's frames must queue until its view is current: between the flip below and
        // the container swap any await window would render them into the OUTGOING DOM.
        WorkspaceViews.suspend(data.id);
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

        // (2026-10-09 switch-race fix) outgoing workspace is now a background chat - resume its routing.
        if (oldConvId) WorkspaceViews.resume(oldConvId);
        WorkspaceViews.resume(data.id);

        // (2026-10-09 per-workspace views) activate the NEW workspace's own folder from its DB row
        // (project-root fallback until the user sets one): chip, file tree and tools all agree
        // from first open - instead of silently keeping the previous workspace's path.
        await _activateWorkingRoot('', data.id);

        showNotification('New workspace created', 'success');
    } catch (e) {
        // (2026-10-09 switch-race fix) creation failed after the outgoing view was stashed:
        // put it back so the user is not left on a blank, suspended workspace.
        if (oldConvId && WorkspaceViews.hasView(oldConvId)) {
            COOLEMS.currentConversation = oldConvId;
            WorkspaceViews.restore(oldConvId);
            ChatSocketPool.ensure(oldConvId);
        }
        if (oldConvId) WorkspaceViews.resume(oldConvId);
        if (data && data.id) WorkspaceViews.resume(data.id);
        showNotification('Failed to create new workspace', 'error');
        console.error('New workspace error:', e);
    } finally {
        isSwitchingConversation = false;   // (2026-10-09 audit fix) always release the switch lock
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

            // (2026-10-09 per-workspace views) the workspace is gone: drop its stashed view,
            // queued frames and attention badge in ALL cases — a background socket for a
            // deleted conversation would otherwise keep queueing frames into nothing.
            WorkspaceViews.drop(id, true);
            ChatSocketPool.clearAttention(id);
            // A deleted workspace must not keep generating (burning tokens into a deleted row):
            // stop its session in ALL cases — the current one is stopped below as well.
            if (!wasCurrent) {
                fetch(`/api/stop/${id}`, { method: 'POST' }).catch(() => {});
                ChatSocketPool.close(id, true);
            }

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

/**
 * (2026-10-09 per-workspace views) Activate THIS workspace's own folder from the database.
 * Shared by both switch paths: stored value -> activate; null / fresh workspace -> empty path
 * + conversation_id, and the server resolves this workspace's row or pins it to the project
 * root fallback (each workspace owns its row from first open - no shared global file).
 */
async function _activateWorkingRoot(path, convId) {
    if (typeof currentWorkingRoot === 'undefined') return;
    try {
        const wrRes = await fetch('/api/working_root', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ working_root: path || '', conversation_id: convId })
        });
        const wrData = await wrRes.json();
        if (wrData.success && wrData.working_root !== currentWorkingRoot) {
            currentWorkingRoot = wrData.working_root;
            // 2026-08-29: working folder now lives in the header chip - refresh its display.
            if (typeof updateWorkingRootChip === 'function') updateWorkingRootChip();
            showNotification('Working folder for this workspace: ' + currentWorkingRoot, 'success');
            // Refresh file tree to the restored folder (existing helpers)
            expandedFolders.clear();
            selectedItems.clear();
            currentTreePath = '';
            setTimeout(() => loadTree(''), 100);
        } else if (!wrData.success) {
            showNotification('Workspace folder unavailable (' + (wrData.message || 'error') + ') - keeping current working folder', 'warning');
        }
    } catch (e) {
        console.warn('Failed to restore workspace working root:', e);
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

    isSwitchingConversation = true;

    const newConvId = id;
    const oldConvId = COOLEMS.currentConversation;

    // (2026-10-09 per-workspace views) Park the OUTGOING workspace's live DOM + streaming
    // state BEFORE anything else: approval cards, in-flight bubbles and running countdowns
    // stay attached to the detached node. currentConversation deliberately stays on the OLD
    // view until its container is swapped below — incoming-chat frames keep queueing instead
    // of rendering into the outgoing DOM (the old code had exactly that race during the DB fetch).
    if (oldConvId) WorkspaceViews.stash(oldConvId);

    // (2026-10-09 switch-race fix) suspend its frame routing: it is still COOLEMS.currentConversation
    // until the swap below completes - without this, its live frames would render into the container
    // being rebuilt for the incoming workspace. Cleared at the end of BOTH paths (and on revert).
    if (oldConvId) WorkspaceViews.suspend(oldConvId);

    try {
        const _liveStatus = await getChatSessionStatus(newConvId);
        const _stash = WorkspaceViews.peekState(newConvId);
        // FAST PATH: a stashed view exists and is safe to reattach. A mid-stream stash may only
        // be reused while the turn is still live; if it finished while away, the final answer is
        // in the DB -> slow path shows the complete bubble instead of a truncated one.
        const _fastOk = !!_stash && (!_stash.midStream || _liveStatus === 'generating' || _liveStatus === 'queued');

        if (_fastOk) {
            COOLEMS.currentConversation = newConvId;      // flip right before the swap
            ChatSocketPool.clearAttention(newConvId);     // user is looking at it now — no badge

            WorkspaceViews.restore(newConvId);            // reattach the REAL node + streaming state
            COOLEMS.isStreaming = (_liveStatus === 'generating' || _liveStatus === 'queued');
            setButtonState(COOLEMS.isStreaming);
            ChatSocketPool.updateChatStatus(newConvId, _liveStatus);

            const welcome = document.getElementById('welcome');
            if (welcome) welcome.style.display = 'none';

            // (2026-10-09 switch-race fix) guard window: between the flip above and the flush below,
            // any frame for THIS chat must be held by the replay guard instead of rendering ahead
            // of the queued frames. ensure() may open a fresh handshake (server replay follows);
            // an already-open pooled socket delivers no replay, so the guard turns off before flush.
            COOLEMS._inReplay = true;
            const _fastWs = ChatSocketPool.ensure(newConvId);   // LRU-evicted idle socket may be gone; alias COOLEMS.ws to THIS chat
            if (!(_fastWs && _fastWs.readyState === WebSocket.CONNECTING)) {
                COOLEMS._inReplay = false;                      // no fresh handshake -> no replay will come
            }

            // Frames captured while away (approval cards, stream chunks) continue the view —
            // the stashed DOM was NOT rebuilt from DB, so there is nothing to duplicate.
            WorkspaceViews.flushFrames(newConvId);

            await _activateWorkingRoot(_stash.workingRoot || '', newConvId);
            await loadConversations();                    // sidebar: active class + badges
            // (2026-10-09 switch-race fix) outgoing workspace may go back to background - resume its routing.
            if (oldConvId) WorkspaceViews.resume(oldConvId);

            isSwitchingConversation = false;
            log('Fast-restored workspace view:', newConvId);
            return;
        }

        // SLOW PATH: first open / page reload / evicted (or stale mid-stream) stash ->
        // rebuild from the DB. A leftover stash would shadow this fresh DOM on the next switch.
        WorkspaceViews.drop(newConvId, false);

        const response = await fetch(`/api/conversations/${newConvId}/messages`);
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}: ${response.statusText}`);
        }
        const data = await response.json();

        // Queued frames while away: a LIVE turn's chunks are the continuation of the rebuilt
        // view (flushed below, after state setup). A FINISHED turn's answer is in the DB now —
        // its queued content would duplicate it, so drop those instead.
        if (!(_liveStatus === 'generating' || _liveStatus === 'queued')) {
            WorkspaceViews.dropFrames(newConvId);
        }

        await _activateWorkingRoot(data.working_root || '', newConvId);

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

        // RESTORE THIS CHAT'S GENERATION STATE (2026-09-08 multi-chat UI fix): if this chat is
        // actively thinking/generating (or queued), the send button shows its red STOP state.
        COOLEMS.streamingBuffer = '';
        if (_liveStatus === 'generating' || _liveStatus === 'queued') {
            COOLEMS.isStreaming = true;
            setButtonState(true);                       // red stop button: this chat is busy
            ChatSocketPool.updateChatStatus(newConvId, _liveStatus);
        } else {
            COOLEMS.isStreaming = false;
            setButtonState(false);                      // blue send button: free to prompt
            ChatSocketPool.updateChatStatus(newConvId, 'idle');
        }
        COOLEMS._baselineAssistant = _countAssistantBubbles();

        // Flip ONLY now that the container shows newConvId's history: any earlier await window
        // (working-root POST, appendMessage loop) would have rendered live frames into the
        // OUTGOING workspace's DOM. From here on this view is current; ensure() then sets the
        // COOLEMS.ws alias to THIS chat's socket and queued frames flush straight after it.
        COOLEMS.currentConversation = newConvId;          // flip right before the swap
        ChatSocketPool.clearAttention(newConvId);         // user is looking at it now — no badge

        // (2026-09-08 multi-chat) open this chat's own socket immediately — the server
        // replays any buffered background frames on connect, so nothing is lost.
        const _switchWs = ChatSocketPool.ensure(newConvId);
        // (2026-10-09 switch-race fix) guard window: between the flip above and the flush below,
        // any frame for THIS chat must be held by the replay guard instead of rendering ahead
        // of the queued frames. Replay mode only applies when THIS switch opens a fresh handshake -
        // an already-OPEN pooled socket delivers no replay (no replay_end marker), so the guard
        // turns off before the flush or it would swallow this chat's live frames.
        COOLEMS._inReplay = true;
        if (!(_switchWs && _switchWs.readyState === WebSocket.CONNECTING)) {
            COOLEMS._inReplay = false;                          // no fresh handshake -> no replay will come
        }

        // Live-turn continuation: chunks that arrived while we were rebuilding (socket was
        // already open, so the server delivered them to our JS queue) render into ONE fresh
        // streaming bubble on top of the DB history. With a fresh handshake the queue is empty
        // — those frames come as server replay instead, and _inReplay guards them.
        WorkspaceViews.flushFrames(newConvId);

        // (2026-10-09 switch-race fix) outgoing workspace may go back to background - resume its routing.
        if (oldConvId) WorkspaceViews.resume(oldConvId);

        isSwitchingConversation = false;

        log('Successfully loaded conversation:', newConvId);

    } catch (e) {
        logError('Failed to load conversation:', e);
        showNotification('Failed to load workspace: ' + (e.message || 'Unknown error'), 'error');

        // Recovery (2026-10-09 switch-race fix): if the flip to newConvId already happened, STAY
        // there - reverting would render this workspace's frames into a container that now shows
        // its (partially rebuilt) history. Otherwise put the outgoing workspace's stashed view
        // back exactly as it was and go on with it.
        const _flipped = (COOLEMS.currentConversation === newConvId);
        if (!_flipped && oldConvId && WorkspaceViews.hasView(oldConvId)) {
            COOLEMS.currentConversation = oldConvId;
            WorkspaceViews.restore(oldConvId);   // reattach the real node + streaming state
            ChatSocketPool.ensure(oldConvId);    // alias COOLEMS.ws back to its socket
        } else if (_flipped) {
            ChatSocketPool.ensure(newConvId);    // make sure this chat's socket/alias is live
        }

        // (2026-10-09 switch-race fix) the outgoing workspace may go back to background - lift its suspension.
        if (oldConvId) WorkspaceViews.resume(oldConvId);

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
                historyEl.innerHTML = data.conversations.map(conv => {
                    const atKind = (typeof ChatSocketPool !== 'undefined') ? (ChatSocketPool.attention[conv.id] || '') : '';
                    return `
                    <div class="chat-history-item ${conv.id === COOLEMS.currentConversation ? 'active' : ''}"
                         data-conv-id="${conv.id}"
                         onclick="loadConversation('${conv.id}')">
                        <svg class="chat-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path>
                        </svg>
                        ${typeof ChatSocketPool !== 'undefined'
                            ? `<span class="chat-status-dot status-${ChatSocketPool.statuses[conv.id] || 'idle'}"></span>`
                            : ''}
                        ${atKind ? `<span class="chat-attention-badge attention-${atKind}" title="${atKind === 'dialog' ? 'Waiting for your decision' : 'Task finished in the background'}"></span>` : ''}
                        <span style="flex:1;overflow:hidden;text-overflow:ellipsis;">${escapeHtml(conv.title)}</span>
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
