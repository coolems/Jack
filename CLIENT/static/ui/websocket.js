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
    // (2026-10-23 live steering) the prompt stays usable while an AGENTIC turn runs:
    // Enter steers that task. The stop button keeps its existing cancel behavior.
    _updateSteerUi(streaming);
}

function _updateSteerUi(streaming) {
    const hint = document.getElementById('steerHint');
    const input = document.getElementById('messageInput');
    const canSteer = !!streaming && !!COOLEMS.streamingAgentMode;
    if (hint) hint.classList.toggle('visible', canSteer);
    if (input) {
        input.placeholder = canSteer
            ? 'Steer the running task… (Enter to send · Shift+Enter for a new line)'
            : 'Message COOLEMS...';
    }
}

// Abort controller for stop requests - prevents multiple concurrent stops
let stopAbortController = null;

function stopGeneration() {
    if (!COOLEMS.currentConversation) {
        showNotification('No active workspace', 'warning');
        return;
    }

    // CRITICAL FIX: Always reset UI state immediately to prevent button from getting stuck.
    // (2026-09-10 stale-task fix) This runs FIRST, so the red stop button is cleared even if
    // the backend call below fails or times out - clicking Stop can never leave a ghost state.
    COOLEMS.isStreaming = false;
    setButtonState(false);
    // (2026-11-24) the alias proxies into ChatStreamState for THIS conversation.
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

// (2026-10-04) ALWAYS-VISIBLE SETUP/TIER REPORT: every generated-image card now carries a
// compact one-liner telling which TIER served the image, on which GPU, with which model
// line, and what was installed/downloaded THIS run - or an honest "already cached" note.
// The data arrives in the 'image_generated' frame (setup/pipeline/model_id/dtype keys),
// filled by CLIENT/logic/tool_executor.py from generate_image's per-run setup block. On a
// fully-cached machine nothing was downloaded, so instead of staying silent this line says
// exactly that - the process is visible even when it "passes in a blink".
function _formatSetupLine(data) {
    if (!data || typeof data !== 'object') return '';
    const s = (data.setup && typeof data.setup === 'object') ? data.setup : null;
    const parts = [];

    // Tier + how it was chosen.
    let tierTxt = '';
    if (s && s.tier) {
        tierTxt = String(s.tier);
        if (s.tier_note) tierTxt += ' (' + s.tier_note + ')';
    } else if (data.pipeline || data.model_id) {
        // No setup block (older server / error path): fall back to the top-level keys.
        tierTxt = String(data.pipeline || '');
    }
    if (tierTxt) parts.push('\u2699\ufe0f tier ' + tierTxt);

    // GPU.
    if (s && s.gpu) parts.push(String(s.gpu));

    // Model line: prefer the setup block, fall back to top-level model_id/dtype/pipeline.
    const model = (s && s.model) || data.model_id;
    const dtype = (s && s.dtype) || data.dtype;
    if (model) parts.push(model + (dtype ? ' ' + dtype : ''));

    // What was installed / downloaded THIS run (or the honest cached note).
    if (s) {
        if (s.note) {
            parts.push(s.note);
        } else {
            const bits = [];
            if (s.venv === 'created this run') bits.push('venv created');
            else if (s.venv) bits.push(String(s.venv));
            if (typeof s.pip_packages === 'number' && s.pip_packages > 0) {
                bits.push(s.pip_packages + ' pip package' + (s.pip_packages > 1 ? 's' : '') + ' installed');
            }
            const mf = String(s.model_files || '');
            if (mf && mf !== '0') {
                let m = 'model files ' + mf;
                if (typeof s.model_bytes === 'number' && s.model_bytes > 0) m += ' (~' + _fmtGB(s.model_bytes) + ')';
                bits.push(m);
            }
            if (bits.length) parts.push(bits.join(' \u00b7 '));
        }
    }
    return parts.join('  \u00b7  ');
}

// Small GB/MB formatter for the setup line.
function _fmtGB(bytes) {
    const gb = bytes / (1024 * 1024 * 1024);
    if (gb >= 1) return gb.toFixed(1).replace(/\.0$/, '') + ' GB';
    const mb = bytes / (1024 * 1024);
    return Math.round(mb) + ' MB';
}

function appendGeneratedImage(data) {
    const container = document.getElementById('chatContainer');
    const welcome = document.getElementById('welcome');
    if (welcome && welcome.style.display !== 'none') welcome.style.display = 'none';

    const messageDiv = document.createElement('div');
    messageDiv.className = 'message assistant';

    // Always-visible setup/tier line (2026-10-04) - rendered for EVERY image, cached or not.
    const setupLine = _formatSetupLine(data);

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
                ${setupLine ? `<div class="generated-image-setup">${escapeHtml(setupLine)}</div>` : ''}
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

// (2026-08-23 session stats) format seconds as MM:SS (or H:MM:SS past one hour).
function formatSessionTime(totalSec) {
    if (!isFinite(totalSec) || totalSec < 0) return '0:00';
    const s = Math.floor(totalSec % 60);
    const m = Math.floor((totalSec / 60) % 60);
    const h = Math.floor(totalSec / 3600);
    const mm = (h > 0 ? String(m).padStart(2, '0') : String(m));
    return (h > 0 ? h + ':' : '') + mm + ':' + String(s).padStart(2, '0');
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

    // (2026-08-23 session stats) cumulative generated tokens + active session times.
    // Shown in BOTH normal and agentic mode whenever the server includes the fields:
    //   📝 N.NK gen  - total provider output for THIS turn (loop 1 -> current, summed live)
    //   ⏱ MM:SS     - ACTIVE time of this turn so far (approval/consent menu pauses excluded)
    //   ↩ MM:SS last- settled duration of the PREVIOUS completed turn ("last session")
    if (data.session_generated_tokens !== undefined && data.session_generated_tokens > 0) {
        text += ` | 📝 ${(data.session_generated_tokens / 1024).toFixed(1)}K gen`;
    }
    if (data.session_elapsed_sec !== undefined && data.session_elapsed_sec >= 0) {
        text += ` | ⏱ ${formatSessionTime(data.session_elapsed_sec)}`;
    }
    if (data.last_session_sec !== undefined && data.last_session_sec > 0) {
        text += ` | ↩ ${formatSessionTime(data.last_session_sec)} last`;
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

// ===== PER-CONVERSATION STREAM STATE (2026-11-24 multi-chat mix fix) ====================
// THE FIX for the cross-workspace content mix: streamingBuffer / _inReplay /
// _baselineAssistant used to be ONE global set of variables shared by every workspace.
// With two concurrent long jobs, a frame from background workspace A arriving during an
// await window (or right after switching) would overwrite the freshly reset globals that
// belonged to the view on screen - text from one chat rendered into another.
// Every piece of per-turn render state now lives in ChatStreamState, keyed by the conv id
// that OWNS the frame (threaded through handleWebSocketMessage(data, ownerConvId)), never
// by whichever workspace happens to be current at delivery time.
const ChatStreamState = {
    states: {},   // convId -> { streamingBuffer, inReplay, baselineAssistant, pendingTurnStatsLine }

    /** Get (or lazily create) the render state of one conversation. */
    get(convId) {
        if (!convId) return null;
        let s = this.states[convId];
        if (!s) {
            s = { streamingBuffer: '', inReplay: false, baselineAssistant: 0, pendingTurnStatsLine: '' };
            this.states[convId] = s;
        }
        return s;
    },

    /** Drop one conversation's render state (workspace deleted / view dropped). */
    drop(convId) { if (convId && this.states[convId]) delete this.states[convId]; },

    clearAll() { this.states = {}; },
};

// Legacy aliases: COOLEMS.streamingBuffer / _inReplay / _baselineAssistant /
// _pendingTurnStatsLine still work for the CURRENT conversation (message.js send path,
// switch paths) - they proxy into ChatStreamState instead of holding their own copy.
(function defineLegacyStreamingAliases() {
    function alias(prop, key) {
        Object.defineProperty(COOLEMS, prop, {
            get: () => {
                const s = COOLEMS.currentConversation ? ChatStreamState.get(COOLEMS.currentConversation) : null;
                return s ? s[key] : (key === 'streamingBuffer' || key === 'pendingTurnStatsLine' ? '' : 0);
            },
            set: v => {
                if (!COOLEMS.currentConversation) return;   // no view - nothing to bind to
                ChatStreamState.get(COOLEMS.currentConversation)[key] = v;
            }
        });
    }
    alias('streamingBuffer', 'streamingBuffer');
    alias('_inReplay', 'inReplay');
    alias('_baselineAssistant', 'baselineAssistant');
    alias('_pendingTurnStatsLine', 'pendingTurnStatsLine');
})();

function _countRenderedAssistantBubbles() {
    const c = document.getElementById('chatContainer');
    return c ? c.querySelectorAll('.message.assistant').length : 0;
}

/**
 * True while replaying the buffered tail AND this chat has a live session.
 * (2026-11-24) ownerConvId is the conversation that OWNS the frame - its status and
 * baseline are consulted, not currentConversation's (the old code evaluated the wrong
 * chat during switch windows).
 */
function _replayShouldRenderAssistantFrame(ownerConvId) {
    const convId = ownerConvId || COOLEMS.currentConversation;
    const st0 = convId ? ChatStreamState.get(convId) : null;
    if (!st0 || !st0.inReplay) return true;                   // live frame - always render
    const st = (typeof ChatSocketPool !== 'undefined' && convId)
        ? (ChatSocketPool.statuses[convId] || 'idle') : 'idle';
    if (st === 'idle') return false;                           // turn finished while away - answer is in the DB
    const fresh = _countRenderedAssistantBubbles() > st0.baselineAssistant;
    return !fresh;                                             // already rendered this turn's bubble once
}

// (2026-10-10) FINAL TURN STATS: splice the last loop-stats line directly above
// 'Agentic AI: TASK DONE' in the final answer bubble. The backend splices the SAME line
// into the persisted text, so live bubbles and DB-reloaded history look identical.
// Idempotent + replay-safe: if the line is already present (DB-loaded bubble or a
// replayed frame) nothing happens; if the bubble is not rendered yet the line is parked
// in COOLEMS._pendingTurnStatsLine and applied on the 'done' frame.
function applyTurnStatsLine(line, ownerConvId) {
    if (!line || typeof line !== 'string') return;
    const convId = ownerConvId || COOLEMS.currentConversation;
    const st0 = convId ? ChatStreamState.get(convId) : null;   // per-conv parking spot (2026-11-24 mix fix)
    const container = document.getElementById('chatContainer');
    if (!container) { if (st0) st0.pendingTurnStatsLine = line; return; }

    // Last assistant bubble whose raw content carries the TASK DONE marker.
    let target = null;
    const bubbles = container.querySelectorAll('.message.assistant .message-content[data-raw-content]');
    for (let i = bubbles.length - 1; i >= 0; i--) {
        const raw = bubbles[i].getAttribute('data-raw-content') || '';
        if (raw.indexOf('Agentic AI: TASK DONE') !== -1) { target = bubbles[i]; break; }
    }
    if (!target) {
        if (st0) st0.pendingTurnStatsLine = line;   // bubble not rendered yet - apply on 'done'
        return;
    }

    const raw = target.getAttribute('data-raw-content');
    if (raw.indexOf(line) !== -1) return;       // already present - idempotent

    // (2026-10-10 fix) Target ONLY the marker on its own line - same rule as the backend.
    // The model can mention "Agentic AI: TASK DONE" mid-prose; splicing before such a
    // meta-mention put the stats at the TOP of the answer (humanly wrong). Use the LAST
    // standalone marker line; if none exists, append both lines cleanly at the very end.
    // (2026-10-10 audit) lastIndexOf() could land on a LATER inline quote of the exact
    // marker text instead of the real standalone line - walk the /gm matches and keep the
    // LAST match's own start index so live bubbles byte-match the backend splice.
    const _doneRe = /^[ \t]*Agentic AI: TASK DONE[ \t]*$/gm;
    let lastStart = -1, _mm;
    while ((_mm = _doneRe.exec(raw)) !== null) { lastStart = _mm.index; }
    let newRaw;
    if (lastStart >= 0) {
        newRaw = raw.slice(0, lastStart) + line + '\n' + raw.slice(lastStart);
    } else {
        newRaw = raw.replace(/\n+$/, '') + '\n' + line + '\nAgentic AI: TASK DONE';
    }
    target.setAttribute('data-raw-content', newRaw);
    const contentDiv = target.querySelector('.content-text');
    if (contentDiv && typeof parseMarkdown === 'function') {
        contentDiv.innerHTML = parseMarkdown(newRaw, false);
    }
}
function handleWebSocketMessage(data, ownerConvId) {
    // (2026-11-24 mix fix) every frame is handled against the state of the conversation that
    // OWNS it - never the one that happens to be on screen at delivery time. All live call
    // sites pass the owner explicitly (socket pool / flushFrames); a frame without an owner
    // has no view it belongs to and must not touch any render state.
    const convId = ownerConvId || COOLEMS.currentConversation;
    if (!convId) return;
    const st = ChatStreamState.get(convId);
    const chatContainer = document.getElementById('chatContainer');

    // THE core mix fix: if this frame's own view is not the one on screen (mid-switch rebuild,
    // or a stale handler call), re-queue it for its workspace instead of rendering into a
    // container that already shows another workspace. On restore, flushFrames() replays it in
    // order - nothing is lost and nothing crosses over.
    if (convId && convId !== COOLEMS.currentConversation) {
        WorkspaceViews.queueFrame(convId, data);
        return;
    }

    const wasNearBottom = isNearBottom(chatContainer);

    // End of the buffered-tail replay: live streaming resumes from here on.
    if (data.type === 'replay_end') {
        if (st) st.inReplay = false;
        return;
    }

    // Marker frame: this turn's streamed answer is now in the DB history (sent by the bus
    // right after persistence, replayed before replay_end on re-attach). Nothing to render.
    if (data.type === 'answer_persisted') {
        return;
    }

    if (data.type === 'content') {
        // Replay guard FIRST (before touching the buffer): skip assistant-streaming
        // frames that would duplicate a DB-persisted answer. Per-conv state, per-conv status.
        if (!_replayShouldRenderAssistantFrame(convId)) return;
        st.streamingBuffer += data.content;

        let lastMsg = chatContainer.lastElementChild;
        if (lastMsg && lastMsg.classList.contains('assistant') && lastMsg.dataset.streaming) {
            const contentDiv = lastMsg.querySelector('.content-text');
            const messageContent = lastMsg.querySelector('.message-content');
            const thinkingBlock = messageContent ? messageContent.querySelector('.thinking-block') : null;

            contentDiv.innerHTML = parseMarkdown(st.streamingBuffer, true);

            if (thinkingBlock && messageContent && !messageContent.querySelector('.thinking-block')) {
                messageContent.insertBefore(thinkingBlock, contentDiv);
            }

            if (messageContent) messageContent.setAttribute('data-raw-content', st.streamingBuffer);
        } else {
            st.streamingBuffer = data.content;
            // SECURITY fix 2026-10-05: appendMessage is async - fire-and-forget in this sync handler (DOM mutation happens synchronously inside).
            void appendMessage('assistant', st.streamingBuffer, true).catch(e => console.error('[WS] appendMessage failed:', e));
        }

        if (COOLEMS.autoScrollEnabled && !COOLEMS.userScrolledUp && wasNearBottom) {
            scrollToBottom();
        } else if (COOLEMS.userScrolledUp && COOLEMS.isStreaming) {
            showScrollIndicator();
        }
    } else if (data.type === 'thinking') {
        // Replay guard (same rule as content frames) - evaluated against THIS frame's owner.
        if (!_replayShouldRenderAssistantFrame(convId)) return;

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
            // (2026-10-23 live steering) a new assistant SEGMENT begins after a steering
            // bubble / tool row / user message. Reset THIS conversation's accumulated content
            // buffer so the next chunks render only this segment - not the previous answer again.
            st.streamingBuffer = '';
            void appendMessage('assistant', '', true).catch(e => console.error('[WS] appendMessage failed:', e));
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
        ApprovalDialog.appendCard(data);   // (2026-07-18) unified consent dialog - one code path for all kinds
    } else if (data.type === 'steer_received') {
        // (2026-10-23 live steering) the backend queued the message for the running loop.
        if (typeof markSteerBubble === 'function') markSteerBubble(data.steer_id, 'received', data);
    } else if (data.type === 'steer_applied') {
        // The ReAct loop actually injected it into its next provider call.
        if (typeof markSteerBubble === 'function') markSteerBubble(data.steer_id, 'applied', data);
    } else if (data.type === 'steer_rejected') {
        // Too late / not an agentic turn / turn stopped before the next boundary.
        if (typeof markSteerBubble === 'function') markSteerBubble(data.steer_id, 'rejected', data);
    } else if (data.type === 'system') {
        showNotification(data.content, 'info');
    } else if (data.type === 'turn_stats') {
        // (2026-10-10) final loop-stats line for the finished agentic turn - splice it
        // above 'Agentic AI: TASK DONE' in the answer bubble (idempotent, replay-safe).
        applyTurnStatsLine(data.line, convId);
    } else if (data.type === 'token_stats') {
        // LIVE token stats pushed from server after every chat_stream() call.
        // (2026-11-24 mix fix) the status bar is a SINGLE view-level element: only frames
        // owned by the workspace on screen may write it - a background job's stats used to
        // overwrite the visible one ("stats jumping between workspaces").
        if (!convId || convId === COOLEMS.currentConversation) updateHistoryStatusBar(data);
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
        void appendMessage('assistant', data.content, false).catch(e => console.error('[WS] appendMessage failed:', e));
        // (2026-11-24 mix fix) reset only THIS conversation's streaming state; the shared
        // send button reflects the view on screen - a background error must not clear it.
        st.streamingBuffer = '';
        if (!convId || convId === COOLEMS.currentConversation) {
            COOLEMS.isStreaming = false; setButtonState(false);
        }
        updateConnectionStatus('error', 'Provider Error');
    } else if (data.type === 'done') {
        // (2026-11-24 mix fix) 'done' settles the conversation that OWNS it. The send button,
        // file panel and scroll state belong to the view on screen - a background workspace's
        // completion must not clear them while another job is visibly running.
        st.streamingBuffer = '';
        if (!convId || convId === COOLEMS.currentConversation) {
            COOLEMS.isStreaming = false; setButtonState(false);
        }
        const lastMsg = chatContainer.lastElementChild;
        if (lastMsg && (!convId || convId === COOLEMS.currentConversation)) {
            delete lastMsg.dataset.streaming;
            lastMsg.querySelectorAll('.streaming-code').forEach(el => el.classList.remove('streaming-code'));
        }
        // (2026-10-10) flush a parked final-stats line if the 'turn_stats' frame arrived
        // before this turn's answer bubble existed (or during replay). Per-conv, idempotent.
        if (st.pendingTurnStatsLine) {
            const _pl = st.pendingTurnStatsLine;
            st.pendingTurnStatsLine = '';
            applyTurnStatsLine(_pl, convId);
        }
        if (!convId || convId === COOLEMS.currentConversation) {
            if (COOLEMS.filesPanelOpen && typeof loadWorkingRootFiles === 'function') loadWorkingRootFiles();
            COOLEMS.userScrolledUp = false;
            COOLEMS.autoScrollEnabled = true;
            hideScrollIndicator();
        }
    } else if (data.type === 'search_web') {
        const d = document.createElement('div'); d.className = 'search-web-link';
        d.innerHTML = `<a href="#" onclick="triggerWebSearch('${escapeHtml(data.query)}'); return false;">🔍 Search same question on internet</a>`;
        chatContainer.appendChild(d);
        if (COOLEMS.autoScrollEnabled && !COOLEMS.userScrolledUp) scrollToBottom();
    } else if (data.type === 'error') {
        logError('Server error:', data.content);
        showNotification(data.content, 'error');
        // (2026-11-24 mix fix) only the view on screen owns the button/steer state.
        if (!convId || convId === COOLEMS.currentConversation) {
            COOLEMS.isStreaming = false;
            COOLEMS.streamingAgentMode = false;
            setButtonState(false);
            if (typeof settlePendingSteerBubbles === 'function') {
                settlePendingSteerBubbles('the response failed before this could be applied.');
            }
        }
    }
}

// ===== UNIFIED CONSENT DIALOG (2026-07-18) ===================================
// All four consent card kinds (python / url / file / network) are now rendered by the
// shared ApprovalDialog object in static/ui/approval-dialog.js - ONE code path, a
// kind->config table (icon + short title + labels), an "Explain request" button on
// every card and per-kind auto-approve timers from COOLEMS.runtimeSettings.approvals.
// The old inline functions below are kept ONLY as compatibility shims.

function appendExecApprovalCard(data) { ApprovalDialog.appendCard(data); }
function submitExecApproval(data, btn) { ApprovalDialog.submitDecision(data, btn); }
async function startExecAutoApproveCountdown(card, data) { return ApprovalDialog.startAutoApproveCountdown(card, data); }

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
            uiMessage = `Routing model...`;
            break;
        case 'reloading':
            uiStatus = 'connecting';
            uiMessage = `Checking the swarm (${modelName})`;
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
                // (2026-10 fleet) NO reload: the SERVER confirms the swarm serves this
                // model, and each chat request is routed to a matching node.
                if (message) log('[MODEL ROUTING] ' + message);
                showNotification('\u2713 ' + (modelName ? modelName + ' — routed' : 'Model routed'), 'success');
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
        showNotification('No active workspace to reconnect to', 'warning');
    }
};
