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
    COOLEMS.thinkingPending = '';

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

// (2026-11-08 loop-history UI) appendGeneratedImage() and appendRecoveryWarning() are
// GONE: a generated image is a row of its turn block (CycleBlock.addImage) and a recovery
// / provider error is a notice row (or a .cycle-system node when no block owns it).
// The old flat bubble renderers were removed with the conversational UI.

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
    states: {},   // convId -> { streamingBuffer, thinkingPending, inReplay, baselineAssistant, pendingTurnStatsLine }

    /** Get (or lazily create) the render state of one conversation. */
    get(convId) {
        if (!convId) return null;
        let s = this.states[convId];
        if (!s) {
            s = { streamingBuffer: '', thinkingPending: '', inReplay: false, baselineAssistant: 0, pendingTurnStatsLine: '' };
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
                return s ? s[key] : (key === 'streamingBuffer' || key === 'pendingTurnStatsLine'
                    || key === 'thinkingPending' ? '' : 0);
            },
            set: v => {
                if (!COOLEMS.currentConversation) return;   // no view - nothing to bind to
                ChatStreamState.get(COOLEMS.currentConversation)[key] = v;
            }
        });
    }
    alias('streamingBuffer', 'streamingBuffer');
    alias('thinkingPending', 'thinkingPending');
    alias('_inReplay', 'inReplay');
    alias('_baselineAssistant', 'baselineAssistant');
    alias('_pendingTurnStatsLine', 'pendingTurnStatsLine');
})();

// ===== CYCLE-BLOCK REPLAY GUARD (2026-11-08 loop-history UI) =====================
// The old bubble-count baseline guarded assistant bubbles. The new view renders ONE
// collapsed block per agentic turn, so the guard is per TURN: a replayed frame whose
// turn_id already has a block in this view is a duplicate and must be dropped, and a
// replay of a turn that finished while we were away is dropped too (its history comes
// from turn_log via conversation.js rebuild, not from frames).
function _replayShouldRenderCycleFrame(ownerConvId, turnId) {
    const convId = ownerConvId || COOLEMS.currentConversation;
    const st0 = convId ? ChatStreamState.get(convId) : null;
    if (!st0 || !st0.inReplay) return true;                    // live frame - always render
    const st = (typeof ChatSocketPool !== 'undefined' && convId)
        ? (ChatSocketPool.statuses[convId] || 'idle') : 'idle';
    if (st === 'idle') return false;                           // turn finished while away - DB rebuild owns it
    if (typeof CycleBlock === 'undefined') return false;
    if (turnId && CycleBlock.get(convId, turnId)) return false; // block already rendered live
    return !CycleBlock.current(convId);                        // no open block yet -> first frame of the turn
}

/** The block a frame belongs to: turn_id first, else the turn running in this workspace. */
function _cycleTarget(convId, data) {
    if (typeof CycleBlock === 'undefined' || !convId) return null;
    const turnId = (data && data.turn_id) || '';
    return turnId ? CycleBlock.target(convId, turnId) : CycleBlock.current(convId);
}

// ===== NORMAL-MODE THINKING SURFACE (2026-12 thinking levels) =====================
// A one-shot (non-agentic) turn has no cycle block, so reasoning used to be dropped on the
// floor. It now renders in the answer bubble as a collapsible panel, created ONLY when
// reasoning frames actually arrive - the SERVER sends them only when the user picked a
// thinking level, so a turn with thinking off looks exactly as it did before.

/** The streaming assistant bubble of a normal turn, or null when it does not exist yet. */
function _streamingBubble(chatContainer) {
    const last = chatContainer.lastElementChild;
    if (last && last.classList.contains('assistant') && last.dataset.streaming) return last;
    return null;
}

/** Paint/refresh the reasoning panel inside one answer bubble. Idempotent; grows as chunks
    arrive. ``done`` (the first answer chunk) stops the pulse and renames the header, because
    reasoning is over at that point. */
