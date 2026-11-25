/*
 * file-actions.js - Move / Rename for the Files panel.
 * Part of the CLIENT/static/ui/files/ module group; loaded AFTER tree-view.js and
 * BEFORE file-context-menu.js / drop-to-folder.js (both call into this file).
 *
 * MOVE   drag a row onto a folder row (or onto the empty area of the list = workspace
 *        root), or "Move to folder..." in the row menu. Folders move with their whole
 *        subtree - the work is done by POST /api/move (shutil.move), the panel only
 *        sends the two relative paths and re-reads the tree afterwards.
 * RENAME "Rename" in the row menu turns the row's name into an input; Enter or blur
 *        commits POST /api/rename, Escape cancels. A double click on the name does the
 *        same thing.
 *
 * Both endpoints refuse traversal, overwrites and a folder moved into itself, so the
 * panel never has to guess - a refusal is shown as the server's own message.
 */

// Internal drag marker: distinguishes "a row of this panel" from a file dragged in from
// the desktop (that one is an UPLOAD, see drop-to-folder.js / file-upload.js).
const FILE_ACTION_DRAG_MARKER = 'coolems-file-drag';

// Row class while it is being dragged for a move (dimmed, see 02-tree-preview-editor.css).
const FILE_ACTION_SOURCE_CLASS = 'file-move-source';

let fileActionBusy = false;

// ===== HELPERS =====

function fileActionParent(path) {
    const value = String(path || '');
    const cut = value.lastIndexOf('/');
    return cut === -1 ? '' : value.slice(0, cut);
}

function fileActionName(path) {
    return String(path || '').split('/').pop();
}

function fileActionJoin(folder, name) {
    return folder ? folder + '/' + name : name;
}

