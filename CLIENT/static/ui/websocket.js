/**
 * websocket.js - WebSocket connection, message handling, and streaming logic.
 * Uses COOLEMS shared state from app.js.
 * 
 * FIXED: Race condition when switching conversations with max_connections=1
 * - Added delay between closing old connection and creating new one
 * - Proper cleanup of old WebSocket event handlers
 * - Separated connection creation into dedicated function
 * 
 * TOKEN STATISTICS:
 * - Listens for 'token_stats' message type which is pushed by the server
 *   after EVERY chat_stream() call (normal mode and every ReAct iteration).
 * - Updates the history status bar LIVE with real token counts from the provider.
 * - No polling needed — data is pushed to the exact WebSocket connection.
 * 
 * FIXED: Token stats display now correctly shows:
 * - 128K ctx | X.XK sent | XX.XK left
 * - Generation speed (tok/s)
 * - Real prompt_tokens and generated_tokens from provider
 */


// (2026-09-08 multi-chat) connectWebSocket / createNewWebSocket now live in
// chat-socket-pool.js: each chat owns its own socket and switching chats NEVER closes
// another chat's connection. The pool is loaded before this file (see index.html).
function updateConnectionStatus(status, message) {
    const el = document.getElementById('connectionStatus');
    if (!el) return;
    
    // Store previous status for model switch recovery
    COOLEMS._lastConnectionStatus = status;
    COOLEMS._lastConnectionMessage = message;
    
    el.className = 'connection-status ' + status;
    const span = el.querySelector('span');
    if (span) span.textContent = message;
    
    // Also update the status in COOLEMS state for debugging
    COOLEMS.connectionStatus = status;
}

function handleSendClick() { 
    if (COOLEMS.isStreaming) stopGeneration(); 
    else sendMessage(); 
}

function setButtonState(streaming) {
    const btn = document.getElementById('sendBtn');
    const icon = document.getElementById('sendIcon');
    if (!btn || !icon) return;
    
    if (streaming) {
        btn.classList.add('stop'); 
        btn.title = 'Stop generation';
        icon.innerHTML = '<rect x="6" y="6" width="12" height="12" rx="1"></rect>';
    } else {
        btn.classList.remove('stop'); 
        btn.title = 'Send message';
        icon.innerHTML = '<line x1="22" y1="2" x2="11" y2="13"></line><polygon points="22 2 15 22 11 13 2 9 22 2"></polygon>';
    }
}

// Abort controller for stop requests - prevents multiple concurrent stops
let stopAbortController = null;

function stopGeneration() {
    if (!COOLEMS.currentConversation) {
        showNotification('No active conversation', 'warning');
        return;
    }

    // CRITICAL FIX: Always reset UI state immediately to prevent button from getting stuck.
    // (2026-09-10 stale-task fix) This runs FIRST, so the red stop button is cleared even if
    // the backend call below fails or times out - clicking Stop can never leave a ghost state.
    COOLEMS.isStreaming = false;
    setButtonState(false);
    COOLEMS.streamingBuffer = '';

    // Abort any pending stop request to prevent multiple concurrent stops
    if (stopAbortController) {
        stopAbortController.abort();
    }
    stopAbortController = new AbortController();

    // (2026-09-08 multi-chat) the socket STAYS OPEN: stopping now goes through /api/stop,
    // which sets the chat's channel stop event on the CLIENT backend. The background turn
    // aborts and sends its 'system' + 'done' frames over this very socket — closing it here
    // (the old behaviour) would have detached the subscriber before those arrived.
    if (typeof ChatSocketPool !== 'undefined') {
        ChatSocketPool.updateChatStatus(COOLEMS.currentConversation, 'idle');
    }

    // Send stop request to the CLIENT backend (bus-aware, 2026-09-10 stale-task fix).
    // The endpoint ALWAYS answers ok for a known conversation - either it stopped a live
    // session or it honestly reports "nothing was running" - so clicking Stop can never be
    // left with an error toast while the red state is already cleared above. A bounded
    // timeout keeps this call from hanging when the backend is unresponsive.
    const _stopTimeout = AbortSignal.timeout ? AbortSignal.timeout(3000) : null;
    fetch(`/api/stop/${COOLEMS.currentConversation}`, {
        method: "POST",
        signal: _stopTimeout || stopAbortController.signal
    })
        .then(response => response.json())
        .then(data => {
            if (data && data.status === 'error') {
                showNotification(data.message || "Stop failed", "warning");
            } else {
                showNotification((data && data.message) || "Generation stopped", "info");
            }
        })
        .catch(error => {
            // UI state was already cleared at the top of this function - the red button is
            // gone no matter what. If even the stop call failed, say so honestly instead of
            // pretending the signal arrived (the backend also stops everything on close).
            if (error.name === 'AbortError') {
                log('Stop request was aborted (new stop requested)');
            } else {
                logError('Stop error:', error);
                showNotification("Could not reach server - stopping local state only", "warning");
            }
        });
}
    