function _paintNormalThinking(bubble, st, done = false) {
    const mc = bubble.querySelector('.message-content');
    if (!mc) return;
    const text = (st && st.thinkingPending) ? String(st.thinkingPending) : '';
    if (!text) return;
    let blk = mc.querySelector('.thinking-block');
    if (!blk) {
        blk = document.createElement('div');
        blk.className = 'thinking-block';
        const head = document.createElement('div');
        head.className = 'thinking-header';
        head.textContent = 'Thinking';
        head.onclick = () => blk.classList.toggle('collapsed');
        const body = document.createElement('div');
        body.className = 'thinking-content';
        blk.appendChild(head);
        blk.appendChild(body);
        mc.insertBefore(blk, mc.querySelector('.content-text') || null);
    }
    const body = blk.querySelector('.thinking-content');
    if (body) body.textContent = text;
    const head = blk.querySelector('.thinking-header');
    if (done) {
        blk.classList.add('done');
        if (head) head.textContent = 'Thought process';
    }
}

// (2026-11-08 loop-history UI) FINAL TURN STATS: the old code spliced a stats line into
// the answer bubble. The new view shows turn facts on the block's collapsed head, so this
// folds the same numbers into that head (generated tokens + tok/s) instead of editing text.
// Idempotent + replay-safe, and per-conversation: only the block of the frame's OWN turn
// is touched, so a background workspace's stats can never rewrite the visible block.
function applyTurnStatsLine(line, ownerConvId, turnId) {
    if (!line || typeof line !== 'string') return;
    const convId = ownerConvId || COOLEMS.currentConversation;
    const block = _cycleTarget(convId, { turn_id: turnId });
    if (!block) return;                       // no block on screen for this turn - nothing to fold
    const gen = /([\d.]+)K gen/.exec(line);
    if (gen) block.generated = Math.max(block.generated, parseFloat(gen[1]) * 1024);
    const speed = /([\d.]+) tok\/s/.exec(line);
    if (speed) block.speed = parseFloat(speed[1]);
    CycleBlock._renderSummary(block);
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

    // ===== LOOP-HISTORY BLOCKS (2026-11-08) ===========================================
    // An agentic turn is ONE collapsed block, not a stream of bubbles. Every frame below
    // resolves its block through _cycleTarget(), which refuses a turn_id owned by another
    // workspace - so frames of workspace A can never land in workspace B's block.

    if (data.type === 'cycle_start') {
        if (!_replayShouldRenderCycleFrame(convId, data.turn_id)) return;
        CycleBlock.open(convId, data.turn_id, data, null, true);
        // A steering message typed in the first instants of the turn (or while this view
        // was being rebuilt) had no block to land in - paint those rows now.
        if (typeof paintPendingSteers === 'function') paintPendingSteers(convId, data.turn_id);
        return;
    }

    if (data.type === 'cycle_end') {
        // The block settles BEFORE 'done' arrives (backend order). A replayed cycle_end whose
        // block is gone (turn finished while we were away) rebuilds it from the payload.
        const payload = data.payload || {};
        const block = _cycleTarget(convId, data);
        // Paint any steer row of this turn that had no block yet (typed before cycle_start
        // or while the view was away) BEFORE settling - a settled block accepts no rows.
        if (block && typeof paintPendingSteers === 'function') paintPendingSteers(convId, data.turn_id);
        if (block) {
            CycleBlock.settle(block, (payload.final && payload.final.text) || '',
                              !!(payload.final && payload.final.stopped));
        } else if (data.turn_id && payload.turn_id) {
            CycleShell.rebuild(convId, { id: data.turn_id, payload: payload }, null);
        }
        return;
    }

    if (data.type === 'cycle_step') {
        const block = _cycleTarget(convId, data);
        if (block) CycleBlock.addStep(block, data);
        return;
    }
    if (data.type === 'cycle_speech') {
        const block = _cycleTarget(convId, data);
        if (block) CycleBlock.addSpeech(block, data);
        return;
    }
    if (data.type === 'cycle_tool') {
        const block = _cycleTarget(convId, data);
        if (block) CycleBlock.addTool(block, data);
        return;
    }
    if (data.type === 'cycle_consent_pending') {
        const block = _cycleTarget(convId, data);
        if (block) CycleBlock.addCard(block, data);
        return;
    }
    if (data.type === 'cycle_consent') {
        // The card is transient: remove it, keep the decision as a row inside the block.
        // (2026-11-08 user rule) a dialog must disappear on ANY decision or timer auto-allow,
        // so a fold that finds no row still removes the card by request id (it may have been
        // rendered as a plain chat bubble because no block owned the frame).
        const block = _cycleTarget(convId, data);
        if (!block || !CycleBlock.settleCard(block, data.request_id, data.decision)) {
            CycleShell.removeCard(data.request_id);
        }
        return;
    }
    if (data.type === 'cycle_grant') {
        const block = _cycleTarget(convId, data);
        if (block) CycleBlock.addGrant(block, data);
        return;
    }
    if (data.type === 'cycle_feedback') {
        const block = _cycleTarget(convId, data);
        if (block) CycleBlock.addNotice(block, data);
        return;
    }
    if (data.type === 'cycle_steer') {
        // The recorder's row for a steering message: a fact of the turn, painted INSIDE the
        // block at the moment the user typed it. Never a chat-level message.
        if (typeof handleSteerFrame === 'function') handleSteerFrame(data, convId);
        return;
    }

    // ===== RUNTIME lane (2026-12 subagents / background jobs / questions) ============
    // A notice is the RUNTIME speaking (a child settled, a job settled, the user answered
    // a question the AI asked). It is never a chat-level message: it is a row of the turn's
    // block, correlated by notice_id, exactly like steering. Children/jobs are rows too, so
    // the user can see delegated work while it runs.

    if (data.type === 'cycle_notice') {
        const block = _cycleTarget(convId, data);
        if (block) CycleBlock.setNotice(block, Object.assign({ state: 'applied' }, data));
        return;
    }
    if (data.type === 'notice_queued') {
        const block = _cycleTarget(convId, data);
        if (block) CycleBlock.setNotice(block, Object.assign({ state: 'queued' }, data));
        return;
    }
    if (data.type === 'notice_applied') {
        const block = _cycleTarget(convId, data);
        if (block) CycleBlock.setNotice(block, Object.assign({ state: 'applied' }, data));
        return;
    }
    if (data.type === 'notice_deferred') {
        // No wake budget left (or quiet-wake mode): the notice waits for the user's next
        // message. With no block on screen it must still be visible as a system notice.
        const block = _cycleTarget(convId, data);
        if (block) {
            CycleBlock.setNotice(block, Object.assign({ state: 'deferred' }, data));
        } else {
            showNotification('A background result is waiting: ' + _oneLine(data.content || ''), 'info');
        }
        return;
    }
    if (data.type === 'cycle_child') {
        const block = _cycleTarget(convId, data);
        if (block) CycleBlock.setChild(block, data);
        return;
    }
    if (data.type === 'cycle_child_output') {
        // (2026-11-30) A delegated child's streamed text. It paints in THAT child's row,
        // never in the parent's answer strip: a child's words are not the parent's answer,
        // and forwarding them as `content` frames used to write them into the parent's
        // bubble (and its `done` frame used to settle the parent's block mid-turn).
        const block = _cycleTarget(convId, data);
        if (block) CycleBlock.appendChildOutput(block, data);
        return;
    }
    if (data.type === 'cycle_job') {
        const block = _cycleTarget(convId, data);
        if (block) CycleBlock.setJob(block, data);
        return;
    }
    if (data.type === 'cycle_job_output') {
        const block = _cycleTarget(convId, data);
        if (block) CycleBlock.appendJobOutput(block, data);
        return;
    }
    if (data.type === 'cycle_question_pending') {
        const block = _cycleTarget(convId, data);
        if (block) CycleBlock.setQuestion(block, Object.assign({ pending: true }, data));
        return;
    }
    if (data.type === 'cycle_question') {
        // The card is transient: an answered OR unanswered decision removes it. With no
        // block row found, still remove the card by request id (it may be a chat bubble).
        const block = _cycleTarget(convId, data);
        if (!block || !CycleBlock.settleQuestion(block, data.request_id, data.decision)) {
            CycleShell.removeCard(data.request_id);
        }
        return;
    }
    if (data.type === 'cycle_final') {
        // The answer row opened - the final AI response is being delivered. The last-tool
        // strip is live-work only, so it is REMOVED here (user rule 2026-11-08, revised:
        // visible while a tool runs, gone once the answer starts being delivered).
        // settle() drops it too, for turns that end without this frame.
        const block = _cycleTarget(convId, data);
        if (block) CycleBlock.clearLastTool(block);
        return;
    }

    // ===== AI stream ============================================================
    // Inside a block: chunks go to that turn's AI-response strip (visible while collapsed).
    // Outside a block (a normal one-shot turn): the plain streaming answer bubble.
    // (2026-12 thinking) A normal turn now HAS a thinking surface: reasoning is painted in a
    // collapsible .thinking-block above the answer text, but only when the user picked a
    // thinking level - the SERVER sends reasoning frames only then. Agentic turns keep using
    // CycleBlock's per-step strip.

    if (data.type === 'content' || data.type === 'thinking') {
        const kind = data.type === 'thinking' ? 'thinking' : 'answer';
        const block = _cycleTarget(convId, data);
        if (block) {
            if (!_replayShouldRenderCycleFrame(convId, data.turn_id)) return;
            // An ANSWER chunk means the AI is delivering its response. The last-tool strip
            // is live work-in-progress, so it goes away HERE (user rule 2026-11-08: visible
            // while a tool runs, gone once the answer starts being delivered). Thinking
            // chunks are reasoning, not the delivered answer, so they leave the strip up.
            // A later tool_start re-opens it - a tool is running again.
            if (kind === 'answer') CycleBlock.clearLastTool(block);
            CycleBlock.addStreamChunk(block, kind, data.content || '');
            if (COOLEMS.autoScrollEnabled && !COOLEMS.userScrolledUp && wasNearBottom) scrollToBottom();
            else if (COOLEMS.userScrolledUp && COOLEMS.isStreaming) showScrollIndicator();
            return;
        }
        if (!_replayShouldRenderCycleFrame(convId, data.turn_id)) return;

        if (kind === 'thinking') {
            // Buffer first: the answer bubble may not exist yet (reasoning arrives before the
            // first answer chunk), and appendMessage is async. Painted once the bubble is there.
            st.thinkingPending += (data.content || '');
            const bubble = _streamingBubble(chatContainer);
            if (!bubble) {
                // Reasoning arrives before the first answer chunk, so the bubble may not
                // exist yet: open the streaming answer bubble, then paint into it once the
                // async append has actually landed in the DOM.
                void appendMessage('assistant', '', true)
                    .then(() => {
                        const b = _streamingBubble(chatContainer);
                        if (b) _paintNormalThinking(b, st);
                    })
                    .catch(e => console.error('[WS] appendMessage failed:', e));
                return;
            }
            _paintNormalThinking(bubble, st);
            if (COOLEMS.autoScrollEnabled && !COOLEMS.userScrolledUp && wasNearBottom) scrollToBottom();
            else if (COOLEMS.userScrolledUp && COOLEMS.isStreaming) showScrollIndicator();
            return;
        }

        st.streamingBuffer += data.content;
        let lastMsg = chatContainer.lastElementChild;
        if (lastMsg && lastMsg.classList.contains('assistant') && lastMsg.dataset.streaming) {
            const contentDiv = lastMsg.querySelector('.content-text');
            if (contentDiv) contentDiv.innerHTML = parseMarkdown(st.streamingBuffer, true);
            const messageContent = lastMsg.querySelector('.message-content');
            if (messageContent) messageContent.setAttribute('data-raw-content', st.streamingBuffer);
            // The answer has started: reasoning is finished, so the panel settles (no pulse,
            // renamed to "Thought process").
            _paintNormalThinking(lastMsg, st, true);
        } else {
            st.streamingBuffer = data.content;
            // SECURITY fix 2026-10-05: appendMessage is async - fire-and-forget in this sync handler.
            void appendMessage('assistant', st.streamingBuffer, true)
                // (2026-12 thinking) If reasoning arrived while the bubble did not exist yet,
                // this is the path that created it - paint the buffered reasoning too (a no-op
                // when nothing was buffered, which is the ordinary no-thinking case).
                .then(() => {
                    const b = _streamingBubble(chatContainer);
                    if (b) _paintNormalThinking(b, st, true);
                })
                .catch(e => console.error('[WS] appendMessage failed:', e));
        }
        if (COOLEMS.autoScrollEnabled && !COOLEMS.userScrolledUp && wasNearBottom) scrollToBottom();
        else if (COOLEMS.userScrolledUp && COOLEMS.isStreaming) showScrollIndicator();
        return;
    }

    if (data.type === 'tool_start') {
        // Tool row + the live last-tool strip (live timer, input 5 / output 4). The strip
        // appears with the FIRST running tool of the turn and is removed once the final
        // answer starts being delivered (cycle_final / settle).
        const block = _cycleTarget(convId, data);
        if (block) CycleBlock.addToolStart(block, data);
        return;
    }

    if (data.type === 'tool_end') {
        const block = _cycleTarget(convId, data);
        if (block) {
            const out = String(data.output || '');
            // Same rule as logic/tool_executor.result_is_error(): only the HEAD marks a
            // failure, so a report that merely mentions "error" stays honest as ok.
            const head = out.trimStart().slice(0, 12).toLowerCase();
            const failed = head.startsWith('error:') || head.startsWith('error ');
            CycleBlock.addTool(block, {
                step_no: block.counts.turns,
                at_ts: data.finished_at || (Date.now() / 1000),
                tool: {
                    name: data.tool, ok: !failed,
                    output: out, call_id: data.call_id || '',
                    duration_ms: data.duration_ms, args: _asText(data.input),
                    // The file identity belongs on the info line, next to the tool name
                    // (user request 2026-11-08); the input frame is the content only.
                    target: data.target || '',
                },
            });
        }
        return;
    }

    if (data.type === 'image_generated') {
        const block = _cycleTarget(convId, data);
        if (block) CycleBlock.addImage(block, data);
        return;
    }

    // (2026-12) the recorder's persisted identity row for a generated image. The live
    // 'image_generated' frame already painted the pixels for THIS call - addImage() dedupes
    // by call_id, so this only paints when the live frame missed the block (replay / late
    // attach). A reload never sees frames: it rebuilds from the payload instead.
    if (data.type === 'cycle_image') {
        const block = _cycleTarget(convId, data);
        if (block) CycleBlock.addImage(block, data);
        return;
    }

    if (data.type === 'exec_approval_request') {
        // Consent card. Inside a block it mounts in that block's live strip (answerable while
        // collapsed) and disappears once answered; a card with no owning block falls back to
        // the chat container so the user can still decide.
        const block = _cycleTarget(convId, data);
        if (block) CycleBlock.addCard(block, data);
        else ApprovalDialog.appendCard(data);
        return;
    }

    // ===== LIVE STEERING (2026-10-23; block rows since 2026-11-08) ==================
    // A steering message is LOOP WORK: it never paints as a chat-level user message.
    // These three ack frames settle the ROW inside the running turn's block
    // (cycle-block.js CycleBlock.setSteer), placed at the moment the user typed it.
    // `cycle_steer` (handled above with the cycle_* frames) is the recorder's own row.
    if (data.type === 'steer_received') {
        // (2026-10-23 live steering) the backend queued the message for the running loop.
        if (typeof handleSteerFrame === 'function') handleSteerFrame(data, convId);
        return;
    }
    if (data.type === 'steer_applied') {
        // The ReAct loop actually injected it into its next provider call.
        if (typeof handleSteerFrame === 'function') handleSteerFrame(data, convId);
        return;
    }
    if (data.type === 'steer_rejected') {
        // Too late / not an agentic turn / turn stopped before the next boundary.
        if (typeof handleSteerFrame === 'function') handleSteerFrame(data, convId);
        return;
    }

    if (data.type === 'system') {
        showNotification(data.content, 'info');
        return;
    }

    if (data.type === 'turn_stats') {
        applyTurnStatsLine(data.line, convId, data.turn_id);
        return;
    }

    if (data.type === 'token_stats') {
        // LIVE token stats pushed from server after every chat_stream() call.
        // (2026-11-24 mix fix) the status bar is a SINGLE view-level element: only frames
        // owned by the workspace on screen may write it - a background job's stats used to
        // overwrite the visible one ("stats jumping between workspaces").
        if (!convId || convId === COOLEMS.currentConversation) {
            updateHistoryStatusBar(data);
            const block = _cycleTarget(convId, data);
            if (block) {
                if (data.session_generated_tokens) block.generated = Math.max(block.generated, data.session_generated_tokens);
                if (data.generation_speed) block.speed = data.generation_speed;
                CycleBlock._renderSummary(block);
            }
        }
        return;
    }

    if (data.type === 'recovery_warning' || data.type === 'provider_error' || data.type === 'ollama_error') {
        // Loop-level facts land inside the block; with no block they read as a system notice.
        const text = data.message || data.content || '';
        const block = _cycleTarget(convId, data);
        if (block) {
            CycleBlock.addNotice(block, { text: text, at_ts: Date.now() / 1000, level: 'error' });
        } else {
            CycleShell.systemNotice(text, data.status === 'success' ? 'warning' : 'error');
        }
        if (data.type === 'provider_error' || data.type === 'ollama_error') {
            st.streamingBuffer = '';
        st.thinkingPending = '';
            if (!convId || convId === COOLEMS.currentConversation) {
                COOLEMS.isStreaming = false;
                setButtonState(false);
                updateConnectionStatus('error', 'Provider Error');
            }
        }
        return;
    }

    if (data.type === 'model_switch_status') {
        handleModelSwitchStatus(data);
        return;
    }

    if (data.type === 'done') {
        // (2026-11-24 mix fix) 'done' settles the conversation that OWNS it. The send button,
        // file panel and scroll state belong to the view on screen - a background workspace's
        // completion must not clear them while another job is visibly running.
        const block = CycleBlock.current(convId);
        if (block) {
            // Safety net: cycle_end normally settles first. settle() removes the last-tool
            // strip - it is live work-in-progress only, so it never outlives the answer
            // (normally it is already gone at the first answer chunk / cycle_final).
            CycleBlock.settle(block, block.finalText || '', false);
        }
        st.streamingBuffer = '';
        st.thinkingPending = '';
        if (!convId || convId === COOLEMS.currentConversation) {
            COOLEMS.isStreaming = false;
            setButtonState(false);
            const lastMsg = chatContainer.lastElementChild;
            if (lastMsg && lastMsg.dataset.streaming) {
                delete lastMsg.dataset.streaming;
                lastMsg.querySelectorAll('.streaming-code').forEach(el => el.classList.remove('streaming-code'));
            }
            if (COOLEMS.filesPanelOpen && typeof loadWorkingRootFiles === 'function') loadWorkingRootFiles();
            COOLEMS.userScrolledUp = false;
            COOLEMS.autoScrollEnabled = true;
            hideScrollIndicator();
        }
        return;
    }

    if (data.type === 'search_web') {
        const d = document.createElement('div'); d.className = 'search-web-link';
        d.innerHTML = `<a href="#" onclick="triggerWebSearch('${escapeHtml(data.query)}'); return false;">\u{1F50D} Search same question on internet</a>`;
        chatContainer.appendChild(d);
        if (COOLEMS.autoScrollEnabled && !COOLEMS.userScrolledUp) scrollToBottom();
        return;
    }

    if (data.type === 'error') {
        logError('Server error:', data.content);
        showNotification(data.content, 'error');
        const block = CycleBlock.current(convId);
        if (block) {
            CycleBlock.addNotice(block, { text: String(data.content || ''), at_ts: Date.now() / 1000, kind: 'error' });
            // settle() drops the last-tool strip: the turn ended (error), so it is no longer live.
            CycleBlock.settle(block, block.finalText || '', true);
        }
        // (2026-11-24 mix fix) only the view on screen owns the button/steer state.
        if (!convId || convId === COOLEMS.currentConversation) {
            COOLEMS.isStreaming = false;
            COOLEMS.streamingAgentMode = false;
            setButtonState(false);
            if (typeof settlePendingSteerRows === 'function') {
                settlePendingSteerRows('the response failed before this could be applied.');
            }
        }
    }
}

