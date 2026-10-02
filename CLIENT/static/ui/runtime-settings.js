/**
 * runtime-settings.js - Settings > Runtime tab (2026-10-02).
 *
 * First setting: PYTHON DIALOG BEHAVIOR for the python_exec approval dialog.
 *   manual (default) -> the dialog waits until the user decides (original behavior, no timeout).
 *   auto             -> the dialog still opens, but after N seconds it approves itself and runs.
 *
 * Values live in settings.json via GET/POST /api/settings (keys: python_exec_auto_approve,
 * python_exec_auto_approve_seconds) and are read LIVE by websocket.js when a new approval
 * card appears - so a save applies to the very next dialog without any restart.
 */

// Live view of the saved values; websocket.js reads this when rendering an approval card.
COOLEMS.runtimeSettings = { mode: 'manual', seconds: 5 };

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

    toggleRuntimeSecondsGroup(mode === 'auto');
    COOLEMS.runtimeSettings = { mode: mode, seconds: seconds };
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
                python_exec_auto_approve_seconds: seconds
            })
        });

        if (response.ok) {
            COOLEMS.runtimeSettings = { mode: mode, seconds: seconds };
            if (statusEl) {
                statusEl.textContent = (mode === 'auto')
                    ? ('\u2713 Saved. python_exec dialogs will auto-approve after ' + seconds + 's.')
                    : '\u2713 Saved. python_exec dialogs wait for your decision.';
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
