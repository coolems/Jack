/*
 * file-tools-sync.js - "File tools sync" toggle for the Files panel.
 * Part of the CLIENT/static/ui/files/ module group; loaded BEFORE files-panel.js and
 * AFTER files-state.js / tree-view.js.
 *
 * ON  -> a file tool running in the VISIBLE loop (read_file / write_file / edit_file /
 *        insert_lines / move_file / rename_file) REVEALS its file in the Files panel:
 *        the folders on the way are expanded and the file row is marked. The panel's
 *        own folder (currentTreePath) is NEVER changed by the jump - the list the user
 *        is looking at stays the list the user is looking at.
 * OFF -> the panel never moves on its own.
 *
 * INDEPENDENT of the toggle: a tool that WRITES / DELETES / MOVES / RENAMES a file makes
 * the panel re-read the folder it shows, so the change is visible (a new file appears, a
 * deleted one is gone, a moved one sits in its new folder).
 *
 * The `sync_file` path arrives on the tool frames from logic/tool_executor.py and comes
 * from app/turn_log.py::tool_file() - workspace-relative, same base as /api/tree.
 */

// localStorage key holding the toggle across reloads.
const FILE_TOOLS_SYNC_STORAGE_KEY = 'fileToolsSync';

const FILE_TOOLS_SYNC_START_TOOLS = ['read_file', 'write_file', 'edit_file', 'insert_lines'];

// Tools that change the tree on disk: the panel must re-read its folder afterwards.
// delete_file only refreshes - the file it names is gone, so there is nothing to select.
const FILE_TOOLS_TREE_CHANGING_TOOLS = [
    'write_file', 'edit_file', 'insert_lines', 'delete_file', 'move_file', 'rename_file',
];

// CSS class marking the row the panel jumped to (see 02-tree-preview-editor.css).
const FILE_TOOLS_SYNC_ROW_CLASS = 'tool-sync-target';

let fileToolsSyncEnabled = false;
let fileToolsSyncRevision = 0;

// Serialized re-reads: two tools ending in the same step must not have their /api/tree
// responses render over each other (the older answer used to win and show a stale list).
let fileToolsSyncRefreshChain = Promise.resolve();

// ===== TOGGLE =====

function toggleFileToolsSync() {
    fileToolsSyncEnabled = !fileToolsSyncEnabled;
    fileToolsSyncRevision++;
    try {
        localStorage.setItem(FILE_TOOLS_SYNC_STORAGE_KEY, fileToolsSyncEnabled ? '1' : '0');
    } catch (e) {
        // Private mode / storage full: the toggle still works for this session.
    }
    paintFileToolsSyncBtn();
    if (!fileToolsSyncEnabled) clearFileToolsSyncHighlight();
    showNotification(
        fileToolsSyncEnabled
            ? 'File tools sync ON - the panel reveals the file the running tool works on'
            : 'File tools sync OFF',
        'info'
    );
}

function paintFileToolsSyncBtn() {
    const btn = document.getElementById('fileToolsSyncBtn');
    if (!btn) return;
    btn.classList.toggle('active', fileToolsSyncEnabled);
    btn.setAttribute('aria-pressed', fileToolsSyncEnabled ? 'true' : 'false');
    btn.title = fileToolsSyncEnabled
        ? 'File tools sync: ON - reveal the file the running tool works on'
        : 'File tools sync: OFF - click to reveal the file the running tool works on';
}

// ===== SELECTION JUMP =====

function clearFileToolsSyncHighlight() {
    document.querySelectorAll('.' + FILE_TOOLS_SYNC_ROW_CLASS).forEach(function (row) {
        row.classList.remove(FILE_TOOLS_SYNC_ROW_CLASS);
    });
}

function normalizeFileToolPath(path) {
    const value = String(path || '').replace(/\\/g, '/');
    if (/^(?:\/|[a-zA-Z]:)/.test(value)) return '';
    const segments = value.split('/').filter(part => part && part !== '.');
    return segments.includes('..') ? '' : segments.join('/');
}

async function jumpFilePanelToFile(path, request) {
    const file = normalizeFileToolPath(path);
    const base = currentTreePath;
    if (!file || (base && !file.startsWith(base + '/'))) return;
    const revision = filesTreeRevision;
    const isCurrent = () => request.isCurrent() && revision === filesTreeRevision;
    if (!isCurrent()) return;
    let row = fileTreeRow(file, '.tree-file');
    if (!row) {
        const folders = (base ? file.slice(base.length + 1) : file).split('/').slice(0, -1);
        if (!folders.length) return;
        // Verify the target before expanding anything (a missing file must not move the panel).
        const parent = file.slice(0, file.lastIndexOf('/'));
        try {
            const items = await fetchFileTreeItems(parent, request.conversation);
            const name = file.slice(parent.length + 1);
            if (!isCurrent() || !items.some(item => item.name === name && item.type === 'file')) return;
        } catch (_) {
            return;
        }
        let folder = base;
        for (const name of folders) {
            folder = folder ? folder + '/' + name : name;
            if (!isCurrent() || !await expandFileTreeFolder(folder, isCurrent)) return;
        }
        row = fileTreeRow(file, '.tree-file');
    }
    if (!row || !isCurrent()) return;
    clearFileToolsSyncHighlight();
    row.classList.add(FILE_TOOLS_SYNC_ROW_CLASS);
    row.scrollIntoView({ block: 'nearest', inline: 'nearest' });
}

// Tool refresh applies independently of sync. Capture the workspace, but read the
// folder at refresh time so a pending refresh never pulls the user back to an old folder.
function refreshFilePanelAfterToolChange(conversation) {
    fileToolsSyncRefreshChain = fileToolsSyncRefreshChain.then(() => {
        if (ownsFileToolFrame(conversation)) return loadTree(currentTreePath);
        return false;
    });
    return fileToolsSyncRefreshChain;
}

function ownsFileToolFrame(conversation) {
    return COOLEMS.filesPanelOpen && !!conversation && conversation === currentConvId();
}

function fileToolJumpRequest(conversation) {
    if (!fileToolsSyncEnabled) return null;
    const revision = ++fileToolsSyncRevision;
    return { conversation, isCurrent: () => fileToolsSyncEnabled
        && revision === fileToolsSyncRevision && ownsFileToolFrame(conversation) };
}

function fileToolsSyncOnToolStart(data, convId, isReplay) {
    if (!data || isReplay || !ownsFileToolFrame(convId) || !FILE_TOOLS_SYNC_START_TOOLS.includes(data.tool)) return;
    const request = fileToolJumpRequest(convId);
    if (request) void jumpFilePanelToFile(data.sync_file, request);
}

function fileToolsSyncOnToolEnd(data, convId, isReplay) {
    if (!data || isReplay || !ownsFileToolFrame(convId) || !FILE_TOOLS_TREE_CHANGING_TOOLS.includes(data.tool)) return;
    const request = data.tool !== 'delete_file' && data.success !== false ? fileToolJumpRequest(convId) : null;
    void refreshFilePanelAfterToolChange(convId).then(refreshed => {
        if (refreshed && request?.isCurrent()) return jumpFilePanelToFile(data.sync_file, request);
    });
}

// ===== INIT =====

(function initFileToolsSync() {
    try {
        fileToolsSyncEnabled = localStorage.getItem(FILE_TOOLS_SYNC_STORAGE_KEY) === '1';
    } catch (e) {
        fileToolsSyncEnabled = false;
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', paintFileToolsSyncBtn);
    } else {
        paintFileToolsSyncBtn();
    }
})();
