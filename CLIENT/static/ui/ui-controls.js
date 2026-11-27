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

/* ===== THINKING LEVEL ==============================================
 * One chip that cycles the levels the SELECTED model really offers.
 *
 * Capability comes from the SERVER catalog entry's `thinking` block, built by the brain
 * from the serving endpoint's /props chat_template (ThinkingPolicy), so the chip never
 * promises a level a model cannot honor:
 *   levels declared by the template -> none / low / medium / high (mapped server-side)
 *   toggle only                     -> none / medium
 *   no knob, capability unknown     -> "Think: n/a" (nothing is sent)
 * Default is THINK_DEFAULT ('medium') for every workspace that never got a level; only a
 * level the user picked with the chip is persisted (coolems_thinking + the
 * coolems_thinking_set flag), so a value parked by the capability check below never turns
 * into the default of the next workspace.
 *
 * This file owns the whole feature: THINK_DEFAULT, startThinkingLevel() (which level the
 * workspace WANTS) and updateThinkingUI() (which level is SENT). Until a capability is
 * known nothing is sent - '' is the SERVER's "no instruction" value, which every provider
 * reads as "the model's own default" (NOT "off") - so a request cannot carry a thinking
 * level that was never checked against the model.
 * =================================================================== */
const THINK_LEVELS = ['none', 'low', 'medium', 'high'];
const THINK_LABELS = { none: 'Think: off', low: 'Think: low', medium: 'Think: medium', high: 'Think: high' };
/* Level a workspace starts at when the user never picked one. */
const THINK_DEFAULT = 'medium';
/** Level this workspace WANTS: the user's own pick, else THINK_DEFAULT.
 *
 * coolems_thinking_set is what marks a real pick - a level that an older build (or the
 * capability check) parked in coolems_thinking is not a choice.
 */
function startThinkingLevel() {
    try {
        const picked = localStorage.getItem('coolems_thinking_set') === '1';
        const v = (localStorage.getItem('coolems_thinking') || '').toLowerCase();
        return (picked && THINK_LEVELS.includes(v)) ? v : THINK_DEFAULT;
    } catch (e) { return THINK_DEFAULT; }
}
/* Shown when there is nothing to switch. The chip must say WHY, because the fallback is
   silent by design: ThinkingPolicy sends NO thinking keys when it cannot figure out the
   model's controls (a strict GGUF template raises on an undeclared variable, which would
   fail every request), so the model just keeps its own default. */
const THINK_NA_LABEL = 'Think: n/a';
const THINK_NA_UNKNOWN = 'Thinking capability not discovered yet for this model - no thinking keys are sent, the model keeps its own default.';
const THINK_NA_NONE = 'This model exposes no thinking control - no thinking keys are sent, the model keeps its own default.';
const THINK_NA_PENDING = 'Still loading the model catalog - nothing is sent yet, so the model keeps its own default until the capabilities arrive.';
const THINK_NA_NO_CATALOG = 'The model catalog could not be loaded - no thinking instruction is sent and the model keeps its own default. It is retried on the next fleet status push (or a page reload).';

/* Whether the catalog ever described the models in this page: 'pending' (first paint, request
   in flight), 'loaded' (loadModels() parsed it) or 'failed' (never arrived). It only picks the
   WORDING of "Think: n/a" - every one of those states sends nothing - but they are different
   faults and a user reading the tooltip deserves the right one. */
let thinkingCatalogState = 'pending';

/** Store a level the USER picked (the flag is what makes it survive as a choice). */
function persistThinking() {
    try {
        localStorage.setItem('coolems_thinking', COOLEMS.thinkingDesired || THINK_DEFAULT);
        localStorage.setItem('coolems_thinking_set', '1');
    } catch (e) {}
}

/** Capability description of the model currently selected in the picker. */
function thinkingCapsForSelected() {
    const sel = document.getElementById('modelSelector');
    const name = sel ? sel.value : '';
    if (!name) return null;
    return COOLEMS.thinkingCaps[name] || null;
}

/** Levels the selected model offers. Always starts with 'none' when it can be disabled. */
function availableThinkingLevels() {
    const caps = thinkingCapsForSelected();
    if (!caps) return ['none'];                 // capability unknown -> chip is n/a, nothing is sent
    const levels = (caps.levels || []).filter(l => THINK_LEVELS.includes(l));
    return levels.length ? levels : ['none'];
}

/** True when this model has anything to switch (a request knob, or reasoning we can show). */
function thinkingAvailable() {
    return availableThinkingLevels().length > 1;
}

