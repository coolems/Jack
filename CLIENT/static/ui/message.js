/**
 * message.js - Message rendering, markdown parsing, copy, edit functionality.
 * Uses COOLEMS shared state from app.js.
 */

/**
 * (2026-11-08) ONE clipboard path for every copy button: the async Clipboard API when the
 * page is a secure context, the hidden-textarea fallback otherwise. Shared by the message
 * bubbles and the turn-block Copy chip so both behave identically.
 */
async function copyTextToClipboard(raw) {
    if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(raw); return; }
    const ta = document.createElement('textarea');
    ta.value = raw; ta.style.cssText = 'position:fixed;left:-999999px';
    document.body.appendChild(ta); ta.select();
    if (!document.execCommand('copy')) throw new Error('copy failed');
    document.body.removeChild(ta);
}

function _markCopied(btn) {
    const orig = btn.innerHTML;
    btn.classList.add('copied');
    btn.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12"></polyline></svg> Copied!';
    setTimeout(() => { btn.classList.remove('copied'); btn.innerHTML = orig; }, 2000);
}

async function copyMessage(btn) {
    // (2026-11-08) works on both shapes: a user prompt (.cycle-user-bubble) and a plain
    // assistant bubble (.message-content).
    const holder = btn.closest('.cycle-user-bubble') || btn.closest('.message-content');
    const raw = holder ? holder.getAttribute('data-raw-content') : null;
    if (!raw) { showNotification('Nothing to copy', 'warning'); return; }
    try {
        await copyTextToClipboard(raw);
        _markCopied(btn);
    } catch (e) { showNotification('Failed to copy', 'error'); }
}

/**
 * (2026-11-08, user request "copy options lost") Copy the AI answer of one turn block.
 * Since the loop-history UI the AI's text lives in the block's AI strip (.cycle-ai-answer
 * segments), not in a bubble - so the strip's Copy chip reads those segments' RAW text
 * (dataset.raw, protocol markers scrubbed via scrubAiText) and joins the segments of this
 * turn. Raw text, not innerHTML: the clipboard gets the real markdown, not rendered markup.
 */
async function copyCycleAnswer(btn) {
    const block = btn.closest('.cycle-block');
    const strip = block ? block.querySelector('.cycle-ai') : null;
    if (!strip) { showNotification('Nothing to copy', 'warning'); return; }
    const scrub = (typeof scrubAiText === 'function') ? scrubAiText : (t => t);
    const parts = Array.from(strip.querySelectorAll('.cycle-ai-answer'))
        .map(el => scrub(el.dataset.raw || ''))
        .filter(Boolean);
    const raw = parts.join('\n\n');
    if (!raw) { showNotification('This turn has no answer text yet', 'warning'); return; }
    try {
        await copyTextToClipboard(raw);
        _markCopied(btn);
    } catch (e) { showNotification('Failed to copy', 'error'); }
}