// ===== CONSENT CARDS (2026-07-18 unified dialog, 2026-11-08 block-mounted) ==========
// ApprovalDialog (static/ui/approval-dialog.js) renders every card kind from one config
// table. websocket.js now passes the owning block's live strip as the mount, so a card
// lives inside its turn instead of the chat stream. These shims keep external callers
// (index.html inline handlers) working.

function appendExecApprovalCard(data, mount) { ApprovalDialog.appendCard(data, mount); }
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
    // (2026-11-08) the last AI text is either the newest block's answer strip or the last
    // plain assistant bubble - the flat transcript no longer exists.
    const container = document.getElementById('chatContainer');
    let lastAiResponse = '';
    const blocks = container.querySelectorAll('.cycle-block');
    for (let i = blocks.length - 1; i >= 0; i--) {
        const answers = blocks[i].querySelectorAll('.cycle-ai-answer');
        if (answers.length) { lastAiResponse = answers[answers.length - 1].textContent; break; }
    }
    if (!lastAiResponse) {
        const messages = container.querySelectorAll('.message');
        for (let i = messages.length - 1; i >= 0; i--) {
            if (messages[i].classList.contains('assistant')) {
                lastAiResponse = messages[i].textContent;
                break;
            }
        }
    }
    COOLEMS.isStreaming = false;
    COOLEMS.streamingBuffer = '';
    COOLEMS.thinkingPending = '';
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
