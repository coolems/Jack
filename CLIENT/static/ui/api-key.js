/**
 * api-key.js - API key management for the frontend.
 * Handles: save, load, set (setup mode), connect (validate email+key), clear, show/hide, inject into all requests.
 * Uses COOLEMS shared state from app.js.
 *
 * SETUP MODE (2026-09-01): while no API key is stored yet, the whole CLIENT backend
 * is locked down server-side - only POST /api/auth/set-key works. The Settings modal
 * reflects that: ONLY "Set API Key" is possible, everything else (server address,
 * email, connect, clear, agent/search tab) are hidden until a key has been set.
 */

// ===== API KEY STORAGE =====

function getStoredApiKey() {
    return localStorage.getItem('coolems_api_key') || '';
}

function saveApiKey() {
    const input = document.getElementById('apiKeyInput');
    if (input) {
        localStorage.setItem('coolems_api_key', input.value.trim());
    }
}

function loadApiKeyToInput() {
    const key = getStoredApiKey();
    const input = document.getElementById('apiKeyInput');
    if (input && key) {
        input.value = key;
    }
}

function toggleApiKeyVisibility() {
    const input = document.getElementById('apiKeyInput');
    if (input) {
        // Toggle CSS class instead of changing type to avoid Chrome password manager
        input.classList.toggle('password-masked');
    }
}

/**
 * SETUP MODE UI (2026-09-01): while no key is stored, Settings shows ONLY the API
 * key field + "Set API Key". Server address / email / connect / clear and the whole
 * Agent tab are hidden - nobody can do anything there except set the key. This
 * mirrors the server-side lock (APIMiddleware setup mode) where only
 * POST /api/auth/set-key is reachable before a real key exists.
 */
function applySetupModeUI() {
    const modal = document.getElementById('settingsModal');
    if (!modal) return;

    const inSetup = !getStoredApiKey();
    modal.classList.toggle('setup-mode', inSetup);

    // Agent / Search tab: only meaningful once a key exists.
    const agentTab = modal.querySelector('.settings-tab[data-pane="agent"]');
    if (agentTab) agentTab.style.display = inSetup ? 'none' : '';

    // Force the Authentication pane while locked down.
    if (inSetup && typeof switchSettingsTab === 'function') {
        switchSettingsTab('auth');
    }

    const statusEl = document.getElementById('apiKeyStatus');
    if (statusEl) {
        if (inSetup) {
            statusEl.textContent = 'No API key set yet - only "Set API Key" is available until then.';
            statusEl.style.color = 'var(--warning)';
        } else if (!statusEl.dataset.customized) {
            // Only overwrite the initial hint once a key exists (keep live messages).
            statusEl.textContent = '';
        }
    }
}

/**
 * "Set API Key" button handler - stores the key locally AND writes it to the CLIENT
 * backend via POST /api/auth/set-key. That endpoint is the ONLY one that works while
 * no real key exists (setup mode), so a successful response means the whole app just
 * unlocked: re-apply the UI and, if an email was already entered, validate with Connect.
 */
async function setApiKeyToBackend() {
    const input = document.getElementById('apiKeyInput');
    if (!input) return;

    const key = input.value.trim();
    if (!key) {
        updateConnectStatus('Please enter your API key first.', 'error');
        showNotification('API key required', 'error');
        return;
    }

    // Persist locally first so the UI unlocks even if the backend call is flaky.
    localStorage.setItem('coolems_api_key', key);
    input.value = key;

    updateConnectStatus('Saving API key...', 'info');

    try {
        // (2026-09-29) include the email when known so the CLIENT's bookkeeping file
        // (email, date_acquired - no key on disk) can record it. The X-User-Email header is injected by
        // the fetch override too; the body field is a fallback.
        const payload = { key: key };
        const storedEmail = getStoredEmail();
        if (storedEmail) payload.email = storedEmail;

        const response = await fetch('/api/auth/set-key', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ key: key })
        });

        let detail = '';
        try { detail = (await response.json()).detail || ''; } catch (e) {}

        if (!response.ok) {
            updateConnectStatus('Could not save API key.' + (detail ? ' ' + detail : ''), 'error');
            showNotification('Failed to set API key' + (detail ? ': ' + detail : ''), 'error');
            return;
        }

        // Key accepted by the backend - setup mode is over. Unlock the Settings UI.
        applySetupModeUI();
        updateConnectStatus('API key saved.', 'success');
        showNotification('API key set - you can now connect.', 'success');

        // If an email was already entered, run the normal validation right away.
        if (getStoredEmail()) {
            setTimeout(() => connectWithCredentials(), 300);
        }
    } catch (e) {
        updateConnectStatus('Could not save API key - is the CLIENT server running?', 'error');
        showNotification('Failed to set API key', 'error');
    }
}

