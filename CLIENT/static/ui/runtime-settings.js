/**
 * runtime-settings.js - Settings > Runtime tab (2026-10-02).
 *
 * First setting: PYTHON DIALOG BEHAVIOR for the python_exec approval dialog.
 *   manual (default) -> the dialog waits until the user decides (original behavior, no timeout).
 *   auto             -> the dialog still opens, but after N seconds it approves itself and runs.
 *
 * Second setting (2026-10-03): IMAGE GENERATOR for generate_image().
 *   Model line: auto (default) -> classify by probed GPU VRAM (high / mid / low);
 *               high | mid | low -> pin that model line (falls back to the auto tier if
 *               the card can't afford it).
 *   Default resolution: image_gen_width x image_gen_height pixels (defaults 600x400) -
 *   used whenever no explicit size is requested; read LIVE by generate_image() on every
 *   call. The worker snaps values down to multiples of 32 and clamps them to the tier's
 *   max_pixels budget, so oversized values degrade gracefully instead of breaking.
 *
 * Values live in settings.json via GET/POST /api/settings (keys: python_exec_auto_approve,
 * python_exec_auto_approve_seconds, image_gen_tier, image_gen_width, image_gen_height)
 * and are read LIVE by websocket.js / generate_image() on every use - so a save applies
 * immediately without any restart.
 */

