/**
 * mcp-settings.js - Settings > MCP tab (2026-12): manage config/mcp_servers.json live.
 *
 * GET  /api/mcp/servers -> every entry (enabled AND disabled) + its live connection state,
 *                          rendered as one row per server with an enable toggle.
 * POST /api/mcp/servers -> full replacement of the "servers" list. The backend validates
 *                          each entry with the SAME validator startup uses, writes the file
 *                          and re-wires the registry immediately - no restart needed. On any
 *                          validation problem nothing is written (400 + reasons).
 *
 * config/mcp_servers.json stays the single source of truth for later use; this tab is a
 * convenience over it (advanced fields like env/cwd are edited in the file directly).
 * Enabling a server here is still a HUMAN decision, and the first use of its tools still
 * asks a consent card - nothing auto-runs.
 */

let mcpServers = [];        // full entries from GET /api/mcp/servers (incl. env/cwd)
let mcpProblems = [];       // config problems reported by the backend
let mcpEditingName = null;  // name of the server being edited, or null when adding new
let mcpSaving = false;      // one save at a time - keeps UI and file in sync

const MCP_NAME_RE = /^[A-Za-z0-9_.-]{1,40}$/;

function mcpStatus(msg, isError) {
    const el = document.getElementById('mcpStatus');
    if (!el) return;
    el.textContent = msg || '';
    el.style.color = isError ? 'var(--danger)' : 'var(--text-secondary)';
}

/** One-line launch summary for a server row. */
function mcpSummary(s) {
    if (s.transport === 'http') return s.url || '(no url set)';
    const parts = [s.command].concat(Array.isArray(s.args) ? s.args : []);
    return parts.join(' ') || '(no command set)';
}

/** Update one row's visual state without re-rendering the whole list. */
function mcpApplyRowState(rowEl, s) {
    if (!rowEl) return;
    rowEl.classList.toggle('se-off', !s.enabled);
    if (rowEl._sw) rowEl._sw.classList.toggle('active', !!s.enabled);
    const st = rowEl._stateEl;
    if (!st) return;
    if (!s.enabled) {
        st.textContent = 'disabled - toggle on to start it';
        st.style.color = 'var(--text-secondary)';
    } else if (s.connected) {
        st.textContent = 'connected - ' + s.tools + ' tool(s)';
        st.style.color = 'var(--success, #4caf50)';
    } else {
        st.textContent = 'not connected' + (s.error ? ' - ' + s.error : '');
        st.style.color = 'var(--danger)';
    }
}

/** One row: name + transport (click to edit), launch summary, live state, enable toggle. */
function mcpBuildRow(s) {
    const div = document.createElement('div');
    div.className = 'se-row' + (s.enabled ? '' : ' se-off');
    div.dataset.name = s.name;
    div.style.cssText = 'display:flex; align-items:center; gap:8px;';

    const nameEl = document.createElement('span');
    nameEl.className = 'se-name';
    nameEl.textContent = s.name + '  [' + s.transport + ']';
    nameEl.title = 'Click to edit - ' + mcpSummary(s);
    nameEl.style.cursor = 'pointer';
    nameEl.onclick = () => mcpStartEdit(s.name);

    const mid = document.createElement('div');
    mid.style.cssText = 'flex:1; min-width:0; display:flex; flex-direction:column; gap:2px; overflow:hidden;';
    const sumEl = document.createElement('span');
    sumEl.className = 'se-name';
    sumEl.textContent = mcpSummary(s);
    sumEl.style.overflow = 'hidden';
    sumEl.style.textOverflow = 'ellipsis';
    sumEl.style.whiteSpace = 'nowrap';
    const stateEl = document.createElement('span');
    mid.appendChild(sumEl);
    mid.appendChild(stateEl);

    const toggleWrap = document.createElement('div');
    toggleWrap.className = 'toggle se-toggle';
    const sw = document.createElement('div');
    sw.className = 'toggle-switch' + (s.enabled ? ' active' : '');
    const thumb = document.createElement('div');
    thumb.className = 'toggle-thumb';
    sw.appendChild(thumb);
    toggleWrap.appendChild(sw);
    toggleWrap.onclick = () => mcpToggleServer(s.name, div);

    div.appendChild(nameEl);
    div.appendChild(mid);
    div.appendChild(toggleWrap);
    div._stateEl = stateEl;   // element refs for in-place updates (not serialized anywhere)
    div._sw = sw;
    mcpApplyRowState(div, s);
    return div;
}

