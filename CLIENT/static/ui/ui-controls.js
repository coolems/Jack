/**
 * ui-controls.js - Toggle buttons, settings modal, model loading, notifications.
 * Uses COOLEMS shared state from app.js.
 */

function persistAgentMode() {
    try { localStorage.setItem('coolems_agent_mode', COOLEMS.agentMode ? '1' : '0'); } catch (e) {}
}

function toggleAgent() { COOLEMS.agentMode = !COOLEMS.agentMode; persistAgentMode(); updateAgentUI(); }

function toggleAgentSwitch() { COOLEMS.agentMode = !COOLEMS.agentMode; persistAgentMode(); updateAgentUI(); }

function updateAgentUI() { 
    document.getElementById('agentToggle').classList.toggle('active', COOLEMS.agentMode); 
    document.getElementById('agentSwitch').classList.toggle('active', COOLEMS.agentMode); 
}

function toggleThink() { 
    COOLEMS.enableThinking = !COOLEMS.enableThinking; 
    document.getElementById('thinkToggle').classList.toggle('active', COOLEMS.enableThinking); 
}

/**
     * Settings tabs - switch between panes (Authentication | Agent / Search).
     * Each .settings-tab button carries data-pane matching a .settings-pane div;
     * the choice is remembered in localStorage so reopening lands on the same tab.
     * Adding a future tab = one more <button class="settings-tab" data-pane="x"> +
     * one <div class="settings-pane" data-pane="x"> in index.html (no JS change needed).
     */
    function switchSettingsTab(name) {
        const modal = document.getElementById('settingsModal');
        if (!modal) return;
        modal.querySelectorAll('.settings-tab').forEach(t => t.classList.toggle('active', t.dataset.pane === name));
        modal.querySelectorAll('.settings-pane').forEach(pn => pn.classList.toggle('active', pn.dataset.pane === name));
        try { localStorage.setItem('coolems_settings_tab', name); } catch (e) {}
    }

    function applyStoredSettingsTab() {
        let tab = 'auth'; // default: Authentication first
        try { tab = localStorage.getItem('coolems_settings_tab') || 'auth'; } catch (e) {}
        const modal = document.getElementById('settingsModal');
        if (!modal) return;
        const has = Array.prototype.some.call(modal.querySelectorAll('.settings-tab'), t => t.dataset.pane === tab);
        switchSettingsTab(has ? tab : 'auth');
    }

    function openSettings() { 
        document.getElementById('settingsModal').classList.add('active'); 
        applyStoredSettingsTab(); // restore last-used tab (default: Authentication)
        loadSettings();
        if (typeof loadSearchEngineSettings === 'function') loadSearchEngineSettings(); // config/search_engines.json -> Settings UI
    }

function closeSettings() {
    document.getElementById('settingsModal').classList.remove('active');
    if (COOLEMS.currentConversation) 
        fetch('/api/conversations/' + COOLEMS.currentConversation + '/update', { 
            method: 'POST', 
            headers: { 'Content-Type': 'application/json' }, 
            body: JSON.stringify({ 
                agent_mode: COOLEMS.agentMode 
            }) 
        }).catch(() => {});
}

/**
 * Format file size in human-readable format
 */
