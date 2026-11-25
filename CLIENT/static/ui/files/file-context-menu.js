/*
 * file-context-menu.js - right-click menu for Files panel rows.
 * Part of the CLIENT/static/ui/files/ module group; load AFTER files-state.js,
 * tree-view.js and working-root-chip.js, BEFORE files-panel.js.
 *
 * Every row of the Files panel (top level, inside an expanded folder, file or folder)
 * answers a right click with the same menu the workspace rows have:
 *   Rename           - the row's name becomes an input; Enter commits (file-actions.js)
 *   Move to folder... - move the row (a folder with its whole subtree) into a folder
 *   Copy name        - the row's own name, no path
 *   Copy full path   - the REAL path: the workspace folder + the row's relative path
 *   Show in explorer - opens the OS file manager with the item selected
 *
 * The listener is DELEGATED on #filesList on purpose: renderTreeItems() /
 * renderChildrenItems() replace the panel's innerHTML on every load and every folder
 * expand, so a per-row listener would be lost each time while the container survives.
 */

const FILE_CONTEXT_MENU_CLASS = 'file-context-menu';
const FILE_CONTEXT_MENU_REVEAL_ENDPOINT = '/api/reveal';

let fileContextMenuEl = null;

// ===== TARGET RESOLUTION =====

/** The .tree-item row a context click landed on, or null (empty folder / breadcrumbs). */
function fileContextMenuRowFor(event) {
    const list = document.getElementById('filesList');
    const target = event.target;
    if (!list || !target || typeof target.closest !== 'function') return null;
    const row = target.closest('.tree-item');
    if (!row || !row.dataset.path || !list.contains(row)) return null;
    return row;
}

function fileContextMenuIsFolder(row) {
    return row.classList.contains('tree-folder');
}

// ===== FULL PATH =====

/** Workspace folder of the OPEN workspace; asks the server when the chip has not loaded it. */
async function fileContextMenuWorkingRoot() {
    if (typeof currentWorkingRoot !== 'undefined' && currentWorkingRoot) return String(currentWorkingRoot);
    try {
        const response = await fetch('/api/working_root');
        if (!response.ok) return '';
        const data = await response.json();
        return data && data.working_root ? String(data.working_root) : '';
    } catch (error) {
        return '';
    }
}

/**
 * Join the workspace folder and a panel-relative path into a native path.
 * The separator is read off the folder itself, so a Windows root yields 'dir\file'
 * and a POSIX root yields 'dir/file' without asking the server which OS it runs on.
 */
function fileContextMenuJoinPath(root, relativePath) {
    const base = String(root || '').replace(/[/\\]+$/, '');
    if (!base) return String(relativePath || '');
    const separator = base.includes('\\') && !base.includes('/') ? '\\' : (base.includes('/') ? '/' : '\\');
    return base + separator + String(relativePath || '').split('/').join(separator);
}

async function fileContextMenuCopyFullPath(relativePath) {
    const root = await fileContextMenuWorkingRoot();
    if (!root) {
        showNotification('No working folder for this workspace - cannot build the full path', 'warning');
        return;
    }
    try {
        await copyTextToClipboard(fileContextMenuJoinPath(root, relativePath));
        showNotification('Full path copied', 'success');
    } catch (error) {
        showNotification('Failed to copy the full path', 'error');
    }
}

async function fileContextMenuCopyName(relativePath) {
    const name = String(relativePath || '').split('/').pop();
    if (!name) return;
    try {
        await copyTextToClipboard(name);
        showNotification('Name copied', 'success');
    } catch (error) {
        showNotification('Failed to copy the name', 'error');
    }
}

// ===== SHOW IN EXPLORER =====

/**
 * Ask the backend to reveal the row in the OS file manager.
 * The workspace id goes as a query param: the files router dependency authorizes the
 * request from it (the global fetch override also sends the X-Conversation-Id header).
 */