function startEditMessage(btn) {
    // (2026-11-08) the user prompt is a .cycle-user node now; the old .message shape is
    // still supported so an edit box works in either view.
    const messageDiv = btn.closest('.cycle-user') || btn.closest('.message');
    const messageContent = btn.closest('.cycle-user-bubble') || btn.closest('.message-content');
    const rawContent = messageContent.getAttribute('data-raw-content');
    if (!rawContent) { showNotification('Cannot edit this message', 'warning'); return; }
    if (COOLEMS.isStreaming) { showNotification('Please wait', 'warning'); return; }
    messageDiv.classList.add('editing');
    const textarea = document.createElement('textarea'); textarea.className = 'edit-textarea'; textarea.rows = 3; textarea.value = rawContent;
    const actionsDiv = document.createElement('div'); actionsDiv.className = 'edit-actions';
    actionsDiv.innerHTML = '<button class="edit-save-btn" onclick="saveEditMessage(this)"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="margin-right:4px"><polyline points="20 6 9 17 4 12"></polyline></svg> Save & Submit</button><button class="edit-cancel-btn" onclick="cancelEditMessage(this)">Cancel</button>';
    const contentText = messageContent.querySelector('.content-text');
    contentText.parentNode.insertBefore(textarea, contentText);
    contentText.parentNode.insertBefore(actionsDiv, textarea.nextSibling);
    // Auto-grow the edit box to fit content (a few lines up, then scroll), and re-fit on window resize
    const autoGrowEdit = () => { textarea.style.height = 'auto'; textarea.style.height = Math.min(textarea.scrollHeight, 320) + 'px'; };
    requestAnimationFrame(autoGrowEdit); setTimeout(autoGrowEdit, 50);
    const onWinResize = () => autoGrowEdit(); window.addEventListener('resize', onWinResize);
    messageDiv._editCleanup = () => { window.removeEventListener('resize', onWinResize); const t2 = messageContent.querySelector('.edit-textarea'); const a2 = messageContent.querySelector('.edit-actions'); if (t2) t2.remove(); if (a2) a2.remove(); };
    textarea.focus();
    textarea.setSelectionRange(textarea.value.length, textarea.value.length);
    textarea.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); saveEditMessage(actionsDiv.querySelector('.edit-save-btn')); } if (e.key === 'Escape') cancelEditMessage(actionsDiv.querySelector('.edit-cancel-btn')); });
}



function saveEditMessage(btn) {
    const messageDiv = btn.closest('.cycle-user') || btn.closest('.message');
    const messageContent = btn.closest('.cycle-user-bubble') || btn.closest('.message-content');
    const newContent = messageContent.querySelector('.edit-textarea').value.trim();
    if (!newContent) { showNotification('Message cannot be empty', 'warning'); return; }
    messageDiv.classList.remove('editing');
    const ta = messageContent.querySelector('.edit-textarea'); const act = messageContent.querySelector('.edit-actions');
    if (messageDiv._editCleanup) { messageDiv._editCleanup(); delete messageDiv._editCleanup; } else { if (ta) ta.remove(); if (act) act.remove(); }
    editAndResend(parseInt(messageDiv.dataset.messageIndex), newContent);
}



function cancelEditMessage(btn) {
    const messageDiv = btn.closest('.cycle-user') || btn.closest('.message');
    const messageContent = btn.closest('.cycle-user-bubble') || btn.closest('.message-content');
    messageDiv.classList.remove('editing');
    const ta = messageContent.querySelector('.edit-textarea'); const act = messageContent.querySelector('.edit-actions');
    if (messageDiv._editCleanup) { messageDiv._editCleanup(); delete messageDiv._editCleanup; } else { if (ta) ta.remove(); if (act) act.remove(); }
}



function parseMarkdown(text, isStreaming = false) {
    if (!text) return '';

    const placeholders = [];
    function store(html) {
        const idx = placeholders.length;
        placeholders.push(html);
        return '\x00' + idx + '\x00';
    }

    text = text.replace(/```[\w]*\n([\s\S]*?)```/g, function(match, code) {
        return store('<pre><code>' + escapeHtml(code) + '</code></pre>');
    });

    if (COOLEMS.isStreaming) {
        text = text.replace(/```[\w]*\n([\s\S]*)$/g, function(match, code) {
            return store('<pre class="streaming-code"><code>' + escapeHtml(code) + '</code></pre>');
        });
    }

    text = text.replace(/`([^`]+)`/g, function(match, code) {
        return store('<code>' + escapeHtml(code) + '</code>');
    });

    let html = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

    html = html.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/\*(.*?)\*/g, '<em>$1</em>');

    html = html.replace(/(?<!["'])((?:https?:\/\/)[^\s<>"']+(?:\.[^\s<>"']+)*)/gi, function(url) {
        let cleanUrl = url, trailing = '';
        const m = url.match(/[.,;:!?\)\]]+$/);
        if (m) { trailing = m[0]; cleanUrl = url.slice(0, -m[0].length); }
        return '<a href="' + cleanUrl + '" target="_blank" rel="noopener noreferrer" class="chat-link">' + cleanUrl + '</a>' + trailing;
    });

    html = html.split(/\n\n+/).map(function(p) {
        if (p.trim() === '' || p.indexOf('<pre>') !== -1) return p;
        return '<p>' + p.replace(/\n/g, '<br>') + '</p>';
    }).join('');

    for (let i = 0; i < placeholders.length; i++) {
        html = html.split('\x00' + i + '\x00').join(placeholders[i]);
    }

    return html;
}