function appendGeneratedImage(data) {
    const container = document.getElementById('chatContainer');
    const welcome = document.getElementById('welcome');
    if (welcome && welcome.style.display !== 'none') welcome.style.display = 'none';
    
    const messageDiv = document.createElement('div');
    messageDiv.className = 'message assistant';
    
    const imageHtml = `
        <div class="avatar">AI</div>
        <div class="message-content">
            <div class="generated-image-container">
                <div class="generated-image-title">
                    <span>🎨</span> Image Generated
                </div>
                <img src="data:image/png;base64,${data.image_base64}" 
                     class="generated-image"
                     onclick="window.open('${data.file_path}')"
                     alt="${data.filename || 'Generated image'}"
                     title="Click to open file">
                <div class="generated-image-meta">
                    <span>📁 ${data.file_path || 'Saved'}</span>
                    ${data.width && data.height ? `<span>📐 ${data.width}×${data.height}</span>` : ''}
                </div>
                <button class="copy-btn copy-top" onclick="copyMessage(this)">📋 Copy</button>
                <button class="copy-btn copy-bottom" onclick="copyMessage(this)">📋 Copy</button>
            </div>
        </div>
    `;
    
    messageDiv.innerHTML = imageHtml;
    messageDiv.querySelector('.message-content').setAttribute('data-raw-content', `Generated image: ${data.file_path}`);
    container.appendChild(messageDiv);
    
    if (COOLEMS.autoScrollEnabled && !COOLEMS.userScrolledUp) {
        scrollToBottom();
    }
}

/**
 * Update the history status bar with token statistics.
 * Called from WebSocket token_stats messages (live, per-user, after every chat_stream call).
 * 
 * @param {Object} data - Token stats data object with:
 *   - prompt_tokens: tokens sent to provider as input
 *   - generated_tokens: tokens generated by provider
 *   - generation_speed: tokens per second
 *   - context_window: total context window size
 *   - tokens_remaining: how much room is left
 */