function clearApiKey() {
    localStorage.removeItem('coolems_api_key');
    const input = document.getElementById('apiKeyInput');
    if (input) {
        input.value = '';
    }
    updateConnectStatus('API key cleared.', 'info');
    showNotification('API key removed. Please enter a new key.', 'warning');
    // Back to setup mode: only "Set API Key" stays available in the UI.
    applySetupModeUI();
}

function updateConnectStatus(message, type) {
    const el = document.getElementById('apiKeyStatus');
    if (!el) return;

    const colors = {
        'success': 'var(--success)',
        'error': 'var(--error)',
        'warning': 'var(--warning)',
        'info': 'var(--text-secondary)',
    };

    el.dataset.customized = '1'; // live message - applySetupModeUI must not overwrite it
    el.textContent = message;
    el.style.color = colors[type] || colors['info'];
}

/**
 * Connect button handler - validates BOTH email AND API key against the server.
 * The fetch override automatically injects X-API-Key and X-User-Email headers,
 * so the middleware's is_key_email_match() check runs on the server side.
 */
async function connectWithCredentials() {
    const key = getStoredApiKey();
    const email = getStoredEmail();

    // Validate inputs are present
    if (!email) {
        updateConnectStatus('Please enter your validation e-mail.', 'error');
        showNotification('Email required', 'error');
        return;
    }

    if (!key) {
        updateConnectStatus('Please set your API key first.', 'error');
        showNotification('API key required', 'error');
        return;
    }

    updateConnectStatus('Connecting...', 'info');

    try {
        // The fetch override injects X-API-Key and X-User-Email automatically.
        // Server middleware validates:
        //   1. is_api_key_valid(key) - key exists and is active
        //   2. is_key_email_match(key, email) - key belongs to the email
        const response = await fetch('/api/status', {
            headers: { 'X-API-Key': key, 'X-User-Email': email },
        });

        if (response.ok) {
            updateConnectStatus('Connected successfully.', 'success');
            showNotification('Connection established! Refreshing...', 'success');
            // 2026-09-20: after a successful connect, reload the page. Connections that
            // were opened BEFORE the key existed (WebSocket pool etc.) were established
            // without X-API-Key and stay stale until refresh - so even though the UI says
            // "connected", the key is not operational until then. A short delay lets the
            // user see the success message first; on reload every request/socket picks up
            // the stored key automatically via the fetch/WS overrides below.
            setTimeout(() => location.reload(), 1000);
        } else if (response.status === 401) {
            updateConnectStatus("Your data can't be validated. Please contact support for API key.", 'error');
            showNotification("Your data can't be validated. Please contact support for API key.", 'error');
        } else {
            updateConnectStatus('Server error (' + response.status + ').', 'error');
            showNotification('Server error', 'error');
        }
    } catch (e) {
        updateConnectStatus("Your data can't be validated. Please contact support for API key.", 'error');
        showNotification("Your data can't be validated. Please contact support for API key.", 'error');
    }
}

// ===== EMAIL STORAGE =====

function getStoredEmail() {
    return localStorage.getItem('coolems_email') || '';
}

function saveEmail() {
    const input = document.getElementById('emailInput');
    if (input) {
        localStorage.setItem('coolems_email', input.value.trim());
    }
}

function loadEmailToInput() {
    const email = getStoredEmail();
    const input = document.getElementById('emailInput');
    if (input && email) {
        input.value = email;
    }
}

// ===== SERVER ADDRESS MANAGEMENT =====

/**
 * Save server address to backend via API.
 * Called on onchange of the server address input field.
 */
async function saveServerAddress() {
    const input = document.getElementById('serverAddressInput');
    const statusEl = document.getElementById('serverAddressStatus');

    if (!input) return;

    const value = input.value.trim();

    // Clear previous status
    if (statusEl) {
        statusEl.textContent = 'Saving...';
        statusEl.style.color = 'var(--text-secondary)';
    }

    try {
        const response = await fetch('/api/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ server_address: value })
        });

        if (response.ok) {
            // Reload to show the freshly resolved address from settings.json
            await loadServerAddressFromApi();

            // Show success message with restart notice
            if (statusEl) {
                statusEl.textContent = '✓ Saved. Restart CLIENT to apply new server address.';
                statusEl.style.color = 'var(--success)';
            }
            showNotification('Server address saved. Restart required.', 'info');
        } else {
            const errData = await response.json().catch(() => ({}));
            if (statusEl) {
                statusEl.textContent = 'Failed: ' + (errData.detail || 'Unknown error');
                statusEl.style.color = 'var(--error)';
            }
        }
    } catch (e) {
        if (statusEl) {
            statusEl.textContent = 'Failed to save settings';
            statusEl.style.color = 'var(--error)';
        }
        console.error('[SERVER ADDRESS] Failed to save:', e);
    }
}