function showNotification(message, type = 'info') {
    const existing = document.querySelector('.notification');
    if (existing) existing.remove();
    const div = document.createElement('div');
    div.className = 'notification notification-' + type;
    div.textContent = message;
    document.body.appendChild(div);
    setTimeout(() => { div.style.opacity = '0'; setTimeout(() => div.remove(), 300); }, 5000);
}



// ============================================================
// LIVE STEERING (2026-10-23, block rows since 2026-11-08)
// While an agentic task is running, the prompt box keeps accepting text. Pressing
// Enter sends it as a steer frame (never a new turn, never a stop).
//
// THE UI CONTRACT THE USER ASKED FOR (2026-11-08): a steering message must NEVER appear
// in the chat as a user message. It is loop work, so it enters the RUNNING turn's
// loop-history block as a row at the moment it was typed (at_ts = received_at), and its
// status line settles as the backend acks: sending -> queued -> applied / not applied.
// The row lives inside CycleBlock; this module only owns the steer_id -> row registry so
// a late ack can still settle a row whose block has already settled.
// ============================================================
const _steerRows = {};    // steer_id -> {el, convId, state, text, at_ts}
const _STEER_RANK = window._STEER_STATE_RANK || { sending: 0, received: 1, applied: 2, rejected: 2 };

/** Paint (or settle) one steering row of a turn block. Never paints a chat bubble. */
function markSteerRow(steerId, state, data) {
    data = data || {};
    if (!steerId) return false;
    const convId = data.conv_id || COOLEMS.currentConversation;
    const reason = data.message || data.reason || '';
    const text = data.text || data.content || '';
    const rec = _steerRows[steerId];

    if (rec) {
        // A late ack must never downgrade a settled row (applied/rejected are final).
        if ((_STEER_RANK[state] || 0) < (_STEER_RANK[rec.state] || 0)) return true;
        rec.state = state;
        if (text && !rec.text) rec.text = text;
        if (rec.el) {
            rec.el.dataset.steerState = state;
            const status = rec.el.querySelector('.cycle-steer-status');
            if (status && typeof _steerStatusLabel === 'function') {
                status.textContent = _steerStatusLabel(state, reason);
            }
        }
    } else if (typeof CycleBlock !== 'undefined') {
        // First frame for this steer: create the row inside the turn's block.
        const block = data.turn_id ? CycleBlock.target(convId, data.turn_id)
                                   : CycleBlock.current(convId);
        if (!block) return false;              // no block on screen for this turn yet
        const el = CycleBlock.setSteer(block, {
            steer_id: steerId, text, state, reason,
            at_ts: data.at_ts, step_no: data.step_no,
        });
        _steerRows[steerId] = { el, convId, state, text, at_ts: data.at_ts };
    }
    return true;
}

/**
 * One entry point for every steer frame: cycle_steer (recorder row at typed time),
 * steer_received (queued), steer_applied (injected), steer_rejected (never applied).
 */
function handleSteerFrame(data, convId) {
    const steerId = data.steer_id || '';
    if (!steerId) return;
    let state = 'received';
    if (data.type === 'steer_applied') state = 'applied';
    else if (data.type === 'steer_rejected') state = 'rejected';
    else if (data.type === 'cycle_steer') state = data.applied ? 'applied' : 'received';

    const ok = markSteerRow(steerId, state, Object.assign({}, data, { conv_id: convId }));

    if (state === 'rejected') {
        const reason = data.message || 'the task ended before it could be applied.';
        showNotification('Steering not applied: ' + reason, 'warning');
        // Nothing the user wrote is lost: put the text back in the prompt box when they
        // have not typed anything new.
        const input = document.getElementById('messageInput');
        const text = data.content || data.text || (_steerRows[steerId] && _steerRows[steerId].text) || '';
        if (input && !input.value.trim() && text) {
            input.value = text;
            if (typeof autoResize === 'function') autoResize(input);
            input.focus();
        }
        // A steer whose row could not be painted (no block yet) still needs a visible
        // answer - say it plainly instead of leaving the user with no feedback.
        if (!ok) showNotification('Steering message was not applied to this turn.', 'warning');
    }
}