function updateHistoryStatusBar(data) {
    const el = document.getElementById('historyStatusText');
    if (!el) {
        console.warn('[TokenStats] historyStatusText element not found');
        return;
    }

    // Get the context window from the data, or fallback to default
    const windowTokens = data.context_window || COOLEMS.contextWindowTokens || 131072;
    const sentTokens = data.prompt_tokens !== undefined && data.prompt_tokens !== null ? data.prompt_tokens : 0;
    const generatedTokens = data.generated_tokens !== undefined && data.generated_tokens !== null ? data.generated_tokens : 0;
    const speed = data.generation_speed;
    
    // Calculate remaining tokens if not provided or invalid
    let remaining = data.tokens_remaining;
    if (remaining === undefined || remaining === null || remaining < 0) {
        remaining = Math.max(0, windowTokens - sentTokens - generatedTokens);
    }
    
    // Ensure remaining is a valid number
    if (isNaN(remaining)) {
        remaining = 0;
    }

    // Format K values (divide by 1024 for KB display)
    const windowK = (windowTokens / 1024).toFixed(0);
    const sentK = (sentTokens / 1024).toFixed(1);
    const remainingK = (remaining / 1024).toFixed(1);

    // Build the display string

    // Add react loop stats if available (agentic mode)
    let loopStatsPrefix = "";
    if (data.loop_tools !== undefined && data.loop_tools > 0) {
        const totalCalls = data.loop_tools;
        const noToolCalls = data.loop_no_tools || 0;
        loopStatsPrefix = `Loop stats: ${totalCalls} / ${noToolCalls} | `;
    }

    let text = loopStatsPrefix + `${windowK}K ctx | ${sentK}K sent | ${remainingK}K left`;
    
    // Store for debugging
    COOLEMS.tokensSentToOllama = sentTokens;
    COOLEMS.contextWindowTokens = windowTokens;
    
    // Add speed if available and positive
    if (speed !== null && speed !== undefined && !isNaN(speed) && speed > 0) {
        text += ` | ⚡ ${speed.toFixed(1)} tok/s`;
        COOLEMS.lastGenerationSpeed = speed;
    } else if (generatedTokens > 0) {
        // Calculate approximate speed if we have generated tokens and time
        // This uses the total_duration from the stats if available
        const totalDuration = data.total_duration;
        if (totalDuration && totalDuration > 0 && generatedTokens > 0) {
            const approxSpeed = (generatedTokens / totalDuration).toFixed(1);
            text += ` | ⚡ ${approxSpeed} tok/s (est)`;
        }
    }
    
    // Update the display
    el.textContent = text;
    
    // Log for debugging
    console.log(`[TokenStats] Updated: prompt=${sentTokens}, gen=${generatedTokens}, speed=${speed}, remaining=${remaining}, window=${windowTokens}`);
}

/**
 * Append a recovery warning block to the chat.
 * This shows an orange warning when the server went down and recovered.
 */
function appendRecoveryWarning(message, status) {
    const container = document.getElementById('chatContainer');
    const welcome = document.getElementById('welcome');
    if (welcome && welcome.style.display !== 'none') welcome.style.display = 'none';

    const messageDiv = document.createElement('div');
    messageDiv.className = 'message assistant';
    messageDiv.dataset.messageIndex = container.querySelectorAll('.message').length;

    let statusClass = '';
    let headerText = 'System Warning';
    if (status === 'success') {
        statusClass = 'success';
        headerText = 'System Recovery';
    } else if (status === 'error') {
        statusClass = 'error';
        headerText = 'System Error';
    }

    messageDiv.innerHTML = '<div class="avatar">AI</div><div class="message-content">' +
        '<div class="recovery-warning-block">' +
            '<div class="recovery-warning-header">' + headerText + '</div>' +
            '<div class="recovery-warning-content ' + statusClass + '">' + escapeHtml(message) + '</div>' +
        '</div>' +
        '<button class="copy-btn copy-top" onclick="copyMessage(this)">📋 Copy</button>' +
        '<button class="copy-btn copy-bottom" onclick="copyMessage(this)">📋 Copy</button>' +
    '</div>';

    messageDiv.querySelector('.message-content').setAttribute('data-raw-content', message);
    container.appendChild(messageDiv);

    if (COOLEMS.autoScrollEnabled && !COOLEMS.userScrolledUp) { scrollToBottom(); }
}

// ===== REPLAY GUARD (2026-09-08 multi-chat UI fix) ============================
// When switching back to a chat, the server replays that chat's buffered frame tail
// (frames captured while nobody was looking). Those frames must NOT be rendered
// blindly: content/thinking chunks whose answer is ALREADY in the DB history would
// duplicate the last assistant bubble. The guard renders replayed assistant-streaming
// frames ONLY when this chat really is generating/queued AND no fresh assistant
// bubble exists yet beyond the baseline (assistant messages loaded from the DB).
// A `replay_end` frame marks where the buffered tail ends; live frames follow it.
COOLEMS._inReplay = false;
COOLEMS._baselineAssistant = 0;

function _countRenderedAssistantBubbles() {
    const c = document.getElementById('chatContainer');
    return c ? c.querySelectorAll('.message.assistant').length : 0;
}