/**
 * Reset server address to empty (will use auto-detected LAN IP on restart).
 */
async function resetServerAddress() {
    const input = document.getElementById('serverAddressInput');
    const statusEl = document.getElementById('serverAddressStatus');

    if (!input) return;

    // Clear the input
    input.value = '';

    try {
        const response = await fetch('/api/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ server_address: '' })
        });

        if (response.ok) {
            // Reload to show the freshly auto-detected value from settings.json
            await loadServerAddressFromApi();

            if (statusEl) {
                statusEl.textContent = '✓ Reset. Will use auto-detected LAN IP on restart.';
                statusEl.style.color = 'var(--success)';
            }
        }
    } catch (e) {
        console.error('[SERVER ADDRESS] Failed to reset:', e);
    }
}

/**
 * Load server address from the API settings endpoint.
 * Uses resolved_server_address which is freshly re-resolved on each call
 * (reflects latest settings.json changes without requiring restart).
 */
async function loadServerAddressFromApi() {
    const input = document.getElementById('serverAddressInput');
    const statusEl = document.getElementById('serverAddressStatus');

    if (!input) return;

    try {
        const response = await fetch('/api/settings');
        if (response.ok) {
            const data = await response.json();

            // Use resolved_server_address - freshly re-resolved from settings.json + env var + auto-detect
            const activeAddr = data.resolved_server_address || data.current_server_address || '';

            // Always show the currently resolved address in the input field
            input.value = activeAddr;

            // (2026-09-23) fill the Connection Mode menu from the same response.
            applyConnectionModeUI(data);

            if (statusEl) {
                if (data.server_address && data.server_address !== '') {
                    // User has a saved custom address
                    if (data.server_address === activeAddr) {
                        statusEl.textContent = 'Active server address: ' + activeAddr;
                        statusEl.style.color = 'var(--text-secondary)';
                    } else {
                        statusEl.textContent = 'Saved: ' + data.server_address + ' | Active: ' + activeAddr;
                        statusEl.style.color = 'var(--warning)';
                    }
                } else if (activeAddr && activeAddr !== '') {
                    // No saved address - auto-detected mode
                    if (activeAddr.includes('127.0.0.1') || activeAddr.includes('localhost')) {
                        statusEl.textContent = 'Auto-detected: ' + activeAddr;
                        statusEl.style.color = 'var(--success)';
                    } else {
                        statusEl.textContent = 'Auto-detected LAN IP: ' + activeAddr;
                        statusEl.style.color = 'var(--success)';
                    }
                } else {
                    statusEl.textContent = 'No server address configured';
                    statusEl.style.color = 'var(--warning)';
                }
            }
        }
    } catch (e) {
        console.error('[SERVER ADDRESS] Failed to load from API:', e);
    }
}

// ===== CONNECTION MODE MANAGEMENT (2026-09-23) =====

/**
 * Fill the Connection Mode UI from a /api/settings response.
 * Defaults when fields are absent: direct mode, empty relay host/port.
 */
function applyConnectionModeUI(data) {
    // (2026-09-23) WEB_RELAY_UI_ENABLED=False on the CLIENT hides the whole Connection
    // Mode group - the relay option must not be visible at all. Direct mode stays fully
    // functional via the Server Address field, which we make sure remains shown.
    if (data.web_relay_ui_enabled === false) {
        const cmGroup = document.getElementById('connectionModeGroup');
        if (cmGroup) cmGroup.style.display = 'none';
        const srvGroup = document.getElementById('serverAddressGroup');
        if (srvGroup) srvGroup.style.display = 'block';
    }
    const radioDirect = document.getElementById('connModeDirect');
    const radioRelay = document.getElementById('connModeRelay');
    if (!radioDirect || !radioRelay) return;

    const mode = (data.connection_mode === 'web_relay') ? 'web_relay' : 'direct';
    radioDirect.checked = (mode === 'direct');
    radioRelay.checked = (mode === 'web_relay');

    const hostInput = document.getElementById('relayHostInput');
    const portInput = document.getElementById('relayPortInput');
    if (hostInput) hostInput.value = data.relay_host || '';
    if (portInput) portInput.value = (data.relay_port === 0 || data.relay_port === '') ? '' : String(data.relay_port);

    toggleRelayFields(mode === 'web_relay');
}

function toggleRelayFields(show) {
    const group = document.getElementById('relayFieldsGroup');
    if (group) group.style.display = show ? 'block' : 'none';
    // In relay mode the home-SERVER address is irrelevant - hide it so users only
    // ever see the one field that matters: the web relay address.
    const serverGroup = document.getElementById('serverAddressGroup');
    if (serverGroup) serverGroup.style.display = show ? 'none' : 'block';
}