/** Paint steering rows that arrived before their block existed (typed in the first
 *  instants of a turn, or while the workspace view was being rebuilt). */
function paintPendingSteers(convId, turnId) {
    if (typeof CycleBlock === 'undefined') return;
    const block = turnId ? CycleBlock.target(convId, turnId) : CycleBlock.current(convId);
    if (!block) return;
    for (const id of Object.keys(_steerRows)) {
        const rec = _steerRows[id];
        if (rec.el || rec.convId !== convId) continue;
        const el = CycleBlock.setSteer(block, {
            steer_id: id, text: rec.text, state: rec.state,
            at_ts: rec.at_ts, step_no: block.counts.turns,
        });
        if (el) rec.el = el;
    }
}

/** Fallback on done/error: anything still waiting in THIS workspace can no longer run.
 *  Rows belonging to a stashed/background workspace are deliberately skipped - their own
 *  frames (or the backend's steer_rejected) settle them when the user returns. */
function settlePendingSteerRows(message) {
    for (const id of Object.keys(_steerRows)) {
        const rec = _steerRows[id];
        if (!rec || rec.convId !== COOLEMS.currentConversation) continue;
        if (rec.state === 'sending' || rec.state === 'received') {
            markSteerRow(id, 'rejected', {
                conv_id: rec.convId, content: rec.text || '',
                message: message || 'the task ended before this could be applied.',
            });
        }
    }
}

/** Drop this workspace's steer rows (view dropped / workspace deleted). */
function clearSteerRows(convId) {
    for (const id of Object.keys(_steerRows)) {
        if (_steerRows[id].convId === convId) delete _steerRows[id];
    }
}

async function sendSteerMessage() {
    const input = document.getElementById('messageInput');
    const message = input ? input.value.trim() : '';
    if (!message) return;
    if (!COOLEMS.currentConversation) return;

    // streamingAgentMode is the authoritative per-turn flag. The agent toggle is a
    // fallback for the rare case where the status restore could not tell us the
    // live turn's mode - the backend still refuses steering for a normal turn.
    if (!COOLEMS.streamingAgentMode && !COOLEMS.agentMode) {
        showNotification('Steering is available while an Agent-mode task is running. Use Stop to interrupt this response.', 'warning');
        return;
    }
    if (COOLEMS.selectedFiles.length > 0) {
        showNotification('Remove attachments to steer the running task.', 'warning');
        return;
    }

    const entry = (typeof ChatSocketPool !== 'undefined')
        ? ChatSocketPool.sockets[COOLEMS.currentConversation] : null;
    const ws = entry ? entry.ws : COOLEMS.ws;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
        showNotification('Connection is not open — steering message not sent.', 'error');
        return;
    }

    const steerId = 'steer_' + Date.now().toString(36) + '_' + Math.random().toString(36).slice(2, 8);
    // The row goes into the RUNNING turn's block immediately (state 'sending'), so the
    // user sees their interjection in the loop history, not as a chat message.
    markSteerRow(steerId, 'sending', { conv_id: COOLEMS.currentConversation, content: message, at_ts: Date.now() / 1000 });
    input.value = '';
    input.style.height = 'auto';
    if (typeof autoResize === 'function') autoResize(input);

    try {
        ws.send(JSON.stringify({
            message: message,
            steer: true,
            steer_id: steerId,
            // The running turn already owns its model/mode; a steer must not
            // re-validate/override them.
            model: null,
            agent_mode: true,
            media_files: [],
            enable_thinking: COOLEMS.enableThinking
        }));
    } catch (e) {
        markSteerRow(steerId, 'rejected', {
            conv_id: COOLEMS.currentConversation, content: message,
            message: 'the connection failed while sending.',
        });
        showNotification('Steering not applied: the connection failed while sending.', 'warning');
    }
}