// Live view of the saved values; websocket.js reads this when rendering an approval card.
// (2026-10-05 first-run fix) The defaults below are only a PLACEHOLDER for the first moments
// after page load - refreshRuntimeSettingsFromApi() replaces them with the real settings.json
// values, and websocket.js awaits that sync (ensureRuntimeSettingsLoaded) BEFORE deciding
// whether to start an auto-approve countdown. Before this fix COOLEMS.runtimeSettings stayed
// 'manual' until the user opened Settings > Runtime, so on a fresh page load the FIRST
// python_exec dialog showed "no time limit" even when settings.json said "auto after 5s".
// approvals[kind] (2026-07-18 unified consent dialog): per-kind auto-approve settings,
// fully detached per category - python / url / file / network each have their own mode+seconds.
COOLEMS.runtimeSettings = { mode: 'manual', seconds: 5, imageGenTier: 'auto', imageGenWidth: 600, imageGenHeight: 400, approvals: { python: { mode: 'manual', seconds: 5 }, url: { mode: 'manual', seconds: 5 }, file: { mode: 'manual', seconds: 5 }, network: { mode: 'manual', seconds: 5 }, subprocess: { mode: 'manual', seconds: 5 } };  // (2026-10-24) subprocess consent cards
COOLEMS._runtimeSettingsLoaded = false;   // true once a /api/settings sync succeeded this page session
let _runtimeRefreshInFlight = null;       // in-flight promise guard (no duplicate concurrent fetches)

/**
 * Sync COOLEMS.runtimeSettings from GET /api/settings and, when the Runtime tab is open,
 * fill its controls. Returns true on a successful backend read, false otherwise - on
 * failure the LAST KNOWN GOOD values are kept (never reset to defaults mid-session).
 */
async function refreshRuntimeSettingsFromApi() {
    let data = null;
    try {
        const r = await fetch('/api/settings');
        if (!r.ok) return false;
        data = await r.json();
    } catch (e) {
        console.error('[RUNTIME SETTINGS] Failed to load:', e);
        return false;
    }

    // python_exec dialog behavior (manual | auto + delay in seconds).
    const mode = (data && data.python_exec_auto_approve === 'auto') ? 'auto' : 'manual';
    let seconds = 5;
    if (data && Number.isFinite(parseInt(data.python_exec_auto_approve_seconds, 10))) {
        seconds = parseInt(data.python_exec_auto_approve_seconds, 10);
    }

    // (2026-07-18 unified consent dialog) per-kind auto-approve pairs - fully detached.
    const approvals = {};
    ['python', 'url', 'file', 'network', 'subprocess'].forEach(function (kind) {  // (2026-10-24)
        let m = 'manual';
        let s = 5;
        if (kind === 'python') {   // historical key names
            m = mode;
            s = seconds;
        } else {
            const mk = kind + '_consent_auto_approve';
            const sk = kind + '_consent_auto_approve_seconds';
            if (data && data[mk] === 'auto') m = 'auto';
            if (data && Number.isFinite(parseInt(data[sk], 10))) s = parseInt(data[sk], 10);
        }
        approvals[kind] = { mode: m, seconds: Math.min(3600, Math.max(1, s)) };
    });

    // Image Generator values (tier + default resolution).
    const tier = (data && ['auto', 'high', 'mid', 'low'].indexOf(data.image_gen_tier) !== -1)
        ? data.image_gen_tier : 'auto';
    let imgWidth = _coerceImageGenSize(data && data.image_gen_width, 600);
    let imgHeight = _coerceImageGenSize(data && data.image_gen_height, 400);

    COOLEMS.runtimeSettings = { mode: mode, seconds: seconds, imageGenTier: tier, imageGenWidth: imgWidth, imageGenHeight: imgHeight, approvals: approvals };
    COOLEMS._runtimeSettingsLoaded = true;

    // Fill the Runtime tab controls when they are in the DOM (openSettings path).
    const manualRadio = document.getElementById('pyExecManual');
    const autoRadio = document.getElementById('pyExecAuto');
    if (manualRadio && autoRadio) {
        manualRadio.checked = (mode === 'manual');
        autoRadio.checked = (mode === 'auto');

        const secInput = document.getElementById('pyExecAutoSeconds');
        if (secInput) secInput.value = String(seconds);

        // (2026-07-18) fill the three per-kind consent rows (url / file / network).
        ['url', 'file', 'network', 'subprocess'].forEach(function (kind) {  // subprocess row added 2026-10-24
            const a = approvals[kind];
            const manualEl = document.getElementById(kind + 'ConsentManual');
            const autoEl = document.getElementById(kind + 'ConsentAuto');
            if (manualEl && autoEl) {
                manualEl.checked = (a.mode === 'manual');
                autoEl.checked = (a.mode === 'auto');
                const secInput = document.getElementById(kind + 'ConsentSeconds');
                if (secInput) secInput.value = String(a.seconds);
                toggleConsentSecondsGroup(kind, a.mode === 'auto');
            }
        });

        setImageGenTierRadio(tier);

        const widthInput = document.getElementById('imgGenWidth');
        const heightInput = document.getElementById('imgGenHeight');
        if (widthInput) widthInput.value = String(imgWidth);
        if (heightInput) heightInput.value = String(imgHeight);

        toggleRuntimeSecondsGroup(mode === 'auto');
    }

    return true;
}

/**
 * (2026-10-05 first-run fix) Await ONE successful /api/settings sync before the caller reads
 * COOLEMS.runtimeSettings. websocket.js calls this right before rendering a python_exec
 * approval card, so the SAVED auto-approve setting is honored even on the very first dialog
 * of a fresh page load - without the user ever opening Settings > Runtime. Concurrent
 * callers share one in-flight fetch; after success it resolves immediately (no re-fetch).
 */
function ensureRuntimeSettingsLoaded() {
    if (COOLEMS._runtimeSettingsLoaded) return Promise.resolve(true);
    if (!_runtimeRefreshInFlight) {
        _runtimeRefreshInFlight = refreshRuntimeSettingsFromApi().finally(() => {
            _runtimeRefreshInFlight = null;   // cleared on settle -> a failed sync is retried next time
        });
    }
    return _runtimeRefreshInFlight;
}

/** Fill the Runtime tab UI from a /api/settings response (called on every openSettings). */
async function loadRuntimeSettings() {
    await refreshRuntimeSettingsFromApi(); // openSettings path: always take the freshest values from the backend
}

// (2026-10-03) Check the ONE image-tier radio matching *tier* and uncheck the rest, so the
// UI can never show two tiers selected at once. Returns nothing; safe if pane not in DOM.
function setImageGenTierRadio(tier) {
    ['imgTierAuto', 'imgTierHigh', 'imgTierMid', 'imgTierLow'].forEach(function (id) {
        const el = document.getElementById(id);
        if (el) el.checked = (el.value === tier);
    });
}

// (2026-10-03) Coerce a default-resolution value to an int in 32..4096, else *fallback*.
function _coerceImageGenSize(value, fallback) {
    const v = parseInt(value, 10);
    if (!Number.isFinite(v)) return fallback;
    return Math.min(4096, Math.max(32, v));
}

function toggleRuntimeSecondsGroup(show) {
    const group = document.getElementById('pyExecAutoSecondsGroup');
    if (group) group.style.display = show ? 'flex' : 'none';
}

// (2026-07-18 unified consent dialog) per-kind rows: show/hide the seconds input for one kind.
function toggleConsentSecondsGroup(kind, show) {
    const group = document.getElementById(kind + 'ConsentSecondsGroup');
    if (group) group.style.display = show ? 'flex' : 'none';
}

// Read all four per-kind consent settings from the Runtime tab controls (python uses its
// historical radio/seconds ids). Returns approvals[kind] = {mode, seconds}.
function readApprovalsFromControls() {
    const out = {};
    // python: legacy control names.
    const pyAuto = document.getElementById('pyExecAuto');
    const pySec = document.getElementById('pyExecAutoSeconds');
    let pyMode = (pyAuto && pyAuto.checked) ? 'auto' : 'manual';
    let pySeconds = pySec ? parseInt(pySec.value, 10) : NaN;
    if (!Number.isFinite(pySeconds)) pySeconds = 5;
    out.python = { mode: pyMode, seconds: Math.min(3600, Math.max(1, pySeconds)) };
        ['url', 'file', 'network', 'subprocess'].forEach(function (kind) {  // subprocess row added 2026-10-24
        const autoEl = document.getElementById(kind + 'ConsentAuto');
        const secInput = document.getElementById(kind + 'ConsentSeconds');
        let s = secInput ? parseInt(secInput.value, 10) : NaN;
        if (!Number.isFinite(s)) s = 5;
        out[kind] = { mode: (autoEl && autoEl.checked) ? 'auto' : 'manual', seconds: Math.min(3600, Math.max(1, s)) };
    });
    return out;
}

/** Save the current Runtime tab selection to settings.json. Called on any radio/seconds change. */
async function saveRuntimeSettings() {
    const manualRadio = document.getElementById('pyExecManual');
    const autoRadio = document.getElementById('pyExecAuto');
    if (!manualRadio || !autoRadio) return;

    const mode = (autoRadio.checked) ? 'auto' : 'manual';
    toggleRuntimeSecondsGroup(mode === 'auto');

    // (2026-10-03) Read the selected image-gen tier radio ('auto' when none is checked).
    let imageGenTier = 'auto';
    ['imgTierAuto', 'imgTierHigh', 'imgTierMid', 'imgTierLow'].forEach(function (id) {
        const el = document.getElementById(id);
        if (el && el.checked) imageGenTier = el.value;
    });

    // Clamp the seconds value to 1..3600 before sending.
    const secInput = document.getElementById('pyExecAutoSeconds');
    let seconds = secInput ? parseInt(secInput.value, 10) : NaN;
    if (!Number.isFinite(seconds)) seconds = 5;
    seconds = Math.min(3600, Math.max(1, seconds));
    if (secInput && String(seconds) !== secInput.value.trim()) secInput.value = String(seconds);

    // (2026-07-18 unified consent dialog) per-kind pairs for all 4 kinds (read from controls).
    const approvalsNow = readApprovalsFromControls();

    // (2026-11-24 fix) show/hide EACH kind's seconds input when its radio changes -
    // previously only the python group was toggled, so selecting "Auto-approve after"
    // on URL / File Access / Network never revealed their seconds field.
        ['url', 'file', 'network', 'subprocess'].forEach(function (kind) {  // subprocess row added 2026-10-24
        toggleConsentSecondsGroup(kind, approvalsNow[kind].mode === 'auto');
    });

    // (2026-10-03) Read the default resolution inputs, clamped to 32..4096 px.
    let imgWidth = _coerceImageGenSize(document.getElementById('imgGenWidth') && document.getElementById('imgGenWidth').value, 600);
    let imgHeight = _coerceImageGenSize(document.getElementById('imgGenHeight') && document.getElementById('imgGenHeight').value, 400);
    const widthInput = document.getElementById('imgGenWidth');
    const heightInput = document.getElementById('imgGenHeight');
    if (widthInput) widthInput.value = String(imgWidth);
    if (heightInput) heightInput.value = String(imgHeight);

    try {
        const response = await fetch('/api/settings', {
            method: 'POST',
            // No manual auth headers needed: the global fetch override in api-key.js
            // injects X-API-Key / X-User-Email for every /api/ request automatically.
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(Object.assign({
                python_exec_auto_approve: mode,
                python_exec_auto_approve_seconds: seconds,
                image_gen_tier: imageGenTier,
                image_gen_width: imgWidth,
                image_gen_height: imgHeight
            }, {
                    url_consent_auto_approve: approvalsNow.url.mode,
                    url_consent_auto_approve_seconds: approvalsNow.url.seconds,
                    file_consent_auto_approve: approvalsNow.file.mode,
                    file_consent_auto_approve_seconds: approvalsNow.file.seconds,
                    network_consent_auto_approve: approvalsNow.network.mode,
                    network_consent_auto_approve_seconds: approvalsNow.network.seconds
            }))
        });

        // Silent by design: the Runtime tab shows no save feedback in the UI.
        if (response.ok) {
            COOLEMS.runtimeSettings = { mode: mode, seconds: seconds, imageGenTier: imageGenTier, imageGenWidth: imgWidth, imageGenHeight: imgHeight, approvals: readApprovalsFromControls() };
        } else {
            const errData = await response.json().catch(() => ({}));
            console.error('[RUNTIME SETTINGS] Save rejected:', errData.detail || 'Unknown error');
        }
    } catch (e) {
        console.error('[RUNTIME SETTINGS] Failed to save:', e);
    }
}