async function fileActionPost(endpoint, body) {
    const conversation = typeof currentConvId === 'function' ? currentConvId() : '';
    const query = conversation ? '?conversation_id=' + encodeURIComponent(conversation) : '';
    const response = await fetch(endpoint + query, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    if (!response.ok) {
        let detail = '';
        try {
            detail = (await response.json()).detail || '';
        } catch (error) {
            detail = '';
        }
        throw new Error(detail || `HTTP ${response.status}`);
    }
    return response.json();
}

/**
 * Re-read the panel so a change on disk is visible.
 *
 * loadTree() rehydrates every expanded branch by itself, so the folder the user is
 * looking at (and every folder they have opened inside it) comes back complete.
 */
function refreshFilePanelAfterFileChange() {
    if (typeof loadTree !== 'function') return Promise.resolve(false);
    return loadTree(currentTreePath);
}

/**
 * Keep the panel's own bookkeeping pointing at the item after it moved.
 *
 * expandedFolders / selectedItems are keyed by panel-relative path, so a move or a rename
 * leaves them on the OLD path: loadTree() only re-opens the branches it still finds in
 * expandedFolders, so the folder the user had open came back collapsed and a ticked file
 * lost its tick. Remapping the prefix keeps both.
 */
function fileActionRemapTreeState(oldPath, newPath) {
    if (!oldPath || !newPath || oldPath === newPath) return;
    const remap = (value) => {
        const text = String(value);
        if (text === oldPath) return newPath;
        if (text.startsWith(oldPath + '/')) return newPath + text.slice(oldPath.length);
        return text;
    };
    const retarget = (set) => {
        if (!set || typeof set.forEach !== 'function') return;
        const next = [];
        set.forEach((value) => next.push(remap(value)));
        set.clear();
        next.forEach((value) => set.add(value));
    };
    if (typeof expandedFolders !== 'undefined') retarget(expandedFolders);
    if (typeof selectedItems !== 'undefined') retarget(selectedItems);
}

// ===== MOVE =====

/** True when the destination folder is the source itself or lives inside it. */
function fileActionIsInside(sourcePath, folderPath) {
    const source = String(sourcePath || '');
    const folder = String(folderPath || '');
    return folder === source || folder.startsWith(source + '/');
}

/**
 * Move a file or folder of the panel into another folder of the same workspace.
 *
 * @param {string} sourcePath - panel-relative path of the row being moved
 * @param {string} destinationPath - panel-relative folder ('' = workspace root)
 */
async function moveFileItem(sourcePath, destinationPath) {
    const source = String(sourcePath || '');
    const destination = String(destinationPath || '');
    if (!source) {
        showNotification('Could not read the dragged item - try again', 'error');
        return false;
    }
    if (fileActionIsInside(source, destination)) {
        showNotification('A folder cannot be moved into itself', 'warning');
        return false;
    }
    if (fileActionParent(source) === destination) {
        showNotification(`"${fileActionName(source)}" is already in that folder`, 'info');
        return false;
    }
    if (fileActionBusy) {
        showNotification('Another file operation is still running', 'info');
        return false;
    }
    fileActionBusy = true;
    try {
        const result = await fileActionPost('/api/move', { source, destination_path: destination });
        showNotification(`Moved "${fileActionName(source)}" to ${destination || 'the workspace root'}`, 'success');
        fileActionRemapTreeState(source, result && result.path);
        await refreshFilePanelAfterFileChange();
        // With "File tools sync" on, keep the moved row marked at its new place.
        if (typeof fileToolsSyncEnabled !== 'undefined' && fileToolsSyncEnabled
            && typeof jumpFilePanelToFile === 'function' && result && result.path) {
            const request = { isCurrent: () => true };
            void jumpFilePanelToFile(result.path, request);
        }
        return true;
    } catch (error) {
        showNotification('Move failed: ' + (error.message || error), 'error');
        return false;
    } finally {
        fileActionBusy = false;
    }
}

/** "Move to folder..." - the folder is typed as a panel-relative path. */
async function moveFileItemToTypedFolder(sourcePath) {
    const name = fileActionName(sourcePath);
    const suggestion = fileActionParent(sourcePath);
    const answer = window.prompt(
        `Move "${name}" into which folder?\n(relative to the workspace root, empty = root)`,
        suggestion
    );
    if (answer === null) return false;
    return moveFileItem(sourcePath, answer.trim().replace(/^[/\\]+|[/\\]+$/g, ''));
}

// ===== RENAME =====

/**
 * Rename a file or folder in place.
 *
 * @param {string} path - panel-relative path of the row
 * @param {string} newName - the new bare name (no separators)
 */
async function renameFileItem(path, newName) {
    const name = String(newName || '').trim();
    const oldName = fileActionName(path);
    if (!name) {
        showNotification('A name is required', 'warning');
        return false;
    }
    if (name === oldName) return false;
    if (fileActionBusy) {
        showNotification('Another file operation is still running', 'info');
        return false;
    }
    fileActionBusy = true;
    try {
        const result = await fileActionPost('/api/rename', { path, new_name: name });
        showNotification(`Renamed "${oldName}" to "${name}"`, 'success');
        fileActionRemapTreeState(path, fileActionJoin(fileActionParent(path), name));
        await refreshFilePanelAfterFileChange();
        if (typeof fileToolsSyncEnabled !== 'undefined' && fileToolsSyncEnabled
            && typeof jumpFilePanelToFile === 'function' && result && result.path) {
            void jumpFilePanelToFile(result.path, { isCurrent: () => true });
        }
        return true;
    } catch (error) {
        showNotification('Rename failed: ' + (error.message || error), 'error');
        return false;
    } finally {
        fileActionBusy = false;
    }
}

/**
 * Turn a row's name into an input (the workspace sidebar's rename, for the file tree).
 * Enter or blur commits, Escape restores the old name. The row keeps its layout: the
 * input takes the name span's place inside .tree-item-main.
 */
function inlineRenameFileRow(path) {
    const row = typeof fileTreeRow === 'function' ? fileTreeRow(path) : null;
    if (!row) return false;
    const nameEl = row.querySelector(':scope > .tree-item-main .tree-name');
    if (!nameEl || row.querySelector('.file-rename-input')) return false;

    const oldName = fileActionName(path);
    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'file-rename-input';
    input.value = oldName;
    input.spellcheck = false;
    nameEl.replaceWith(input);
    input.focus();
    // Select the stem, not the extension: renaming 'code.py' usually keeps '.py'.
    const dot = oldName.lastIndexOf('.');
    input.setSelectionRange(0, dot > 0 ? dot : oldName.length);

    let done = false;
    const finish = async (commit) => {
        if (done) return;
        done = true;
        const value = input.value.trim();
        input.replaceWith(nameEl);
        if (!commit || !value || value === oldName) return;
        await renameFileItem(path, value);
    };
    input.addEventListener('click', (event) => event.stopPropagation());
    input.addEventListener('dblclick', (event) => event.stopPropagation());
    input.addEventListener('keydown', (event) => {
        event.stopPropagation();
        if (event.key === 'Enter') {
            event.preventDefault();
            void finish(true);
        } else if (event.key === 'Escape') {
            event.preventDefault();
            void finish(false);
        }
    });
    input.addEventListener('blur', () => { void finish(true); });
    return true;
}

// ===== DRAG PAYLOAD =====

/** The panel-relative path carried by an internal drag, or '' for anything else. */
function fileActionDraggedPath(dataTransfer) {
    if (!dataTransfer) return '';
    let marker = '';
    try {
        marker = dataTransfer.getData(FILE_ACTION_DRAG_MARKER);
    } catch (error) {
        return '';
    }
    if (marker !== 'true') return '';
    try {
        const payload = JSON.parse(dataTransfer.getData('text/plain') || '{}');
        return typeof payload.filePath === 'string' ? payload.filePath : '';
    } catch (error) {
        return '';
    }
}

/** True when the drag currently in flight started on a row of this panel. */
function fileActionIsInternalDrag(dataTransfer) {
    if (!dataTransfer || !dataTransfer.types) return false;
    const types = Array.from(dataTransfer.types);
    return types.includes(FILE_ACTION_DRAG_MARKER);
}

/** Dim the row that is being dragged (cleared on dragend). */
function fileActionMarkDraggedRow(path) {
    document.querySelectorAll('.' + FILE_ACTION_SOURCE_CLASS).forEach(function (row) {
        row.classList.remove(FILE_ACTION_SOURCE_CLASS);
    });
    if (!path || typeof fileTreeRow !== 'function') return;
    const row = fileTreeRow(path);
    if (row) row.classList.add(FILE_ACTION_SOURCE_CLASS);
}

// ===== INIT =====

(function initFileActions() {
    function bind() {
        const list = document.getElementById('filesList');
        if (!list) return;
        // Double click on a row's name = rename (same gesture as the sidebar).
        list.addEventListener('dblclick', (event) => {
            const row = event.target && event.target.closest ? event.target.closest('.tree-item') : null;
            if (!row || !row.dataset.path) return;
            if (event.target.closest('.tree-item-actions')) return;
            event.preventDefault();
            event.stopPropagation();
            inlineRenameFileRow(row.dataset.path);
        });
        // The dragged row is dimmed for the whole gesture (dragstart fires on the row).
        document.addEventListener('dragstart', (event) => {
            if (!fileActionIsInternalDrag(event.dataTransfer)) return;
            fileActionMarkDraggedRow(fileActionDraggedPath(event.dataTransfer));
        });
        document.addEventListener('dragend', () => fileActionMarkDraggedRow(''));
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bind);
    else bind();
})();
