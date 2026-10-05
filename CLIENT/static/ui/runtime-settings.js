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
COOLEMS.runtimeSettings = { mode: 'manual', seconds: 5, imageGenTier: 'auto', imageGenWidth: 600, imageGenHeight: 400 };

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


    // (2026-10-03) Image Generator radios - restore the saved pick.
    const tier = (data && ['auto', 'high', 'mid', 'low'].indexOf(data.image_gen_tier) !== -1)
        ? data.image_gen_tier : 'auto';
    setImageGenTierRadio(tier);

    // (2026-10-03) Default resolution inputs - restore the saved values (defaults 600x400).
    let imgWidth = _coerceImageGenSize(data && data.image_gen_width, 600);
    let imgHeight = _coerceImageGenSize(data && data.image_gen_height, 400);
    const widthInput = document.getElementById('imgGenWidth');
    const heightInput = document.getElementById('imgGenHeight');
    if (widthInput) widthInput.value = String(imgWidth);
    if (heightInput) heightInput.value = String(imgHeight);

    toggleRuntimeSecondsGroup(mode === 'auto');
    COOLEMS.runtimeSettings = { mode: mode, seconds: seconds, imageGenTier: tier, imageGenWidth: imgWidth, imageGenHeight: imgHeight };
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
            body: JSON.stringify({
                python_exec_auto_approve: mode,
                python_exec_auto_approve_seconds: seconds,
                image_gen_tier: imageGenTier,
                image_gen_width: imgWidth,
                image_gen_height: imgHeight
            })
        });

        // Silent by design: the Runtime tab shows no save feedback in the UI.
        if (response.ok) {
            COOLEMS.runtimeSettings = { mode: mode, seconds: seconds, imageGenTier: imageGenTier, imageGenWidth: imgWidth, imageGenHeight: imgHeight };
        } else {
            const errData = await response.json().catch(() => ({}));
            console.error('[RUNTIME SETTINGS] Save rejected:', errData.detail || 'Unknown error');
        }
    } catch (e) {
        console.error('[RUNTIME SETTINGS] Failed to save:', e);
    }
}