function formatFileSize(bytes) {
    if (!bytes || bytes === 0) return '';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(0) + ' KB';
    if (bytes < 1024 * 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
    return (bytes / (1024 * 1024 * 1024)).toFixed(1) + ' GB';
}

/**
 * Load ALL models from disk (llama_server/models/*.gguf)
 * Shows currently loaded model with indicator
 */
async function loadModels() {
    try {
        const r = await fetch('/api/models'); 
        const models = await r.json();
        const sel = document.getElementById('modelSelector'); 
        const cur = sel.value;
        
        // Determine which model to select:
        // 1. Prefer currently loaded model (m.loaded === true)
        // 2. Fall back to preserving current selection
        // 3. Fall back to first model in list
        let defaultModel = null;
        const loadedModel = models.find(m => m.loaded);
        if (loadedModel) {
            defaultModel = loadedModel.name;
        } else if (cur && models.some(m => m.name === cur)) {
            defaultModel = cur;
        } else if (models.length > 0) {
            defaultModel = models[0].name;
        }
        
        // Build options with size and loaded indicator
        sel.innerHTML = models.map(m => {
            const name = m.name;
            const sizeStr = m.size ? ` (${formatFileSize(m.size)})` : '';
            const loadedStr = m.loaded ? ' ⬤' : '';
            const selected = (name === defaultModel) ? ' selected' : '';
            const mtpStr = /mtp/i.test(name) ? ' [MTP]' : '';
            return '<option value="' + name + '"' + selected + '>' + name + sizeStr + loadedStr + mtpStr + '</option>';
        }).join('');
        
        log('Loaded ' + models.length + ' models from disk');
        
    } catch (e) { 
        logError('Failed to load models:', e); 
    }
}

/**
 * Switch to a different model - SERVER restarts llama.cpp with the new model.
 *
 * (2026-08-23 fix) The UI no longer polls /api/models or shows its own success
 * message: the AUTHORITATIVE result comes from the 'model_switch_status'
 * broadcast pushed by agent.py when the SERVER confirms the reload
 * (websocket.js handleModelSwitchStatus -> "success" notification + dropdown
 * refresh). This function only starts the switch and reports hard failures.
 */
async function switchModel(modelName) {
    if (!modelName) { logError('[MODEL SWITCH] No model name provided'); return; }
    
    log('[MODEL SWITCH] Switching to: ' + modelName);
    
    // Find which model is currently LOADED (the one with ⬤ indicator)
    const sel = document.getElementById('modelSelector');
    let currentlyLoadedModel = null;
    if (sel) {
        const allOptions = sel.querySelectorAll('option');
        for (const opt of allOptions) {
            if (opt.textContent.includes('\u2B24')) {
                currentlyLoadedModel = opt.value;
                break;
            }
        }
    }
    
    // Check if this is the same model already loaded
    if (currentlyLoadedModel && currentlyLoadedModel === modelName) {
        log('[MODEL SWITCH] Already on this model (' + modelName + '), skipping.');
        return;
    }
    
    log('[MODEL SWITCH] Current loaded: ' + (currentlyLoadedModel || 'unknown') + ', target: ' + modelName);
    
    // Disable UI during switch
    const sendBtn = document.getElementById('sendBtn');
    if (sendBtn) sendBtn.disabled = true;
    
    showNotification('\u23F3 Switching model... Server will restart. This may take a moment.', 'info');
    
    try {
        log('[MODEL SWITCH] Sending POST to /api/llama/load-model...');
        const r = await fetch('/api/llama/load-model', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ model: modelName })
        });
        
        log('[MODEL SWITCH] Response status: ' + r.status);
        
        if (!r.ok) {
            const errData = await r.json().catch(() => ({}));
            throw new Error(errData.detail || errData.message || 'HTTP ' + r.status);
        }
        
        const data = await r.json();
        log('[MODEL SWITCH] Server accepted: ' + data.message);
        
        // SERVER confirmed the requested model is ALREADY loaded (no reload happened,
        // so no model_switch_status broadcast will come). This IS a server-side
        // confirmation, so we may show the good message now and restore the UI.
        if (/already loaded/i.test(data.message || '')) {
            COOLEMS.modelSwitchPending = false;
            const b2 = document.getElementById('sendBtn');
            if (b2) b2.disabled = false;
            showNotification('\u2713 ' + (data.message || modelName), 'success');
            if (typeof loadModels === 'function') {
                loadModels().catch(() => {});
            }
            return;
        }
        
        // NOTE: no polling / success notification here on purpose.
        // The "good" message is shown ONLY by websocket.js when the SERVER
        // confirms the reload (model_switch_status -> 'success'), and that same
        // handler refreshes the model dropdown so the UI shows the new model.
        // Send button stays disabled until that confirmation arrives (websocket.js
        // re-enables it on success/failed). Safety net: force-re-enable after the
        // SERVER-side switch budget in case the broadcast never reaches us.
        COOLEMS.modelSwitchPending = true;
        setTimeout(() => {
            if (COOLEMS.modelSwitchPending) {
                log('[MODEL SWITCH] No confirmation received - re-enabling send button (safety net)');
                COOLEMS.modelSwitchPending = false;
                const b = document.getElementById('sendBtn');
                if (b) b.disabled = false;
            }
        }, 345000);
        
    } catch (e) {
        showNotification('\u2717 Failed to switch model: ' + e.message, 'error');
        logError('[MODEL SWITCH] Exception:', e);
        COOLEMS.modelSwitchPending = false;
        if (sendBtn) sendBtn.disabled = false;
    }
}

async function checkOllamaStatus() {
    try {
        // First get the current provider info
        const statusResp = await fetch('/api/status');
        if (!statusResp.ok) return; // Can't even reach backend
        
        const statusData = await statusResp.json();
        const providerName = (statusData.provider || '').toLowerCase();
        
        // Skip provider-specific warnings for coolems providers
        // They manage their own connectivity internally
        if (providerName.includes('coolems')) {
            log('[STATUS] Provider is ' + providerName + ', skipping ollama/llama health check');
            return;
        }
        
        // For ollama/llama providers, do the actual health check
        const r = await fetch('/api/ollama/check');
        if (r.ok) { 
            const d = await r.json(); 
            if (!d.healthy) {
                // Show provider-appropriate message
                if (providerName.includes('llama')) {
                    showNotification('Llama server is not running. Start it with: llama-server', 'warning');
                } else {
                    showNotification('Ollama is not running. Start it with: ollama serve', 'warning');
                }
            } 
        }
    } catch (e) { 
        logError('[STATUS] Failed to check provider status:', e);
        // Don't show notification here - the backend might just be slow to start
    }
}

// ===== Settings Management =====

async function loadSettings() {
    try {
        const r = await fetch('/api/settings');
        if (r.ok) {
            const settings = await r.json();

            // Load server address from settings.json (single source of truth)
            const serverAddrInput = document.getElementById('serverAddressInput');
            const serverAddrStatus = document.getElementById('serverAddressStatus');
            if (serverAddrInput && settings.current_server_address) {
                serverAddrInput.value = settings.current_server_address;

                // Show status - always shows the active address from settings.json
                if (serverAddrStatus) {
                    serverAddrStatus.textContent = 'Server: ' + settings.current_server_address;
                    serverAddrStatus.style.color = 'var(--text-secondary)';
                }
            }
            log('Settings loaded:', settings);
        }
    } catch (e) {
        logError('Failed to load settings:', e);
    }
}