async function sendMessage() {
    // Clear any error bar on new send

    const input = document.getElementById('messageInput');
    const message = input.value.trim();
    if (!message && COOLEMS.selectedFiles.length === 0) return;
    if (COOLEMS.isStreaming) { showNotification('Please wait for current response...', 'warning'); return; }
    if (!COOLEMS.currentConversation) { await newChat(); await new Promise(r => setTimeout(r, 500)); }

    COOLEMS.userScrolledUp = false;
    COOLEMS.autoScrollEnabled = true;
    hideScrollIndicator();

    const fileContentsForDisplay = COOLEMS.selectedFiles.filter(f => f.content).map(f => ({ filename: f.name, content: f.content, type: 'text' }));
    // SECURITY fix 2026-10-05: appendMessage is async (media tokens minted via header auth).
    await appendMessage('user', message, false, COOLEMS.selectedFiles.map(f => f.url), fileContentsForDisplay);
    input.value = ''; input.style.height = 'auto'; document.getElementById('filePreview').innerHTML = '';
    // (2026-09-08 multi-chat UI fix) a user send is always LIVE: leave replay mode so
    // the incoming stream of THIS turn renders normally.
    COOLEMS._inReplay = false;
    // (2026-10-23 live steering) remember whether THIS turn can be steered
    COOLEMS.streamingAgentMode = !!COOLEMS.agentMode;
    COOLEMS.isStreaming = true; COOLEMS.streamingBuffer = ''; COOLEMS.thinkingPending = ''; setButtonState(true);
    const payload = {
        message,
        model: document.getElementById('modelSelector').value,
        agent_mode: COOLEMS.agentMode,
        media_files: COOLEMS.selectedFiles.map(f => f.url),
        enable_thinking: COOLEMS.enableThinking
    };
    COOLEMS.selectedFiles = [];
    // (2026-09-08 multi-chat) send on THIS chat's pooled socket; if it is not open
    // yet, wait briefly for the handshake instead of the old fixed 1s timeout.
    const _sendWs = () => ChatSocketPool.isOpen(COOLEMS.currentConversation)
        ? ChatSocketPool.sockets[COOLEMS.currentConversation].ws : null;
    if (_sendWs() && _sendWs().readyState === WebSocket.OPEN) {
        _sendWs().send(JSON.stringify(payload));
    } else {
        ChatSocketPool.ensure(COOLEMS.currentConversation);
        let _tries = 0;
        const _waitOpen = () => {
            _tries++;
            const w = _sendWs();
            if (w && w.readyState === WebSocket.OPEN) { w.send(JSON.stringify(payload)); return; }
            if (_tries >= 50) { // ~5s budget
                showNotification('Connection not available. Please retry.', 'error');
                COOLEMS.isStreaming = false; setButtonState(false);
                return;
            }
            setTimeout(_waitOpen, 100);
        };
        _waitOpen();
    }
}



