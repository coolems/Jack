/**
 * search-engine-settings.js - Settings UI for config/search_engines.json (on/off only).
 * The JSON file is the single source of truth: each engine renders as one clean row
 * with its display name and a toggle switch. Flipping a toggle saves instantly via
 * POST /api/search-engines - the full list is sent back, so id/priority/settings are
 * preserved untouched. Adding/removing engines or editing advanced settings = edit
 * config/search_engines.json directly (no restart needed).
 */

let seEngines = []; // full engine objects from the JSON file; only `enabled` gets flipped here
let seSaving = false;

function seStatus(msg, isError) {
    const el = document.getElementById('searchEngineStatus');
    if (!el) return;
    el.textContent = msg || '';
    el.style.color = isError ? 'var(--danger)' : 'var(--text-secondary)';
}

/** Render one minimal row: display name on the left, toggle switch on the right. */
function seBuildRow(engine) {
    const div = document.createElement('div');
    div.className = 'se-row' + (engine.enabled ? '' : ' se-off');
    div.dataset.id = engine.id || '';

    const nameEl = document.createElement('span');
    nameEl.className = 'se-name';
    nameEl.textContent = engine.name || engine.id; // textContent -> no HTML injection
    nameEl.title = (engine.settings && Object.keys(engine.settings).length) ? 'Advanced settings live in config/search_engines.json' : '';

    const toggleWrap = document.createElement('div');
    toggleWrap.className = 'toggle se-toggle';
    const sw = document.createElement('div');
    sw.className = 'toggle-switch' + (engine.enabled ? ' active' : '');
    const thumb = document.createElement('div');
    thumb.className = 'toggle-thumb';
    sw.appendChild(thumb);
    toggleWrap.appendChild(sw);
    if (engine.id) {
        toggleWrap.onclick = () => seToggleEngine(engine.id, div);
    } else {
        toggleWrap.style.opacity = '0.4'; // no id -> nothing to save; ignore clicks
    }

    div.appendChild(nameEl);
    div.appendChild(toggleWrap);
    return div;
}

/** Update a row's visual state (dim name + switch position) without re-rendering. */
function seApplyRowState(rowEl, enabled) {
    if (!rowEl) return;
    rowEl.classList.toggle('se-off', !enabled);
    const sw = rowEl.querySelector('.toggle-switch');
    if (sw) sw.classList.toggle('active', !!enabled);
}

/** Render the cached engine list into #searchEngineList (priority order). */
function seRenderList(list) {
    list.innerHTML = '';
    if (!seEngines.length) {
        const empty = document.createElement('div');
        empty.className = 'se-row';
        empty.style.color = 'var(--text-secondary)';
        const span = document.createElement('span');
        span.className = 'se-name';
        span.textContent = 'No search engines configured - add some in config/search_engines.json.';
        empty.appendChild(span);
        list.appendChild(empty);
        seStatus('');
        return;
    }
    const sorted = [...seEngines].sort((a, b) => ((a.priority || 99) - (b.priority || 99)));
    for (const e of sorted) list.appendChild(seBuildRow(e));
    seStatus(seEngines.length + ' engine(s) from config/search_engines.json - toggle to enable/disable.');
}

/** Load the JSON via API and render all rows into #searchEngineList. */
async function loadSearchEngineSettings() {
    const list = document.getElementById('searchEngineList');
    if (!list) return;
    seStatus('Loading search engines...');
    try {
        const r = await fetch('/api/search-engines');
        const data = await r.json();
        if (!r.ok) throw new Error(data.detail || ('HTTP ' + r.status));
        seEngines = Array.isArray(data.engines) ? data.engines : [];
        seRenderList(list);
    } catch (e) {
        list.innerHTML = '';
        const err = document.createElement('div');
        err.className = 'se-row';
        const span = document.createElement('span');
        span.className = 'se-name';
        span.style.color = 'var(--danger)';
        span.textContent = 'Could not load search engines: ' + e.message;
        err.appendChild(span);
        list.appendChild(err);
        seStatus('Load failed - check the server is running.', true);
    }
}

/** Flip one engine's enabled flag and save the whole list instantly (reverts on error). */
async function seToggleEngine(id, rowEl) {
    if (seSaving) return; // one save at a time - keeps UI and file in sync
    const eng = seEngines.find(e => e.id === id);
    if (!eng) return;

    seSaving = true;
    eng.enabled = !eng.enabled;       // optimistic update
    seApplyRowState(rowEl, eng.enabled);
    seStatus('Saving...');
    try {
        const r = await fetch('/api/search-engines', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ engines: seEngines })
        });
        const data = await r.json();
        if (!r.ok) throw new Error(data.detail || ('HTTP ' + r.status));
        seStatus('Saved - changes apply immediately (no restart needed).');
    } catch (e) {
        eng.enabled = !eng.enabled;   // revert on failure
        seApplyRowState(rowEl, eng.enabled);
        seStatus('Save failed: ' + e.message, true);
    } finally {
        seSaving = false;
    }
}

window.loadSearchEngineSettings = loadSearchEngineSettings;
window.seToggleEngine = seToggleEngine;