/** Why there is nothing to switch - the tooltip/notification text. */
function thinkingUnavailableReason() {
    const caps = thinkingCapsForSelected();
    if (!caps) {
        if (thinkingCatalogState === 'pending') return THINK_NA_PENDING;
        return thinkingCatalogState === 'loaded' ? THINK_NA_UNKNOWN : THINK_NA_NO_CATALOG;
    }
    if (caps.hint) return caps.hint;
    return THINK_NA_NONE;
}

/** Paint the chip: label + active state + tooltip naming the model's own values. */
function updateThinkingUI() {
    const btn = document.getElementById('thinkToggle');
    const label = document.getElementById('thinkLabel');
    if (!btn) return;
    const levels = availableThinkingLevels();
    const caps = thinkingCapsForSelected();
    // Resolve the wish once per workspace: the user's own pick, else THINK_DEFAULT.
    const want = COOLEMS.thinkingDesired = COOLEMS.thinkingDesired || startThinkingLevel();
    // Send only a level this model honors: the wished level when offered, else the default
    // when offered, else off. thinkingDesired is left alone - the model's limits must never
    // erase the user's choice (or the 'medium' a fresh workspace starts with), so switching
    // to a model that can think brings it back.
    // DECIDED (unknown capability): send NOTHING (''), which every provider reads as "the
    // model keeps its own default". That is not the same as "off", and '' is chosen over a
    // 'none' stand-in on purpose: OpenAI's policy drops 'none' (thinking_body -> {}), so
    // 'none' would mean exactly what '' means there, while on DeepSeek/llama it would
    // silently disable the reasoning of a model the user may have picked FOR it. The chip
    // says which happens ("Think: n/a" - the model keeps its own default) and the next
    // fleet_status push (websocket.js) fills the caps and repaints this same function.
    COOLEMS.enableThinking = !caps ? ''
        : (levels.includes(want) ? want : (levels.includes(THINK_DEFAULT) ? THINK_DEFAULT : 'none'));

    const na = levels.length <= 1 && (!caps || !caps.supported);
    const off = COOLEMS.enableThinking === 'none';
    btn.classList.toggle('active', !off && !na);
    btn.classList.toggle('na', na);
    if (label) {
        label.textContent = na ? THINK_NA_LABEL
            : (off ? 'Think' : (THINK_LABELS[COOLEMS.enableThinking] || 'Think'));
    }

    btn.title = na ? thinkingUnavailableReason()
        : (!caps || !caps.supported)
            // Nothing to INSTRUCT, but reasoning may still arrive (an R1-style model, or a
            // node started with --reasoning): the level only decides whether we SHOW it.
            ? ((caps && caps.hint) ? caps.hint : 'This model cannot be instructed to think')
              + ' - the level only decides whether reasoning is displayed'
            : 'Thinking level: ' + COOLEMS.enableThinking + ' (this model offers '
                + levels.join(', ') + '; its own values: '
                + ((caps.model_values || []).join('/') || 'on/off') + ')'
                + (caps.enforced === false ? ' - note: ' + (caps.hint || 'control may be ignored') : '');
    // NOT disabled on purpose: a click on an unavailable chip explains WHY (see
    // cycleThinking). A disabled button would be silent and read as a broken control.
    btn.disabled = false;
    btn.setAttribute('aria-disabled', na ? 'true' : 'false');
    btn.style.opacity = na ? '0.55' : '1';
}

/** Click: step to the next level this model offers (wraps around). */
function cycleThinking() {
    const levels = availableThinkingLevels();
    if (levels.length <= 1) {
        // Nothing to switch: say so plainly instead of pretending the level changed.
        showNotification('Thinking not available: ' + thinkingUnavailableReason(), 'info');
        return;
    }
    const i = levels.indexOf(COOLEMS.enableThinking);
    COOLEMS.thinkingDesired = levels[(i + 1) % levels.length];
    COOLEMS.enableThinking = COOLEMS.thinkingDesired;
    persistThinking();
    updateThinkingUI();
}

window.cycleThinking = cycleThinking;
window.updateThinkingUI = updateThinkingUI;
window.availableThinkingLevels = availableThinkingLevels;
window.thinkingAvailable = thinkingAvailable;

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
        if (typeof loadRuntimeSettings === 'function') loadRuntimeSettings(); // settings.json runtime keys -> Runtime tab
        if (typeof loadMcpServers === 'function') loadMcpServers(); // config/mcp_servers.json -> MCP tab
        if (typeof loadAddons === 'function') loadAddons(); // config/addons.json -> Addons tab
    }

