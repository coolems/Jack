/**
 * runtime-settings.js - Settings > Runtime tab (2026-10-02).
 *
 * First setting: PYTHON DIALOG BEHAVIOR for the python_exec approval dialog.
 *   manual (default) -> the dialog waits until the user decides (original behavior, no timeout).
 *   auto             -> the dialog still opens, but after N seconds it approves itself and runs.
 *
 * Second setting (2026-10-03): IMAGE GENERATOR TIER for generate_image().
 *   auto (default) -> classify by probed GPU VRAM (high / mid / low).
 *   high | mid | low -> pin that model line (falls back to the auto tier if the card can't afford it).
 *
 * Values live in settings.json via GET/POST /api/settings (keys: python_exec_auto_approve,
 * python_exec_auto_approve_seconds, image_gen_tier) and are read LIVE by websocket.js /
 * generate_image() on every use - so a save applies immediately without any restart.
 */

// Live view of the saved values; websocket.js reads this when rendering an approval card.
COOLEMS.runtimeSettings = { mode: 'manual', seconds: 5, imageGenTier: 'auto' };

function _runtimeStatusEl() {
    return document.getElementById('runtimeSettingsStatus');
}

/** Fill the Runtime tab UI from a /api/settings response (called on every openSettings). */
async function loadRuntimeSettings() {
    const manualRadio = document.getElementById('pyExecManual');
    const autoRadio = document.getElementById('pyExecAuto');
    if (!manualRadio || !autoRadio) return; // pane not in DOM yet

    let data = null;
    try {
        const r = await fetch('/api/settings');
        if (r.ok) data = await r.json();
    } catch (e) {
        console.error('[RUNTIME SETTINGS] Failed to load:', e);
    }

    const mode = (data && data.python_exec_auto_approve === 'auto') ? 'auto' : 'manual';
    let seconds = 5;
    if (data && Number.isFinite(parseInt(data.python_exec_auto_approve_seconds, 10))) {
        seconds = parseInt(data.python_exec_auto_approve_seconds, 10);
    }

    manualRadio.checked = (mode === 'manual');
    autoRadio.checked = (mode === 'auto');

    const secInput = document.getElementById('pyExecAutoSeconds');
    if (secInput) secInput.value = String(seconds);

    // (2026-10-03) Image Generator Tier radios - restore the saved pick.
    const tier = (data && ['auto', 'high', 'mid', 'low'].indexOf(data.image_gen_tier) !== -1)
        ? data.image_gen_tier : 'auto';
    setImageGenTierRadio(tier);

    toggleRuntimeSecondsGroup(mode === 'auto');
    COOLEMS.runtimeSettings = { mode: mode, seconds: seconds, imageGenTier: tier };
}

// (2026-10-03) Check the ONE image-tier radio matching *tier* and uncheck the rest, so the
// UI can never show two tiers selected at once. Returns nothing; safe if pane not in DOM.
function setImageGenTierRadio(tier) {
    ['imgTierAuto', 'imgTierHigh', 'imgTierMid', 'imgTierLow'].forEach(function (id) {
        const el = document.getElementById(id);
        if (el) el.checked = (el.value === tier);
    });
}

function toggleRuntimeSecondsGroup(show) {
    const group = document.getElementById('pyExecAutoSecondsGroup');
    if (group) group.style.display = show ? 'flex' : 'none';
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

    const statusEl = _runtimeStatusEl();
    if (statusEl) {
        statusEl.textContent = 'Saving...';
        statusEl.style.color = 'var(--text-secondary)';
    }

    try {
        const response = await fetch('/api/settings', {
            method: 'POST',
            // No manual auth headers needed: the global fetch override in api-key.js
            // injects X-API-Key / X-User-Email for every /api/ request automatically.
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                python_exec_auto_approve: mode,
                python_exec_auto_approve_seconds: seconds,
                image_gen_tier: imageGenTier
            })
        });

        if (response.ok) {
            COOLEMS.runtimeSettings = { mode: mode, seconds: seconds, imageGenTier: imageGenTier };
            if (statusEl) {
                statusEl.textContent = '\u2713 Saved. ' +
                    ((mode === 'auto')
                        ? ('python_exec dialogs will auto-approve after ' + seconds + 's.')
                        : 'python_exec dialogs wait for your decision.') +
                    ' Image tier: ' + imageGenTier;
                statusEl.style.color = 'var(--success)';
            }
        } else {
            const errData = await response.json().catch(() => ({}));
            if (statusEl) {
                statusEl.textContent = 'Not saved: ' + (errData.detail || 'Unknown error');
                statusEl.style.color = 'var(--error)';
            }
        }
    } catch (e) {
        if (statusEl) {
            statusEl.textContent = 'Failed to save runtime settings';
            statusEl.style.color = 'var(--error)';
        }
        console.error('[RUNTIME SETTINGS] Failed to save:', e);
    }
}