function mcpRenderList() {
    const list = document.getElementById('mcpServerList');
    if (!list) return;
    list.innerHTML = '';
    if (!mcpServers.length) {
        const empty = document.createElement('div');
        empty.className = 'se-row';
        empty.style.color = 'var(--text-secondary)';
        empty.textContent = 'No MCP servers configured yet - add one below (e.g. the Blender bridge).';
        list.appendChild(empty);
    } else {
        for (const s of mcpServers) list.appendChild(mcpBuildRow(s));
    }
    let msg = mcpServers.length + ' server(s) in config/mcp_servers.json - click a row to edit, toggle to enable/disable.';
    if (mcpProblems.length) msg += ' Problems: ' + mcpProblems.join('; ');
    mcpStatus(msg, mcpProblems.length > 0);
}

/** Load the config via API and render all rows into #mcpServerList. */
async function loadMcpServers() {
    const list = document.getElementById('mcpServerList');
    if (!list) return;
    mcpStatus('Loading MCP servers...');
    try {
        // No manual auth headers needed: the global fetch override in api-key.js injects
        // X-API-Key / X-User-Email for every /api/ request automatically.
        const r = await fetch('/api/mcp/servers');
        const data = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error((data && data.detail) || ('HTTP ' + r.status));
        mcpServers = Array.isArray(data.servers) ? data.servers : [];
        mcpProblems = Array.isArray(data.problems) ? data.problems : [];
        mcpRenderList();
    } catch (e) {
        list.innerHTML = '';
        const err = document.createElement('div');
        err.className = 'se-row';
        err.style.color = 'var(--danger)';
        err.textContent = 'Could not load MCP servers: ' + e.message;
        list.appendChild(err);
        mcpStatus('Load failed - check the CLIENT backend is running.', true);
    }
}

/** Flip one server's enabled flag and save the whole list (reverts on error). */
async function mcpToggleServer(name, rowEl) {
    if (mcpSaving) return; // one save at a time - keeps UI and file in sync
    const s = mcpServers.find(x => x.name === name);
    if (!s) return;

    mcpSaving = true;
    s.enabled = !s.enabled;       // optimistic update
    mcpApplyRowState(rowEl, s);
    mcpStatus('Saving...');
    try {
        const r = await fetch('/api/mcp/servers', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ servers: mcpServers })
        });
        const data = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error((data && data.detail) || ('HTTP ' + r.status));
        mcpStatus(s.enabled ? 'Enabled - starting now...' : 'Disabled.');
        // The re-wire starts the process in the background; give it a moment, then pick up
        // the live connected/error state (a first stdio start may download its package).
        setTimeout(loadMcpServers, 1500);
    } catch (e) {
        s.enabled = !s.enabled;   // revert on failure
        mcpApplyRowState(rowEl, s);
        mcpStatus('Save failed: ' + e.message, true);
        loadMcpServers();         // restore the last good state from disk
    } finally {
        mcpSaving = false;
    }
}

function mcpTransportIsHttp() {
    const sel = document.getElementById('mcpTransportSelect');
    return !!sel && sel.value === 'http';
}

/** Show/hide the stdio (command+args) vs http (url) fields + update the hint. */
function mcpToggleTransportFields() {
    const http = mcpTransportIsHttp();
    const stdio = document.getElementById('mcpStdioFields');
    const url = document.getElementById('mcpUrlInput');
    if (stdio) stdio.style.display = http ? 'none' : 'grid';
    if (url) url.style.display = http ? 'block' : 'none';
    const hint = document.getElementById('mcpFormHint');
    if (hint) hint.innerHTML = http
        ? 'http transport: enter the remote MCP endpoint URL.'
        : 'stdio transport: the program to start on this machine + its arguments. Example - Blender bridge: command <code style="background:var(--bg-tertiary);padding:2px 6px;border-radius:3px;font-family:monospace;">uvx</code>, args one per line: <code style="background:var(--bg-tertiary);padding:2px 6px;border-radius:3px;font-family:monospace;">--from</code> / <code style="background:var(--bg-tertiary);padding:2px 6px;border-radius:3px;font-family:monospace;">blender-mcp</code> / <code style="background:var(--bg-tertiary);padding:2px 6px;border-radius:3px;font-family:monospace;">blender-mcp</code>. The first start may download the package (needs internet).';
}

/** Blank the form back to "Add MCP Server" defaults. */
function mcpResetForm() {
    mcpEditingName = null;
    const setVal = (id, v) => { const el = document.getElementById(id); if (el) el.value = v; };
    setVal('mcpNameInput', '');
    setVal('mcpCommandInput', '');
    setVal('mcpArgsInput', '');
    setVal('mcpUrlInput', '');
    setVal('mcpTimeoutInput', '30');
    setVal('mcpRolesInput', '*');
    const sel = document.getElementById('mcpTransportSelect');
    if (sel) sel.value = 'stdio';
    mcpToggleTransportFields();
    const title = document.getElementById('mcpFormTitle');
    if (title) title.textContent = 'Add MCP Server';
    const saveBtn = document.getElementById('mcpSaveBtn');
    if (saveBtn) saveBtn.textContent = 'Add Server';
    const delBtn = document.getElementById('mcpDeleteBtn');
    if (delBtn) delBtn.style.display = 'none';
    const cancelBtn = document.getElementById('mcpCancelEditBtn');
    if (cancelBtn) cancelBtn.style.display = 'none';
}