/** True while replaying the buffered tail AND this chat has a live session. */
function _replayShouldRenderAssistantFrame() {
    if (!COOLEMS._inReplay) return true;                       // live frame - always render
    const st = (typeof ChatSocketPool !== 'undefined' && COOLEMS.currentConversation)
        ? (ChatSocketPool.statuses[COOLEMS.currentConversation] || 'idle') : 'idle';
    if (st === 'idle') return false;                            // turn finished while away - answer is in the DB
    const fresh = _countRenderedAssistantBubbles() > COOLEMS._baselineAssistant;
    return !fresh;                                              // already rendered this turn's bubble once
}

function handleWebSocketMessage(data) {
    const chatContainer = document.getElementById('chatContainer');
    const wasNearBottom = isNearBottom(chatContainer);

    // End of the buffered-tail replay: live streaming resumes from here on.
    if (data.type === 'replay_end') {
        COOLEMS._inReplay = false;
        return;
    }

    // Marker frame: this turn's streamed answer is now in the DB history (sent by the bus
    // right after persistence, replayed before replay_end on re-attach). Nothing to render.
    if (data.type === 'answer_persisted') {
        return;
    }

    if (data.type === 'content') {
        // Replay guard FIRST (before touching the buffer): skip assistant-streaming
        // frames that would duplicate a DB-persisted answer.
        if (!_replayShouldRenderAssistantFrame()) return;
        COOLEMS.streamingBuffer += data.content;

        let lastMsg = chatContainer.lastElementChild;
        if (lastMsg && lastMsg.classList.contains('assistant') && lastMsg.dataset.streaming) {
            const contentDiv = lastMsg.querySelector('.content-text');
            const messageContent = lastMsg.querySelector('.message-content');
            const thinkingBlock = messageContent ? messageContent.querySelector('.thinking-block') : null;

            contentDiv.innerHTML = parseMarkdown(COOLEMS.streamingBuffer, true);

            if (thinkingBlock && messageContent && !messageContent.querySelector('.thinking-block')) {
                messageContent.insertBefore(thinkingBlock, contentDiv);
            }

            if (messageContent) messageContent.setAttribute('data-raw-content', COOLEMS.streamingBuffer);
        } else {
            COOLEMS.streamingBuffer = data.content;
            appendMessage('assistant', COOLEMS.streamingBuffer, true);
        }

        if (COOLEMS.autoScrollEnabled && !COOLEMS.userScrolledUp && wasNearBottom) {
            scrollToBottom();
        } else if (COOLEMS.userScrolledUp && COOLEMS.isStreaming) {
            showScrollIndicator();
        }
    } else if (data.type === 'thinking') {
        // Replay guard (same rule as content frames).
        if (!_replayShouldRenderAssistantFrame()) return;

        let lastMsg = chatContainer.lastElementChild;
        let thinkingBlock = null;
        let messageContent = null;

        if (lastMsg && lastMsg.classList.contains('assistant')) {
            messageContent = lastMsg.querySelector('.message-content');
            if (messageContent) {
                thinkingBlock = messageContent.querySelector('.thinking-block');
                if (!thinkingBlock) {
                    thinkingBlock = document.createElement('div');
                    thinkingBlock.className = 'thinking-block';
                    thinkingBlock.innerHTML = '<div class="thinking-header">Reasoning</div><div class="thinking-content"></div>';
                    const contentDiv = messageContent.querySelector('.content-text');
                    messageContent.insertBefore(thinkingBlock, contentDiv);
                }
            }
        } else {
            appendMessage('assistant', '', true);
            lastMsg = chatContainer.lastElementChild;
            messageContent = lastMsg.querySelector('.message-content');
            thinkingBlock = document.createElement('div');
            thinkingBlock.className = 'thinking-block';
            thinkingBlock.innerHTML = '<div class="thinking-header">Reasoning</div><div class="thinking-content"></div>';
            const contentDiv = messageContent.querySelector('.content-text');
            messageContent.insertBefore(thinkingBlock, contentDiv);
        }

        if (thinkingBlock) {
            const thinkingContent = thinkingBlock.querySelector('.thinking-content');
            thinkingContent.textContent += data.content;
            thinkingContent.scrollTop = thinkingContent.scrollHeight;
        }

        if (COOLEMS.autoScrollEnabled && !COOLEMS.userScrolledUp) { scrollToBottom(); }
    } else if (data.type === 'image_generated') {
        appendGeneratedImage(data);
    } else if (data.type === 'tool_start') {
        const d = document.createElement('div'); d.className = 'tool-call';
        d.innerHTML = `<span class="tool-name">🔧 Using ${data.tool}:</span> ${escapeHtml(JSON.stringify(data.input))}`;
        chatContainer.appendChild(d);
        // (2026-09-21) live Setup/Downloads section for self-unpacking tools: venv + pip
        // packages + model files with size/speed/ETA, polled from /api/setup/status.
        if (typeof SetupProgressUI !== 'undefined' && data.tool === 'generate_image') {
            SetupProgressUI.start('generate_tool');
        }
        if (COOLEMS.autoScrollEnabled && !COOLEMS.userScrolledUp) scrollToBottom();
    } else if (data.type === 'tool_end') {
        const d = document.createElement('div'); d.className = 'tool-call'; d.style.opacity = '0.7';
        d.innerHTML = `<span class="tool-name">✓ Result:</span> ${escapeHtml(String(data.output))}`;
        chatContainer.appendChild(d);
        // (2026-09-21) settle the Setup/Downloads card: drop it immediately when no setup
        // happened this turn, otherwise let its final state render and polling stop.
        if (typeof SetupProgressUI !== 'undefined' && data.tool === 'generate_image') {
            SetupProgressUI.settle('generate_tool');
        }
        if (COOLEMS.autoScrollEnabled && !COOLEMS.userScrolledUp) scrollToBottom();
    } else if (data.type === 'exec_approval_request') {
        // python_exec user approval card: the AI wants to run code ON THIS machine.
        appendExecApprovalCard(data);
    } else if (data.type === 'system') {
        showNotification(data.content, 'info');
    } else if (data.type === 'token_stats') {
        // LIVE token stats pushed from server after every chat_stream() call
        // This is bulletproof — each WebSocket connection gets its own data
        updateHistoryStatusBar(data);
    } else if (data.type === 'recovery_warning') {
        // Server recovery warning - show orange warning block in chat
        appendRecoveryWarning(data.message, data.status || 'warning');
    } else if (data.type === 'model_switch_status') {
        // Model switch progress updates pushed from server to all connected UI clients
        handleModelSwitchStatus(data);
    } else if (data.type === 'ollama_error' || data.type === 'provider_error') {
        // (2026-08-23 fix) Render provider errors as a NORMAL assistant message bubble -
        // exactly what the conversation history shows after a UI restart. The old
        // formatOllamaError() div card ('Provider Error' + generic 'restart the
        // provider' suggestions) looked like a broken widget and was useless for real
        // errors such as 'vision is not available (no mmproj)'. Plain text bubble = clean.
        appendMessage('assistant', data.content, false);
        COOLEMS.isStreaming = false; setButtonState(false); COOLEMS.streamingBuffer = '';
        updateConnectionStatus('error', 'Provider Error');
    } else if (data.type === 'done') {
        COOLEMS.isStreaming = false; setButtonState(false); COOLEMS.streamingBuffer = '';
        const lastMsg = chatContainer.lastElementChild;
        if (lastMsg) {
            delete lastMsg.dataset.streaming;
            lastMsg.querySelectorAll('.streaming-code').forEach(el => el.classList.remove('streaming-code'));
        }
        if (COOLEMS.filesPanelOpen && typeof loadWorkingRootFiles === 'function') loadWorkingRootFiles();
        COOLEMS.userScrolledUp = false;
        COOLEMS.autoScrollEnabled = true;
        hideScrollIndicator();
    } else if (data.type === 'search_web') {
        const d = document.createElement('div'); d.className = 'search-web-link';
        d.innerHTML = `<a href="#" onclick="triggerWebSearch('${escapeHtml(data.query)}'); return false;">🔍 Search same question on internet</a>`;
        chatContainer.appendChild(d);
        if (COOLEMS.autoScrollEnabled && !COOLEMS.userScrolledUp) scrollToBottom();
    } else if (data.type === 'error') {
        logError('Server error:', data.content);
        showNotification(data.content, 'error');
        COOLEMS.isStreaming = false;
        setButtonState(false);
    }
}

