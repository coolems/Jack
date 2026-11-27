/**
 * app.js - Core application state, shared variables, utility functions, and initialization.
 * This file is loaded FIRST and provides shared state for all other modules.
 */

// ===== DEBUG & LOGGING =====
const DEBUG = true;
function log(...args) { if (DEBUG) console.log('[COOLEMS]', ...args); }
function logError(...args) { console.error('[COOLEMS ERROR]', ...args); }

// ===== UTILITY FUNCTIONS =====
function getWebSocketUrl(path) {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${protocol}//${window.location.host}${path}`;
}

function escapeHtml(text) {
    const d = document.createElement('div');
    d.textContent = text;
    return d.innerHTML;
}

// escapeHtml() above is TEXT-context only: textContent -> innerHTML escapes
// & < > but never the quote that delimits an HTML attribute. Interpolating it
// into a double-quoted attribute (src/title/data-*/value) lets a value holding
// `"` close the attribute and turn the rest into injected markup or handlers.
// Use escapeAttr() for any value placed INSIDE an attribute value.
function escapeAttr(value) {
    return String(value == null ? '' : value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;')
        .replace(/`/g, '&#96;');
}

// For a value inside the single-quoted argument of an inline handler, e.g.
// onclick="fn('${escapeJsAttr(p)}')". Attribute-safe AND JS-string-safe:
// backslashes are doubled first so a literal \ cannot swallow the escapes added
// below; `'` becomes the literal `\'` (entity forms would be decoded before the
// handler is compiled and break the JS string); `"`/`` ` `` stay entities so the
// attribute cannot close; newlines/tabs cannot split the one-line handler.
function escapeJsAttr(value) {
    return String(value == null ? '' : value)
        .replace(/\\/g, '\\\\')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/`/g, '&#96;')
        .replace(/'/g, "\\'")
        .replace(/\n/g, '\\n')
        .replace(/\r/g, '\\r')
        .replace(/\t/g, '\\t');
}

// ===== WORKSPACE SCOPING (file panel) =====
// File-panel requests must carry the active workspace's id so the backend resolves the
// working_root PER CONVERSATION instead of reading one process-global (two tabs on two
// workspaces used to race the Files panel). fetch() calls get the id via the
// X-Conversation-Id header injected in api-key.js; anchor/<img>/<iframe> downloads
// cannot send headers, so they use convParam()/withConv()/applyConvToUrl() below.
function currentConvId() {
    return (typeof COOLEMS !== 'undefined' && COOLEMS.currentConversation)
        ? String(COOLEMS.currentConversation) : '';
}

function convParam() {
    const id = currentConvId();
    return id ? 'conversation_id=' + encodeURIComponent(id) : '';
}

function withConv(url) {
    if (typeof url !== 'string' || !url) return url;
    const p = convParam();
    if (!p) return url;
    return url + (url.indexOf('?') === -1 ? '?' : '&') + p;
}

function applyConvToUrl(url) {
    const id = currentConvId();
    if (id && url && typeof url.searchParams === 'object') {
        url.searchParams.set('conversation_id', id);
    }
    return url;
}

// ===== SHARED STATE =====
window.COOLEMS = {
    currentConversation: null,
    ws: null,
    selectedFiles: [],
    // Persisted in localStorage (see persistAgentMode in ui-controls.js)
    agentMode: (function () { try { return localStorage.getItem('coolems_agent_mode') === '1'; } catch (e) { return false; } })(),
    // Thinking level, not a boolean: '' | 'none' | 'low' | 'medium' | 'high'. Both fields
    // start EMPTY on purpose - nothing about thinking may be asserted before the model's
    // capability is known (ui-controls.js owns the whole feature: startThinkingLevel() is
    // the default/carry-over resolver, updateThinkingUI() the only writer). updateThinkingUI
    // runs on first paint, before the model catalog is fetched.
    //   thinkingDesired - what this workspace should use; '' until resolved, then the user's
    //                     pick or THINK_DEFAULT ('medium'), and only the chip's click changes it.
    //   enableThinking  - the level actually SENT, always one the selected model offers, and
    //                     '' while that is still unknown: the SERVER reads '' as "no
    //                     instruction" (schemas.normalize_thinking) and sends no thinking keys,
    //                     which every provider turns into "the model's own default" - NOT
    //                     "off". (Decision, not an accident: 'none' is no safe stand-in, see
    //                     updateThinkingUI().) A model that cannot honor the wish parks this
    //                     at 'none'/'medium' without touching thinkingDesired, so switching to
    //                     a model that can think again brings the wish back instead of
    //                     staying off.
    // COOLEMS.thinkingCaps is filled by loadModels() from the SERVER catalog, which
    // discovered the levels from the model's chat template (app/providers/llama/thinking.py),
    // so a value its template would reject is never sent (that is an HTTP 400 per request).
    enableThinking: '',
    thinkingDesired: '',
    // model_key -> capability description from the SERVER catalog ({levels, supported,...})
    thinkingCaps: {},
    // Last confirmed assignment for each workspace opened in this tab.
    convModels: Object.create(null),
    isStreaming: false,
    // (2026-10-23 live steering) true when the CURRENT in-flight turn is agentic, i.e. it
    // has a ReAct loop that can accept a mid-run steering message. Normal-mode completions
    // must be stopped instead (there is no next step to inject into).
    streamingAgentMode: false,
    wsReconnectAttempts: 0,
    filesPanelOpen: false,
    streamingBuffer: '',
    autoScrollEnabled: true,
    userScrolledUp: false,
    scrollTimeout: null,

    lastGenerationSpeed: null,       // tokens/sec from last generation (updated via WS token_stats)
    contextWindowTokens: 131072,     // default, updated from API
    tokensSentToOllama: null,        // real tokens actually sent (updated via WS token_stats)
    
    // Diff-related state
    diffMode: false,
    currentDiffFileA: null,
    currentDiffFileB: null,
};

// ===== CLIPBOARD IMAGE/PASTE HANDLER =====
function setupClipboardPaste() {
    const messageInput = document.getElementById('messageInput');
    if (!messageInput) return;
    
    messageInput.addEventListener('paste', async (e) => {
        const items = e.clipboardData.items;
        const files = [];
        
        // Collect all file items from clipboard
        for (const item of items) {
            if (item.kind === 'file') {
                const file = item.getAsFile();
                if (file) {
                    files.push(file);
                }
            }
        }
        
        if (files.length > 0) {
            e.preventDefault(); // Prevent default paste (which would paste binary data)
            
            showNotification(`Processing ${files.length} file(s) from clipboard...`, 'info');
            
            let successCount = 0;
            for (const file of files) {
                if (file.size > 2 * 1024 * 1024 * 1024) {
                    showNotification(`File too large (max 2GB): ${file.name}`, 'error');
                    continue;
                }
                
                const formData = new FormData();
                formData.append('file', file);
                
                try {
                    const response = await fetch('/api/upload', {
                        method: 'POST',
                        body: formData
                    });
                    
                    if (response.ok) {
                        const data = await response.json();
                        COOLEMS.selectedFiles.push({ 
                            name: file.name, 
                            url: data.url, 
                            content: data.content || null, 
                            is_text: data.is_text, 
                            is_image: data.is_image 
                        });
                        successCount++;
                    }
                } catch (err) {
                    console.error(`Failed to upload pasted file ${file.name}:`, err);
                }
            }
            
            updateFilePreview();
            
            if (successCount > 0) {
                // Add reference text to input
                const currentValue = messageInput.value;
                const fileRefs = files.map(f => `📎 ${f.name}`).join(' ');
                messageInput.value = currentValue + (currentValue ? '\n' + fileRefs : fileRefs);
                if (typeof autoResize === 'function') autoResize(messageInput);
                messageInput.focus();
                
                showNotification(`${successCount} file(s) pasted and ready`, 'success');
            } else if (files.length > 0) {
                showNotification('Failed to paste files from clipboard', 'error');
            }
        }
        // If no files, let the default text paste happen
    });
}

// ===== CURRENT TIME DISPLAY =====
function updateCurrentTime() {
    const now = new Date();
    const hours = String(now.getHours()).padStart(2, '0');
    const minutes = String(now.getMinutes()).padStart(2, '0');
    const seconds = String(now.getSeconds()).padStart(2, '0');
    // Day abbreviated to 3 letters too, so the clock line stays compact.
    const days = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
    // Month abbreviated to 3 letters so the clock line stays short.
    const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                    'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    const dayName = days[now.getDay()];
    const date = now.getDate();
    const monthName = months[now.getMonth()];
    const year = now.getFullYear();

    const timeText = `${hours}:${minutes}:${seconds} - ${dayName} ${date} ${monthName} ${year}`;
    const el = document.getElementById('currentTimeText');
    if (el) el.textContent = timeText;
}


// ===== DRAG AND DROP =====
// Safe check for whether a drag event carries file payloads. e.dataTransfer is null on
// some drag events (notably dragend, and in some engines dragleave), and
// dataTransfer.types is a DOMStringList (no .includes) in older browsers. Guard both.
function hasDraggedFiles(e) {
    const dt = e && e.dataTransfer;
    if (!dt) return false;
    const types = dt.types;
    if (typeof types.includes === 'function') return types.includes('Files');
    if (typeof types.contains === 'function') return types.contains('Files');
    return false;
}

let dragCounter = 0;

document.addEventListener('DOMContentLoaded', () => {
    const dragOverlay = document.getElementById('dragOverlay');
    const inputWrapper = document.getElementById('inputWrapper');

    ['dragenter', 'dragover', 'dragleave', 'drop'].forEach(eventName => {
        document.body.addEventListener(eventName, e => { e.preventDefault(); e.stopPropagation(); }, false);
    });

    document.body.addEventListener('dragenter', (e) => {
        if (!hasDraggedFiles(e)) return;
        dragCounter++;
        dragOverlay.classList.add('active');
        if (inputWrapper) inputWrapper.classList.add('drag-over');
    });

    document.body.addEventListener('dragleave', (e) => {
        if (!hasDraggedFiles(e)) return;
        dragCounter = Math.max(0, dragCounter - 1);
        if (dragCounter === 0) {
            dragOverlay.classList.remove('active');
            if (inputWrapper) inputWrapper.classList.remove('drag-over');
        }
    });

    document.body.addEventListener('dragover', (e) => {
        e.preventDefault();
        if (hasDraggedFiles(e)) e.dataTransfer.dropEffect = 'copy';
    });

    // GLOBAL DROP HANDLER - Skip if target is inside input area to prevent double processing
    document.body.addEventListener('drop', async (e) => {
        dragCounter = 0;
        dragOverlay.classList.remove('active');
        if (inputWrapper) inputWrapper.classList.remove('drag-over');
        
        // CRITICAL FIX: Skip processing if the drop target is inside input area
        // The input area has its own dedicated drop handler
        const target = e.target;
        if (inputWrapper && inputWrapper.contains(target)) {
            log('[DROP] Drop inside input area - skipping global handler to prevent double processing');
            return;
        }
        
        if (e.dataTransfer.files.length > 0) {
            const filesList = document.getElementById('filesList');
            const isInsideFilesList = filesList && filesList.contains(e.target);

            e.stopPropagation();
            if (!isInsideFilesList) {
                // Check if handleDroppedFiles exists
                if (typeof handleDroppedFiles === 'function') {
                    await handleDroppedFiles(e.dataTransfer.files);
                } else {
                    logError('handleDroppedFiles function not found');
                }
            }
        }
    });

    if (inputWrapper) {
        inputWrapper.addEventListener('dragenter', (e) => {
            e.preventDefault();
            inputWrapper.classList.add('drag-over');
        });

        inputWrapper.addEventListener('dragleave', (e) => {
            if (!inputWrapper.contains(e.relatedTarget)) {
                inputWrapper.classList.remove('drag-over');
            }
        });

        inputWrapper.addEventListener('dragover', (e) => {
            e.preventDefault();
            e.dataTransfer.dropEffect = 'copy';
            inputWrapper.classList.add('drag-over');
        });
    }
});

// ===== KEYBOARD SHORTCUTS =====
document.addEventListener('keydown', (e) => {
    if (e.ctrlKey && e.key === 'k') { e.preventDefault(); if (typeof newChat === 'function') newChat(); }
    if (e.key === 'Escape') { if (typeof closeSettings === 'function') closeSettings(); }
});

// ===== INPUT HELPERS =====
function autoResize(textarea) {
    if (!textarea) return;
    textarea.style.height = 'auto';
    textarea.style.height = Math.min(textarea.scrollHeight, 200) + 'px';
}

function handleKeyDown(e) {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        if (COOLEMS.isStreaming) {
            // (2026-10-23 live steering) Enter while the agent is working does NOT stop it
            // and does NOT start a new turn: the text is delivered to the running task.
            if (typeof sendSteerMessage === 'function') sendSteerMessage();
        } else if (typeof sendMessage === 'function') {
            sendMessage();
        }
    }
}

function toggleSidebar() {
    document.getElementById('sidebar').classList.toggle('open');
}

// ===== DIFF FUNCTIONS =====
function enterDiffMode(fileA, contentA, fileB, contentB) {
    COOLEMS.diffMode = true;
    COOLEMS.currentDiffFileA = fileA;
    COOLEMS.currentDiffFileB = fileB;
    
    // Hide single preview, show diff view
    const previewContent = document.getElementById('previewContent');
    const diffContainer = document.getElementById('diffContainer');
    const diffToolbar = document.getElementById('diffToolbar');
    const schemaSelector = document.getElementById('schemaSelector');
    const diffUnified = document.getElementById('diffUnifiedContainer');
    
    if (previewContent) previewContent.style.display = 'none';
    if (diffContainer) diffContainer.style.display = 'flex';
    if (diffUnified) diffUnified.style.display = 'none';
    if (diffToolbar) diffToolbar.style.display = 'flex';
    if (schemaSelector) schemaSelector.style.display = 'inline-block';
    
    // Initialize DiffView
    if (window.DiffView && window.SyntaxRegistry && window.SchemaManager) {
        window.DiffView.init(window.SyntaxRegistry, window.SchemaManager);
        
        const leftContainer = document.getElementById('diffContentLeft');
        const rightContainer = document.getElementById('diffContentRight');
        
        window.DiffView.renderSideBySide(
            { path: fileA, content: contentA },
            { path: fileB, content: contentB },
            leftContainer,
            rightContainer
        );
    } else {
        console.error('[COOLEMS] DiffView or dependencies not loaded');
    }
    
    // Initialize sync scroll
    const leftPanel = document.getElementById('diffContentLeft');
    const rightPanel = document.getElementById('diffContentRight');
    
    if (window.SyncScroll) {
        window.SyncScroll.initialize(leftPanel, rightPanel);
        const syncScrollBtn = document.getElementById('diffSyncScroll');
        if (syncScrollBtn) syncScrollBtn.classList.add('active');
    }
    
    // Set default mode button state
    const sideBySideBtn = document.getElementById('diffModeSideBySide');
    const unifiedBtn = document.getElementById('diffModeUnified');
    if (sideBySideBtn) sideBySideBtn.classList.add('active');
    if (unifiedBtn) unifiedBtn.classList.remove('active');
    
    log('Entered diff mode:', fileA, 'vs', fileB);
}

function exitDiffMode() {
    COOLEMS.diffMode = false;
    COOLEMS.currentDiffFileA = null;
    COOLEMS.currentDiffFileB = null;
    
    const previewContent = document.getElementById('previewContent');
    const diffContainer = document.getElementById('diffContainer');
    const diffToolbar = document.getElementById('diffToolbar');
    const schemaSelector = document.getElementById('schemaSelector');
    const diffUnified = document.getElementById('diffUnifiedContainer');
    const diffLeft = document.getElementById('diffContentLeft');
    const diffRight = document.getElementById('diffContentRight');
    
    if (previewContent) previewContent.style.display = 'block';
    if (diffContainer) diffContainer.style.display = 'none';
    if (diffUnified) diffUnified.style.display = 'none';
    if (diffToolbar) diffToolbar.style.display = 'none';
    if (schemaSelector) schemaSelector.style.display = 'inline-block';
    
    // Clear diff containers
    if (diffLeft) diffLeft.innerHTML = '';
    if (diffRight) diffRight.innerHTML = '';
    
    // Clear stats
    const statsContainer = document.getElementById('diffStats');
    if (statsContainer) statsContainer.innerHTML = '';
    
    if (window.SyncScroll) {
        window.SyncScroll.destroy();
    }
    
    if (window.DiffView) {
        window.DiffView.clear();
    }
    
    log('Exited diff mode');
}


// ===== INITIALIZATION =====
document.addEventListener('DOMContentLoaded', async () => {
    log('Initializing COOLEMS...');

    // Setup clipboard paste handler FIRST
    setupClipboardPaste();
    log('[INIT] Clipboard paste handler initialized');

    // Initialize the workspace tree and sidebar controls
    if (window.ConversationMgr && typeof window.ConversationMgr.init === 'function') {
        window.ConversationMgr.init();
        log('[INIT] ConversationManager initialized');
    }

    // Paint the Think chip before the catalog request, so its state is real from the first
    // frame: with no capability known it shows "Think: n/a" and enableThinking stays '' (send
    // nothing) instead of naming a level nobody has checked. loadModels() repaints once the
    // capabilities are known.
    if (typeof updateThinkingUI === 'function') updateThinkingUI();
    const catalogLoaded = await loadModels();
    if (typeof loadTree === 'function') loadTree();
    initScrollDetection();
    if (typeof updateAgentUI === 'function') updateAgentUI(); // sync header toggle with persisted state
    // Second paint: NOT a recovery from a failed catalog. With no catalog the caps are still
    // empty, so this produces the same "n/a" state as the first paint - the chip and the wire
    // agree on that ('' = the model's own default, never a guessed 'off'), and repeating the
    // call merely covers the paths where loadModels() returns WITHOUT painting (missing
    // picker, thrown error). Real recovery is the fleet_status push in websocket.js: it
    // refills thinkingCaps and repaints (or re-runs loadModels when models are unknown).
    if (typeof updateThinkingUI === 'function') updateThinkingUI();
    if (!catalogLoaded) {
        logError('Model catalog unavailable: the Think chip stays "n/a" and sends no thinking '
            + 'instruction (the model keeps its own default) until a fleet status push arrives.');
    }
    checkOllamaStatus();

    // Load conversations on page init (so sidebar populates after refresh)
    if (typeof loadConversations === 'function') {
        const data = await loadConversations();
        if (data) await restoreWorkspace(data.conversations);
        log('[INIT] Conversations loaded');
    }

    // === Current time display ===
    updateCurrentTime();
    setInterval(updateCurrentTime, 1000);

    
    // === Initialize diff UI if available ===
    if (window.DiffUI && typeof window.DiffUI.init === 'function') {
        window.DiffUI.init();
        log('DiffUI initialized from app.js');
        
        // Connect exit callback
        if (window.PreviewManager) {
            window.DiffUI.onExit(() => {
                log('DiffUI exit triggered');
                exitDiffMode();
                if (window.PreviewManager && window.PreviewManager.currentFile) {
                    window.PreviewManager.loadFile(
                        window.PreviewManager.currentFile.path,
                        window.PreviewManager.currentFile.name
                    );
                }
            });
        }
    }
    
    // === Initialize PreviewManager if available ===
    if (window.PreviewManager && typeof window.PreviewManager.init === 'function') {
        window.PreviewManager.init();
        log('PreviewManager initialized from app.js');
    }
    
    // === Initialize SyntaxRegistry if available ===
    if (window.SyntaxRegistry && !window.SyntaxRegistry.initialized) {
        window.SyntaxRegistry.init();
        log('SyntaxRegistry initialized from app.js');
    }
    
    log('COOLEMS initialization complete');
});

// ===== CLEANUP ON PAGE UNLOAD / WINDOW CLOSE (2026-09-10 stale-task fix) =====
// USER CONTRACT: "anytime we close the UI, all tasks must be stopped".
// The CLIENT backend keeps chat sessions alive in its ChatBus even after every socket is
// gone - closing the sockets alone would leave the turn streaming (and burning tokens).
// So on unload we ALSO fire a keepalive beacon to POST /api/shutdown-all which stops
// EVERY live session server-side. fetch(keepalive) survives page teardown; auth headers
// are injected automatically by the api-key.js fetch override. The call is idempotent -
// firing it from both beforeunload and pagehide is safe (second call stops 0 sessions).
function _notifyBackendUiClosed() {
    try {
        if (window.fetch) {
            fetch('/api/shutdown-all', { method: 'POST', keepalive: true }).catch(() => {});
        }
    } catch (e) { /* page is tearing down - ignore */ }
}

function _cleanupOnUiClose() {
    // 1) Tell the backend to stop ALL live tasks first (the important part).
    _notifyBackendUiClosed();
    // 2) Close EVERY chat's socket, not just the current one — each conversation keeps
    //    its own connection now. (2026-09-08 multi-chat)
    if (typeof ChatSocketPool !== 'undefined') {
        ChatSocketPool.closeAll();
    } else if (COOLEMS.ws) {
        COOLEMS.ws.close();   // legacy fallback in case the pool is missing
    }
    log('Cleanup complete');
}

window.addEventListener('beforeunload', _cleanupOnUiClose);
// pagehide covers paths where beforeunload does not fire reliably (tab crash/close on some
// browsers). Both are idempotent.
window.addEventListener('pagehide', _cleanupOnUiClose);
// ===== GLOBAL FUNCTIONS EXPOSED FOR OTHER MODULES =====
window.escapeHtml = escapeHtml;
window.escapeAttr = escapeAttr;
window.escapeJsAttr = escapeJsAttr;
window.getWebSocketUrl = getWebSocketUrl;
window.enterDiffMode = enterDiffMode;
window.exitDiffMode = exitDiffMode;
window.autoResize = autoResize;
window.handleKeyDown = handleKeyDown;