// Build an authenticated inline URL for a saved media file (working_root-relative).
// SECURITY fix 2026-10-05: <img> tags cannot send auth headers, so instead of the
// master key travelling as ?api_key=...&email=..., we mint a short-lived, single-use,
// path-bound media token via header auth (window.getMediaToken, from the preview
// module) and pass ?t=<token>. The master key no longer appears in ANY URL.
async function buildMediaUrl(url) {
    const rel = String(url || '').replace(/^\/files\//, '');
    if (typeof window.getMediaToken !== 'function') throw new Error('media token helper unavailable');
    const token = await window.getMediaToken(rel);
    return '/api/download?path=' + encodeURIComponent(rel) + '&inline=true&t=' + encodeURIComponent(token);
}

// Click handler: open the full-size image in a new tab.
// SECURITY fix 2026-10-05: mint a FRESH token on click - tokens are short-lived
// (60 s TTL, max 3 uses) and the <img> only stores data-path, never a URL.
async function openMessageImage(img) {
    try {
        // The <img> carries data-path (working_root-relative, /files/-stripped by buildMediaUrl callers).
        const rel = String(img.dataset.path || '').replace(/^\/files\//, '');
        const token = await window.getMediaToken(rel);
        const u = new URL('/api/download', window.location.origin);
        u.searchParams.set('path', rel);
        u.searchParams.set('inline', 'true');
        u.searchParams.set('t', token);
        window.open(u.toString(), '_blank');
    } catch (e) {
        showNotification('Could not open image', 'error');
    }
}

// Fallback when the file is missing/unauthorized: degrade to a small file tag
// (path+name visible) instead of a broken-image icon.
function messageImageError(img) {
    const div = document.createElement('div');
    div.className = 'file-tag';
    div.textContent = '\u{1F5BC}\uFE0F ' + (img.getAttribute('data-name') || 'image');
    if (img.parentNode) img.parentNode.replaceChild(div, img);
}


async function appendMessage(role, content, streaming = false, mediaUrls = [], fileContents = null) {
    const container = document.getElementById('chatContainer');
    const welcome = document.getElementById('welcome');
    if (welcome && welcome.style.display !== 'none') welcome.style.display = 'none';

    // (2026-11-08 loop-history UI) THE ONLY chat-level message the new view keeps is the
    // user's own prompt. Everything the AI says belongs to a turn block; a normal one-shot
    // turn still renders as a plain streaming answer bubble (role assistant).
    const messageDiv = document.createElement('div');
    messageDiv.className = role === 'user' ? 'cycle-user' : 'message assistant';
    if (streaming) messageDiv.dataset.streaming = 'true';
    // (2026-11-08) THE INDEX CONTRACT for edit/resend: the index sent to
    // DELETE /messages/truncate must be the position among CHAT-LEVEL items, and the server
    // counts the same set - every DB row EXCEPT a '[STEER]' marker (folded into the block).
    // So the count must include a turn block (.cycle-block = one DB answer row), otherwise
    // an edit after a turn would cut the history at the wrong row.
    messageDiv.dataset.messageIndex =
        container.querySelectorAll('.cycle-user, .message, .cycle-block').length;
    let mediaHtml = '';

    // BUG FIX (2026-10-03): declared OUTSIDE the if-block on purpose - the
    // token loop below runs unconditionally, so a text-only message (empty
    // mediaUrls) used to hit `imageEntries is not defined` and killed the whole
    // workspace load ('Failed to load workspace').
    const imageEntries = [];
    if (mediaUrls && mediaUrls.length > 0) {
        // SECURITY (2026-08-25): /files/ is no longer public - render message images
        // through the authenticated download endpoint. History stores "/files/<name>"
        // or plain "<name>"; both normalize to a working_root-relative path.
        // SECURITY fix 2026-10-05: tokens are minted ASYNC (header auth) - the <img>
        // starts with an empty src and gets its ?t= URL once the token arrives. No
        // credential ever sits in the DOM or in a static URL.
        mediaHtml = '<div class="' + (role === 'user' ? 'cycle-user-images' : 'message-images') + '">' + mediaUrls.map(url => {
            const name = String(url).split('/').pop();
            if (url.match(/\.(jpg|jpeg|png|webp|gif)$/i)) {
                imageEntries.push({ rel: url, name });
                return '<img data-path="' + escapeHtml(url) + '" data-name="' + escapeHtml(name) + '" class="message-image message-image-pending" onclick="openMessageImage(this)" onerror="messageImageError(this)" title="Click to open full size">';
            }
            return '<div class="file-tag">' + escapeHtml(name) + '</div>';
        }).join('') + '</div>';
    }
    let fileContentHtml = '';
    if (fileContents && fileContents.length > 0) {
        const textFiles = fileContents.filter(fc => fc.type === 'text');
        if (textFiles.length > 0) {
            fileContentHtml = '<div class="file-contents">' + textFiles.map(fc => {
                const displayContent = fc.content.length > 1000 ? fc.content.substring(0, 1000) + '...' : fc.content;
                return '<div class="file-content-preview"><div class="file-header">📄 ' + escapeHtml(fc.filename) + '</div><pre class="file-content-text">' + escapeHtml(displayContent) + '</pre></div>';
            }).join('') + '</div>';
        }
    }
    const copyIcon = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg> Copy';
    const editIcon = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"></path><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"></path></svg> Edit';

    if (role === 'user') {
        // (2026-11-08) THE REPO SHAPE (git HEAD message.js): Copy and Edit are floating
        // chips anchored to the bubble - a pair at the top edge and a pair at the bottom
        // edge (.copy-top / .copy-bottom), revealed by hovering the bubble. No actions row.
        messageDiv.innerHTML =
            '<div class="cycle-user-bubble">' +
                '<button class="edit-btn copy-top" onclick="startEditMessage(this)">' + editIcon + '</button>' +
                '<button class="copy-btn copy-top" onclick="copyMessage(this)">' + copyIcon + '</button>' +
                mediaHtml + fileContentHtml +
                '<div class="content-text">' + escapeHtml(content).replace(/\n/g, '<br>') + '</div>' +
                '<button class="edit-btn copy-bottom" onclick="startEditMessage(this)">' + editIcon + '</button>' +
                '<button class="copy-btn copy-bottom" onclick="copyMessage(this)">' + copyIcon + '</button>' +
            '</div>';
        messageDiv.querySelector('.cycle-user-bubble').setAttribute('data-raw-content', content);
    } else {
        const contentHtml = parseMarkdown(content);
        messageDiv.innerHTML =
            '<div class="avatar">AI</div>' +
            '<div class="message-content">' +
                '<button class="copy-btn copy-top" onclick="copyMessage(this)">' + copyIcon + '</button>' +
                mediaHtml + fileContentHtml +
                '<div class="content-text">' + contentHtml + '</div>' +
                '<button class="copy-btn copy-bottom" onclick="copyMessage(this)">' + copyIcon + '</button>' +
            '</div>';
        messageDiv.querySelector('.message-content').setAttribute('data-raw-content', content);
    }

    // SECURITY fix 2026-10-05: mint media tokens now (appendMessage is async; every
    // caller already awaits or fire-and-forgets it). Each <img> gets its ?t= URL as
    // soon as its token arrives - no master key in any URL, ever.
    if (imageEntries.length > 0) {
        await Promise.all(imageEntries.map(async entry => {
            const imgEl = messageDiv.querySelector('img.message-image-pending[data-path="' + CSS.escape(entry.rel) + '"]');
            if (!imgEl) return;
            try {
                const token = await window.getMediaToken(String(entry.rel).replace(/^\/files\//, ''));
                imgEl.src = '/api/download?path=' + encodeURIComponent(String(entry.rel).replace(/^\/files\//, '')) + '&inline=true&t=' + encodeURIComponent(token);
            } catch (e) {
                messageImageError(imgEl);
            }
        }));
    }
    container.appendChild(messageDiv);

    if (COOLEMS.autoScrollEnabled && !COOLEMS.userScrolledUp) { scrollToBottom(); }
}

// (2026-10-23 live steering) exposed explicitly so websocket.js / app.js can use
// them regardless of classic-script load order.
window.handleSteerFrame = handleSteerFrame;
window.markSteerRow = markSteerRow;
window.paintPendingSteers = paintPendingSteers;
window.settlePendingSteerRows = settlePendingSteerRows;
window.clearSteerRows = clearSteerRows;
window.sendSteerMessage = sendSteerMessage;
// (2026-11-08) cycle-block.js renders the AI strip's Copy chip with onclick="copyCycleAnswer(this)"
// and is loaded BEFORE this file, so the handler must be an explicit global.
window.copyCycleAnswer = copyCycleAnswer;
window.copyMessage = copyMessage;