// ===== PYTHON_EXEC USER APPROVAL CARD (2026-09-24) ==============================
// The backend emits one 'exec_approval_request' frame per python_exec call the AI
// wants to run on THIS machine. This card shows the exact code and three options:
//   Allow this run / Run until task done (auto-run for the rest of this agentic run)
//   / Do not run. The decision is POSTed to /api/exec-approval/<conv>/<request>;
// on 'Do not run' the AI receives a message that the user declined and adapts.
function appendExecApprovalCard(data) {
    const container = document.getElementById('chatContainer');
    if (!container) return;
    const welcome = document.getElementById('welcome');
    if (welcome && welcome.style.display !== 'none') welcome.style.display = 'none';

    // Replay/duplicate safety: never render the same request twice.
    if (data.request_id && container.querySelector('.exec-approval-card[data-request-id="' + data.request_id + '"]')) {
        return;
    }

    // (2026-07-15 URL consent): the frame may carry title/description for non-code
    // consents (opening a local file / localhost target in the browser). When absent,
    // fall back to the original python_exec wording -- old frames keep rendering as before.
    const isUrlConsent = typeof data.title === 'string' && data.title.length > 0;
    const headerText = isUrlConsent
        ? '\u{1F517} ' + escapeHtml(data.title)
        : '&#128273; Run this Python code on your machine?';
    const descHtml = (typeof data.description === 'string' && data.description.length > 0)
        ? '<div class="exec-approval-desc">' + escapeHtml(data.description) + '</div>'
        : '';
    const allowLabel = isUrlConsent ? '&#10003; Allow this URL' : '&#10003; Allow this run';
    const runAllLabel = isUrlConsent ? '&#9889; Open local targets until task done' : '&#9889; Run until task done';

    const codeHtml = escapeHtml(String(data.code || ''));
    const card = document.createElement('div');
    card.className = 'message assistant';

    const inner = document.createElement('div');
    inner.className = 'avatar';
    inner.textContent = 'AI';

    const body = document.createElement('div');
    body.className = 'message-content exec-approval-card';
    if (data.request_id) body.setAttribute('data-request-id', data.request_id);
    body.innerHTML =
        '<div class="exec-approval-header">' + headerText + '</div>' +
        descHtml +
        '<pre class="exec-approval-code"><code>' + codeHtml + '</code></pre>' +
        '<span class="exec-approval-status pending">Waiting for your decision (no time limit - answer whenever you are ready)</span>' +
        '<div class="exec-approval-actions">' +
            '<button type="button" class="exec-btn exec-btn-allow" data-decision="allow">' + allowLabel + '</button>' +
            '<button type="button" class="exec-btn exec-btn-runall" data-decision="run_all">' + runAllLabel + '</button>' +
            '<button type="button" class="exec-btn exec-btn-deny" data-decision="deny">&#10007; ' + (isUrlConsent ? 'Do not open' : 'Do not run') + '</button>' +
        '</div>';

    body.querySelectorAll('.exec-btn').forEach(btn => {
        btn.addEventListener('click', () => submitExecApproval(data, btn));
    });

    card.appendChild(inner);
    card.appendChild(body);
    container.appendChild(card);

    if (COOLEMS.autoScrollEnabled && !COOLEMS.userScrolledUp) scrollToBottom();
}