async function fileContextMenuReveal(relativePath) {
    const conversation = typeof currentConvId === 'function' ? (currentConvId() || '') : '';
    const query = new URLSearchParams({ path: relativePath });
    if (conversation) query.set('conversation_id', conversation);
    try {
        const response = await fetch(`${FILE_CONTEXT_MENU_REVEAL_ENDPOINT}?${query}`, { method: 'POST' });
        if (!response.ok) {
            let detail = '';
            try {
                detail = (await response.json()).detail || '';
            } catch (error) {
                detail = '';
            }
            throw new Error(detail || `HTTP ${response.status}`);
        }
        showNotification('Opened in the file manager', 'success');
    } catch (error) {
        showNotification('Show in explorer failed: ' + (error.message || error), 'error');
    }
}

// ===== RENAME / MOVE =====

/** Rename in place: the row's name becomes an input (file-actions.js owns the call). */
function fileContextMenuRename(relativePath) {
    if (typeof inlineRenameFileRow !== 'function') return;
    inlineRenameFileRow(relativePath);
}

/** Move into a typed folder (file-actions.js owns the call and the refresh). */
function fileContextMenuMove(relativePath) {
    if (typeof moveFileItemToTypedFolder !== 'function') return;
    void moveFileItemToTypedFolder(relativePath);
}

// ===== MENU =====

function closeFileContextMenu() {
    if (!fileContextMenuEl) return;
    fileContextMenuEl.remove();
    fileContextMenuEl = null;
}

function showFileContextMenu(event) {
    const row = fileContextMenuRowFor(event);
    if (!row) return;  // empty-folder placeholder and breadcrumbs keep the native menu
    event.preventDefault();
    closeFileContextMenu();

    const relativePath = row.dataset.path;
    const menu = document.createElement('div');
    menu.className = FILE_CONTEXT_MENU_CLASS;
    menu.setAttribute('role', 'menu');
    menu.setAttribute('aria-label', `${fileContextMenuIsFolder(row) ? 'Folder' : 'File'} actions`);

    const actions = [
        ['Rename', () => fileContextMenuRename(relativePath)],
        ['Move to folder...', () => fileContextMenuMove(relativePath)],
        ['Copy name', () => fileContextMenuCopyName(relativePath)],
        ['Copy full path', () => fileContextMenuCopyFullPath(relativePath)],
        ['Show in explorer', () => fileContextMenuReveal(relativePath)],
    ];
    for (const [label, action] of actions) {
        const button = document.createElement('button');
        button.type = 'button';
        button.textContent = label;
        button.setAttribute('role', 'menuitem');
        button.addEventListener('click', (clickEvent) => {
            clickEvent.preventDefault();
            clickEvent.stopPropagation();
            closeFileContextMenu();
            action();
        });
        menu.append(button);
    }

    document.body.append(menu);
    // Measured after insertion: the menu is position:fixed, so offsetWidth/Height are real
    // and both edges can be clamped into the viewport (a right click near the right edge
    // must open leftwards instead of off-screen).
    menu.style.left = `${Math.max(0, Math.min(event.clientX, window.innerWidth - menu.offsetWidth))}px`;
    menu.style.top = `${Math.max(0, Math.min(event.clientY, window.innerHeight - menu.offsetHeight))}px`;

    fileContextMenuEl = menu;
    // preventScroll: focusing must not scroll anything, or the scroll-dismiss listener
    // above would close the menu the moment it opens.
    menu.querySelector('button')?.focus({ preventScroll: true });
}

// ===== INIT =====

(function initFileContextMenu() {
    function bind() {
        const list = document.getElementById('filesList');
        if (!list) return;
        list.addEventListener('contextmenu', showFileContextMenu);
        // The menu floats over the page, so it is dismissed by anything that would move the
        // row it points at: a click elsewhere, Escape, a scroll (capture: the files list
        // scrolls itself) or a resize.
        document.addEventListener('click', (event) => {
            if (fileContextMenuEl && !fileContextMenuEl.contains(event.target)) closeFileContextMenu();
        });
        document.addEventListener('keydown', (event) => {
            if (event.key === 'Escape') closeFileContextMenu();
        });
        document.addEventListener('scroll', closeFileContextMenu, true);
        window.addEventListener('resize', closeFileContextMenu);
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bind);
    else bind();
})();