/**
 * Save the Connection Mode selection (+ relay host/port when in web_relay mode).
 * Called on radio change and on host/port blur. The backend validates:
 *   - connection_mode must be 'direct' | 'web_relay'
 *   - web_relay requires a non-empty relay_host
 *   - relay_port (when given) must be 1-65535
 */
async function saveConnectionMode() {
    const radioDirect = document.getElementById('connModeDirect');
    const radioRelay = document.getElementById('connModeRelay');
    const statusEl = document.getElementById('connectionModeStatus');
    if (!radioDirect || !radioRelay) return;

    const mode = (radioRelay.checked) ? 'web_relay' : 'direct';
    toggleRelayFields(mode === 'web_relay');

    const hostInput = document.getElementById('relayHostInput');
    const portInput = document.getElementById('relayPortInput');
    const relay_host = hostInput ? hostInput.value.trim() : '';

    if (statusEl) {
        statusEl.textContent = 'Saving...';
        statusEl.style.color = 'var(--text-secondary)';
    }

    const payload = { connection_mode: mode, relay_host: relay_host };
    if (portInput) payload.relay_port = portInput.value.trim();

    try {
        const response = await fetch('/api/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });

        if (response.ok) {
            if (statusEl) {
                statusEl.textContent = mode === 'web_relay'
                    ? ('\u2713 Saved. Connecting via relay: ' + (relay_host || '(no host saved yet)'))
                    : '\u2713 Saved. Direct LAN mode.';
                statusEl.style.color = 'var(--success)';
            }
            showNotification(mode === 'web_relay' ? 'Connection mode: Internet Web Relay' : 'Connection mode: Direct', 'info');
        } else {
            const errData = await response.json().catch(() => ({}));
            if (statusEl) {
                statusEl.textContent = 'Not saved: ' + (errData.detail || 'Unknown error') + ' - selection reverts to the last saved mode on reload.';
                statusEl.style.color = 'var(--error)';
            }
        }
    } catch (e) {
        if (statusEl) {
            statusEl.textContent = 'Failed to save connection mode';
            statusEl.style.color = 'var(--error)';
        }
        console.error('[CONNECTION MODE] Failed to save:', e);
    }
}

    // ===== INJECT API KEY INTO ALL REQUESTS =====

// Override fetch to automatically add API key header
(function () {
    const originalFetch = window.fetch;

    window.fetch = function (url, options) {
        const key = getStoredApiKey();

        // Skip adding key for non-API paths
        if (!url.startsWith('/api/') && !url.startsWith('/ws/')) {
            return originalFetch(url, options);
        }

        // Don't add key if empty
        if (!key) {
            return originalFetch(url, options);
        }

        options = options || {};
        options.headers = options.headers || {};

        // Use Headers object if available, otherwise plain object
        if (options.headers instanceof Headers) {
            options.headers.set('X-API-Key', key);
            const email = getStoredEmail();
            if (email) {
                options.headers.set('X-User-Email', email);
            }
        } else if (typeof options.headers === 'object') {
            options.headers['X-API-Key'] = key;
            const email = getStoredEmail();
            if (email) {
                options.headers['X-User-Email'] = email;
            }
        }

        return originalFetch(url, options);
    };
})();

// ===== INJECT API KEY INTO WEBSOCKET URL =====

function getWebSocketUrlWithKey(path) {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const key = getStoredApiKey();
    const email = getStoredEmail();
    const separator = path.includes('?') ? '&' : '?';
    let url = protocol + '//' + window.location.host + path;
    if (key) {
        url = url + separator + 'api_key=' + encodeURIComponent(key);
        if (email) {
            url = url + '&email=' + encodeURIComponent(email);
        }
    }
    return url;
}

// ===== INIT ON DOM LOAD =====

document.addEventListener('DOMContentLoaded', function () {
    loadEmailToInput();
    loadApiKeyToInput();

    // SETUP MODE (2026-09-01): no key stored yet -> lock the UI to "Set API Key" only.
    applySetupModeUI();

    if (!getStoredApiKey()) {
        showNotification('No API key set yet - open Settings and set your API key.', 'warning');
    }

    // Load server address from API on page load (background, non-blocking).
    // Skipped in setup mode: /api/settings is locked until a key exists.
    if (typeof loadServerAddressFromApi === 'function' && getStoredApiKey()) {
        setTimeout(() => loadServerAddressFromApi(), 1000);
    }

    // Override getWebSocketUrl to inject API key (must be after app.js loads)
    if (typeof window.getWebSocketUrl === 'function') {
        const originalWS = window.getWebSocketUrl;
        window.getWebSocketUrl = function (path) {
            return getWebSocketUrlWithKey(path);
        };
    }
});