function submitExecApproval(data, clickedBtn) {
    const card = clickedBtn.closest('.exec-approval-card');
    if (!card || card.dataset.answered === '1') return;
    card.querySelectorAll('.exec-btn').forEach(b => { b.disabled = true; });
    const statusEl = card.querySelector('.exec-approval-status');
    const decision = clickedBtn.dataset.decision;

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
            const urlConsent = typeof data.title === 'string' && data.title.length > 0;
            if (decision === 'allow') msg = urlConsent ? '\u2705 Allowed - opening this URL in the browser now.' : '\u2705 Allowed - the code is running now.';
            else if (decision === 'run_all') msg = urlConsent
                ? '\u26A1 Auto-open enabled: local file/localhost targets will open without asking until this task ends.'
                : '\u26A1 Auto-run enabled: python_exec will run without asking until this task ends.';
            else msg = urlConsent
                ? '\u274C Not opened - you decided not to open this target. The AI was told and will continue differently.'
                : '\u274C Not run - you decided not to execute this code. The AI was told and will continue differently.';
            if (statusEl) { statusEl.className = 'exec-approval-status done-' + decision; statusEl.textContent = msg; }
        })
        .catch(err => {
            card.dataset.answered = '1';
            if (statusEl) { statusEl.className = 'exec-approval-status done-error'; statusEl.textContent = '\u26A0 ' + err.message; }
        });
}
/**
 * Handle model_switch_status broadcast messages from server.
 * Updates the connection status indicator with live model switch progress.
 */