/** Fill the form from one configured server (row click). */
function mcpStartEdit(name) {
    const s = mcpServers.find(x => x.name === name);
    if (!s) return;
    mcpEditingName = name;
    const setVal = (id, v) => { const el = document.getElementById(id); if (el) el.value = v; };
    setVal('mcpNameInput', s.name);
    const sel = document.getElementById('mcpTransportSelect');
    if (sel) sel.value = s.transport || 'stdio';
    mcpToggleTransportFields();
    setVal('mcpCommandInput', s.command || '');
    setVal('mcpArgsInput', Array.isArray(s.args) ? s.args.join('\n') : '');
    setVal('mcpUrlInput', s.url || '');
    setVal('mcpTimeoutInput', String(s.timeout_sec || 30));
    setVal('mcpRolesInput', (s.roles && s.roles.length) ? s.roles.join(', ') : '*');
    const title = document.getElementById('mcpFormTitle');
    if (title) title.textContent = 'Edit MCP Server - ' + name;
    const saveBtn = document.getElementById('mcpSaveBtn');
    if (saveBtn) saveBtn.textContent = 'Update Server';
    const delBtn = document.getElementById('mcpDeleteBtn');
    if (delBtn) delBtn.style.display = '';
    const cancelBtn = document.getElementById('mcpCancelEditBtn');
    if (cancelBtn) cancelBtn.style.display = '';
}

/** Add/update/remove the form's server and save the whole list. remove=true deletes it. */
async function mcpSaveServer(remove) {
    if (mcpSaving) return;
    const nameInput = document.getElementById('mcpNameInput');
    if (!nameInput) return;
    const name = (nameInput.value || '').trim();
    if (!MCP_NAME_RE.test(name)) {
        mcpStatus("Invalid name - use letters, digits, '_', '.' or '-' (max 40 chars).", true);
        return;
    }

    const transport = document.getElementById('mcpTransportSelect').value || 'stdio';
    let timeout = parseInt(document.getElementById('mcpTimeoutInput').value, 10);
    if (!Number.isFinite(timeout)) timeout = 30;
    timeout = Math.min(300, Math.max(1, timeout));

    const rolesRaw = (document.getElementById('mcpRolesInput').value || '')
        .split(',').map(x => x.trim()).filter(Boolean);
    const roles = rolesRaw.length ? rolesRaw : ['*'];

    if (remove) {
        mcpServers = mcpServers.filter(s => s.name !== name);
    } else {
        const existing = mcpEditingName ? mcpServers.find(s => s.name === mcpEditingName) : null;
        // Rename support: drop the old row when the name changed while editing.
        if (existing && existing.name !== name) mcpServers = mcpServers.filter(s => s !== existing);

        const base = existing || { env: {}, cwd: '' };   // keep advanced fields untouched
        const entry = {
            name: name,
            enabled: existing ? existing.enabled : true,  // adding means you want it on; first use still asks consent
            transport: transport,
            command: '', args: [], url: '',
            env: base.env || {}, cwd: base.cwd || ''
        };
        if (transport === 'stdio') {
            entry.command = (document.getElementById('mcpCommandInput').value || '').trim();
            entry.args = (document.getElementById('mcpArgsInput').value || '')
                .split('\n').map(x => x.trim()).filter(Boolean);
        } else {
            entry.url = (document.getElementById('mcpUrlInput').value || '').trim();
        }
        entry.roles = roles;
        entry.timeout_sec = timeout;

        if (!existing) mcpServers.push(entry);
        else Object.assign(existing, entry);
    }

    mcpSaving = true;
    mcpStatus('Saving...');
    try {
        const r = await fetch('/api/mcp/servers', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ servers: mcpServers })
        });
        const data = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error((data && data.detail) || ('HTTP ' + r.status));
        mcpStatus('Saved - applied immediately, no restart needed.');
        mcpResetForm();
        setTimeout(loadMcpServers, 1200);   // let the re-wire start before reading live state
    } catch (e) {
        mcpStatus('Save failed: ' + e.message, true);
        loadMcpServers();                   // restore the last good state from disk
    } finally {
        mcpSaving = false;
    }
}

window.loadMcpServers = loadMcpServers;   // called from openSettings() in ui-controls.js
window.mcpToggleServer = mcpToggleServer;
window.mcpStartEdit = mcpStartEdit;
window.mcpResetForm = mcpResetForm;
window.mcpSaveServer = mcpSaveServer;
window.mcpToggleTransportFields = mcpToggleTransportFields;