function closeSettings() {
    document.getElementById('settingsModal').classList.remove('active');
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

/* ===== PROVIDER BALANCE CHIP =========================================
 * (2026-11 balance chip) Shows the remaining balance of the provider that
 * serves the model loaded in this workspace (DeepSeek: CNY, OpenAI: USD).
 *
 * Visibility is decided on the SERVER: the profile flag show_provider_balance
 * arrives in auth_ok (apply_auth_info caches it) and /api/balance answers
 * enabled=false for profiles without it - the number is never revealed.
 * balance=null means the loaded model has no billable account (local llama)
 * or the lookup failed: the chip stays hidden in both cases.
 *
 * Refresh points: page load (loadModels), model switch (switchModel + the
 * success broadcast) and every 'done' frame (websocket.js) - the last one
 * throttled to 30s so a busy agentic turn does not spam the free endpoint.
 * =================================================================== */
let _lastBalanceFetch = 0;
let _balanceRequestId = 0;
const BALANCE_THROTTLE_MS = 30000;

async function fetchBalance(model) {
    try {
        const r = await fetch('/api/balance?model=' + encodeURIComponent(model || ''));
        if (!r.ok) return null;
        return await r.json();
    } catch (e) {
        return null;
    }
}

function updateBalanceChip(data) {
    const chip = document.getElementById('balanceChip');
    if (!chip) return;
    const bal = (data && data.enabled) ? data.balance : null;
    const label = document.getElementById('balanceChipText');
    const link = document.getElementById('balanceChipLink');
    const openaiCredits = bal?.kind === 'openai_credit_grants';
    if (link) link.hidden = !openaiCredits;
    if (label) label.hidden = openaiCredits;
    if (openaiCredits && link) {
        chip.title = 'View the current prepaid credit balance on OpenAI';
        chip.classList.remove('low');
        chip.style.display = 'flex';
        return;
    }
    if (!bal || bal.total === null || bal.total === undefined) {
        chip.style.display = 'none';
        return;
    }
    const cur = bal.currency === 'USD' ? '$' : (bal.currency === 'CNY' ? '\u00A5' : bal.currency + ' ');
    const text = cur + String(bal.total);
    if (label) label.textContent = text;
    chip.title = 'Provider balance: ' + text
        + (bal.is_available === false ? ' (insufficient funds)' : '');
    chip.classList.toggle('low', bal.is_available === false);
    chip.style.display = 'flex';
}

/** Refresh the chip for the selected model, at most once per 30s. */
function refreshBalanceThrottled(force) {
    const sel = document.getElementById('modelSelector');
    const model = sel ? sel.value : '';
    if (!model) {
        updateBalanceChip(null);
        return;
    }
    const now = Date.now();
    if (!force && now - _lastBalanceFetch < BALANCE_THROTTLE_MS) return;
    _lastBalanceFetch = now;
    const request = ++_balanceRequestId;
    if (force) updateBalanceChip(null);
    fetchBalance(model).then(data => {
        if (request === _balanceRequestId && sel.value === model) updateBalanceChip(data);
    });
}

/**
 * Load ALL models from disk (llama_server/models/*.gguf)
 * Shows currently loaded model with indicator
 *
 * Returns true when the catalog was read (thinkingCaps filled, chip repainted), false when
 * the fetch/parse failed - the caller then knows the Think chip is in its "n/a" fallback.
 */
async function loadModels() {
    try {
        const r = await fetch('/api/models'); 
        const models = await r.json();
        const sel = document.getElementById('modelSelector'); 
        const cur = COOLEMS.currentConversation ? '' : sel.value;
        
        // Determine which model to select:
        // 1. PRESERVE the user's current selection when it is still served (2026-10 fleet:
        //    the list is rebuilt after routing; without this the picker would snap back to
        //    the local model and the next chat would silently go to the wrong model)
        // 2. Otherwise prefer a served model that is loaded locally (m.loaded)
        // 3. Otherwise the first served model
        let defaultModel = null;
        if (cur && models.some(m => m.name === cur && m.served !== false)) {
            defaultModel = cur;
        } else {
            const loadedModel = models.find(m => m.loaded && m.served !== false)
                || models.find(m => m.served !== false);
            if (loadedModel) {
                defaultModel = loadedModel.name;
            } else if (cur && models.some(m => m.name === cur)) {
                defaultModel = cur;
            } else if (models.length > 0) {
                defaultModel = models[0].name;
            }
        }
        
        // Build options with size, fleet state (2026-10: models served by any node)
        // and loaded indicator. Unserved models are disabled - picking one would only
        // produce a "no llama node is serving this" error.
        // Keep each model's discovered thinking capability so the chip offers exactly
        // the levels that model honors.
        COOLEMS.thinkingCaps = {};
        models.forEach(m => { if (m && m.name) COOLEMS.thinkingCaps[m.name] = m.thinking || null; });
        thinkingCatalogState = 'loaded';
        sel.replaceChildren(...models.map(m => {
            const option = document.createElement('option');
            option.value = m.name;
            option.disabled = m.served === false;
            const size = m.size ? ` (${formatFileSize(m.size)})` : '';
            const state = option.disabled ? ' — no node' : (m.online_slots > 0 ? ` · ${m.online_slots} slot(s)` : '');
            option.textContent = m.name + size + state + (m.loaded ? ' ⬤' : '') + (/mtp/i.test(m.name) ? ' [MTP]' : '');
            return option;
        }));
        if (COOLEMS.currentConversation) {
            setModelPicker(workspaceModel(COOLEMS.currentConversation));
            return true;
        }
        if (defaultModel) sel.value = defaultModel;

        log('Loaded ' + models.length + ' models (fleet catalog)');
        // The picker changed (possibly to another model), so re-check which thinking
        // levels are selectable.
        if (typeof updateThinkingUI === 'function') updateThinkingUI();
        // (2026-11 balance chip) the loaded model may have changed - refresh the chip.
        refreshBalanceThrottled(true);
        return true;

    } catch (e) { 
        // The catalog did not arrive. thinkingCaps is left exactly as it was (it is only
        // replaced after a successful parse), and so is a 'loaded' state: a failed RELOAD still
        // lets a known model be described, while a page that never got a catalog says so.
        // Either way the chip sends nothing ('' = the model's own default, never a guessed off).
        if (thinkingCatalogState !== 'loaded') thinkingCatalogState = 'failed';
        logError('Failed to load models:', e); 
        return false;
    }
}

// Keep confirmed assignments separate from pending writes. Requests for one workspace
// are serialized; requests for different workspaces can finish independently.
const workspaceModelWrites = new Map();

function workspaceModel(convId) {
    if (convId) return workspaceModelWrites.get(convId)?.model || COOLEMS.convModels[convId] || null;
    return document.getElementById('modelSelector')?.value || null;
}

/** Project a workspace's assignment into the shared picker without changing it. */
function setModelPicker(model) {
    const sel = document.getElementById('modelSelector');
    if (!sel) return;
    sel.querySelector('option[data-workspace-model]')?.remove();
    if (!model || !Array.from(sel.options).some(option => option.value === model)) {
        const option = document.createElement('option');
        option.dataset.workspaceModel = '';
        option.value = model || '';
        option.textContent = model ? `${model} — unavailable` : 'Server default';
        option.disabled = true;
        sel.prepend(option);
    }
    sel.value = model || '';
    updateThinkingUI();
    refreshBalanceThrottled(true);
}

function persistWorkspaceModel(convId, model) {
    const previous = workspaceModelWrites.get(convId);
    const write = { model };
    const result = (previous?.done || Promise.resolve()).then(async () => {
        try {
            const response = await fetch(`/api/conversations/${convId}/update`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ model })
            });
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            COOLEMS.convModels[convId] = model;
            return true;
        } catch (e) {
            logError('[MODEL PERSIST] Failed to save workspace model:', e);
            return false;
        }
    });
    workspaceModelWrites.set(convId, write);
    write.done = result.then(saved => {
        const latest = workspaceModelWrites.get(convId) === write;
        if (latest) workspaceModelWrites.delete(convId);
        if (latest && !saved) {
            if (COOLEMS.currentConversation === convId) setModelPicker(workspaceModel(convId));
            showNotification(`Could not save ${model} for the workspace. Previous model retained.`, 'error');
        }
        return saved;
    });
    return write.done;
}

async function switchModel(model) {
    const convId = COOLEMS.currentConversation;
    if (isConversationSwitching()) {
        if (convId) setModelPicker(workspaceModel(convId));
        return;
    }
    if (!model) return;
    const saved = convId ? persistWorkspaceModel(convId, model) : Promise.resolve(true);
    updateThinkingUI();
    refreshBalanceThrottled(true);
    if (!await saved) return;
    // This is an availability check, not a reload or a prerequisite for sending.
    try {
        const response = await fetch('/api/llama/load-model', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ model })
        });
        const data = await response.json();
        if (!response.ok || data.status !== 'ok') {
            throw new Error(data.message || data.detail || `HTTP ${response.status}`);
        }
        if (COOLEMS.currentConversation === convId && workspaceModel(convId) === model) {
            showNotification(`${model} is available`, 'success');
        }
    } catch (e) {
        if (COOLEMS.currentConversation === convId && workspaceModel(convId) === model) {
            showNotification(`Could not confirm routing for ${model}: ${e.message}`, 'warning');
        }
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
