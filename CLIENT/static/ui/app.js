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

// ===== SHARED STATE =====
window.COOLEMS = {
    currentConversation: null,
    ws: null,
    selectedFiles: [],
    // Persisted in localStorage (see persistAgentMode in ui-controls.js)
    agentMode: (function () { try { return localStorage.getItem('coolems_agent_mode') === '1'; } catch (e) { return false; } })(),
    enableThinking: false,
    isStreaming: false,
    wsReconnectAttempts: 0,
    filesPanelOpen: false,
    MAX_RECONNECT_ATTEMPTS: 3,
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
    const days = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
    const months = ['January', 'February', 'March', 'April', 'May', 'June',
                    'July', 'August', 'September', 'October', 'November', 'December'];
    const dayName = days[now.getDay()];
    const date = now.getDate();
    const monthName = months[now.getMonth()];
    const year = now.getFullYear();

    const timeText = `${hours}:${minutes}:${seconds} - ${dayName} ${date} ${monthName} ${year}`;
    const el = document.getElementById('currentTimeText');
    if (el) el.textContent = timeText;
}


// ===== DRAG AND DROP =====
let dragCounter = 0;

document.addEventListener('DOMContentLoaded', () => {
    const dragOverlay = document.getElementById('dragOverlay');
    const inputWrapper = document.getElementById('inputWrapper');

    ['dragenter', 'dragover', 'dragleave', 'drop'].forEach(eventName => {
        document.body.addEventListener(eventName, e => { e.preventDefault(); e.stopPropagation(); }, false);
    });

    document.body.addEventListener('dragenter', (e) => {
        dragCounter++;
        if (e.dataTransfer.types.includes('Files')) {
            dragOverlay.classList.add('active');
            if (inputWrapper) inputWrapper.classList.add('drag-over');
        }
    });

    document.body.addEventListener('dragleave', () => {
        dragCounter--;
        if (dragCounter === 0) {
            dragOverlay.classList.remove('active');
            if (inputWrapper) inputWrapper.classList.remove('drag-over');
        }
    });

    document.body.addEventListener('dragover', (e) => {
        e.preventDefault();
        e.dataTransfer.dropEffect = 'copy';
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
    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        if (!COOLEMS.isStreaming && typeof sendMessage === 'function') sendMessage();
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

    // Initialize ConversationManager (color, rename, sort)
    if (window.ConversationMgr && typeof window.ConversationMgr.init === 'function') {
        window.ConversationMgr.init();
        log('[INIT] ConversationManager initialized');
    }

    await loadModels();
    if (typeof loadTree === 'function') loadTree();
    initScrollDetection();
    if (typeof updateAgentUI === 'function') updateAgentUI(); // sync header toggle with persisted state
    checkOllamaStatus();

    // Load conversations on page init (so sidebar populates after refresh)
    if (typeof loadConversations === 'function') {
        await loadConversations();
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
window.getWebSocketUrl = getWebSocketUrl;
window.enterDiffMode = enterDiffMode;
window.exitDiffMode = exitDiffMode;
window.autoResize = autoResize;
window.handleKeyDown = handleKeyDown;