let _modelSwitchSuccessShownFor = null;  // guard: one "good" message per switch

function handleModelSwitchStatus(data) {
    const { status, message, model } = data;
    
    // Shorten model name for display (just the filename without path)
    const modelName = model ? model.split('/').pop() : '';
    
    let uiStatus = 'connecting';  // default pulsing/orange state
    let uiMessage = message;
    
    switch(status) {
        case 'started':
            uiStatus = 'connecting';
            uiMessage = `Switching model...`;
            break;
        case 'reloading':
            uiStatus = 'connecting';
            uiMessage = `Server reloading (${modelName})`;
            break;
        case 'waiting':
            uiStatus = 'connecting';
            uiMessage = message || 'Waiting for server to reload...';
            break;
        case 'success':
            uiStatus = 'connected';
            uiMessage = `Connected (${modelName})`;
            // Store the new model name in global state
            if (modelName) {
                COOLEMS.currentModel = modelName;
            }
            // (2026-08-23 fix) This is the ONLY place the "good" message appears:
            // it arrives only after the SERVER confirmed the reload. Refresh the
            // dropdown so the UI immediately shows the NEW model (+ loaded marker).
            if (_modelSwitchSuccessShownFor !== modelName) {
                _modelSwitchSuccessShownFor = modelName;
                showNotification('\u2713 Model switched to ' + (modelName || message), 'success');
                if (typeof loadModels === 'function') {
                    loadModels().catch(() => {});
                }
            } else {
                log('[MODEL SWITCH] Duplicate success broadcast for ' + modelName + ' ignored');
            }
            // Switch finished - send button can come back.
            COOLEMS.modelSwitchPending = false;
            const _sbOk = document.getElementById('sendBtn');
            if (_sbOk) _sbOk.disabled = false;
            break;
        case 'failed':
            uiStatus = 'error';
            uiMessage = `Switch failed: ${message}`;
            _modelSwitchSuccessShownFor = null;  // allow a good message on the next switch
            showNotification(`Model switch failed: ${message}`, 'error');
            COOLEMS.modelSwitchPending = false;
            const _sbFail = document.getElementById('sendBtn');
            if (_sbFail) _sbFail.disabled = false;
            break;
    }
    
    updateConnectionStatus(uiStatus, uiMessage);
}

function triggerWebSearch(query) {
    const messages = document.getElementById('chatContainer').querySelectorAll('.message');
    let lastAiResponse = '';
    for (let i = messages.length - 1; i >= 0; i--) { 
        if (messages[i].classList.contains('assistant')) { 
            lastAiResponse = messages[i].textContent; 
            break; 
        } 
    }
    COOLEMS.isStreaming = false; 
    COOLEMS.streamingBuffer = ''; 
    setButtonState(false);
    const fullMessage = lastAiResponse ? `/search ${query}\n\nPrevious answer for comparison:\n${lastAiResponse}` : `/search ${query}`;
    document.getElementById('messageInput').value = fullMessage;
    sendMessage();
}

// Helper function to manually reconnect (exposed for debugging)
window.forceReconnect = function() {
    if (COOLEMS.currentConversation) {
        log('Manual reconnect requested');
        // (2026-09-08 multi-chat) pool-aware: drop ONLY this chat's socket, then re-open it.
        ChatSocketPool.close(COOLEMS.currentConversation, true);
        connectWebSocket(COOLEMS.currentConversation);
    } else {
        showNotification('No active conversation to reconnect to', 'warning');
    }
};
