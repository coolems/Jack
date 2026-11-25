/*
     * drop-to-folder.js - split from file-handler.js on 2026-09-18 (verbatim cut/paste).
     * Part of the CLIENT/static/ui/files/ module group. Load order matters:
     * files-state.js must load first; see index.html script tags.
     *
     * Two different drops land on the Files panel:
     *   * a ROW of this panel dragged onto a folder row (or onto the empty area of the
     *     list, which means the workspace root) -> MOVE that file/folder there
     *     (file-actions.js -> POST /api/move; a folder moves with its whole subtree).
     *   * files dragged in from the desktop -> upload/copy into the folder (unchanged).
     */

    
// External desktop files dropped onto tree folders + internal row moves.



// ===== FILES LIST DRAG AND DROP (internal move + external files into folders) =====



(function initFilesListDrop() {

    const filesList = document.getElementById('filesList');

    if (!filesList) return;



    let currentDragTargetFolder = null;

    let dragOverFolderPath = null;

    // Internal drag in flight: the panel-relative path of the row being moved ('' = none).
    let draggedRowPath = '';



    function findFolderAncestor(el) {

        while (el && el !== filesList) {

            if (el.classList && el.classList.contains('tree-folder')) {

                return el;

            }

            el = el.parentElement;

        }

        return null;

    }



    // A row can only be dropped INTO a folder: the folder row itself, or the empty area
    // of the list (= workspace root). Dropping on a file row means "the folder it is in".
    function dropFolderPathFor(el) {

        const folderEl = findFolderAncestor(el);

        if (folderEl) return folderEl.getAttribute('data-path') || '';

        const fileRow = el && el.closest ? el.closest('.tree-file') : null;

        if (fileRow) return typeof fileActionParent === 'function' ? fileActionParent(fileRow.dataset.path) : '';

        return '';

    }



    // Highlight the folder an internal row drag would move into. '' = the workspace root:
    // no row to paint there, so the list itself gets the outline (see the CSS rule).
    function paintMoveTarget(folderPath) {

        if (currentDragTargetFolder && dragOverFolderPath !== folderPath) {

            currentDragTargetFolder.classList.remove('drag-target');

        }

        filesList.classList.toggle('root-move-target', !folderPath);

        if (folderPath) {

            const folderEl = filesList.querySelector('.tree-folder[data-path="' + CSS.escape(folderPath) + '"]');

            if (folderEl) {

                currentDragTargetFolder = folderEl;

                folderEl.classList.add('drag-target');

                dragOverFolderPath = folderPath;

                return;

            }

        }

        currentDragTargetFolder = null;

        dragOverFolderPath = null;

    }



    function clearMoveTarget() {

        if (currentDragTargetFolder) {

            currentDragTargetFolder.classList.remove('drag-target');

        }

        filesList.classList.remove('root-move-target');

        currentDragTargetFolder = null;

        dragOverFolderPath = null;

    }



    filesList.addEventListener('dragenter', function(e) {

        e.preventDefault();

        e.stopPropagation();



        if (e.dataTransfer.types && e.dataTransfer.types.includes('Files')) {

            if (e.relatedTarget && filesList.contains(e.relatedTarget)) {

                return;

            }

            const folderEl = findFolderAncestor(e.target);

            const folderPath = folderEl ? folderEl.getAttribute('data-path') : null;



            if (folderPath) {

                currentDragTargetFolder = folderEl;

                currentDragTargetFolder.classList.add('drag-target');

                dragOverFolderPath = folderPath;

            } else {

                currentDragTargetFolder = null;

                dragOverFolderPath = null;

            }

        }

    });



    filesList.addEventListener('dragover', function(e) {

        e.preventDefault();

        e.stopPropagation();



        // Internal row drag: highlight the folder it would move into.
        if (typeof fileActionIsInternalDrag === 'function' && fileActionIsInternalDrag(e.dataTransfer)) {

            const target = dropFolderPathFor(e.target);

            const source = draggedRowPath || (typeof fileActionDraggedPath === 'function'
                ? fileActionDraggedPath(e.dataTransfer) : '');

            const allowed = !source || !(typeof fileActionIsInside === 'function' && fileActionIsInside(source, target));

            e.dataTransfer.dropEffect = allowed ? 'move' : 'none';

            // A refused move (a folder onto itself / into its own subtree) highlights
            // NOTHING. Passing '' here painted the ROOT outline instead, which claimed the
            // drop would land at the workspace root.
            if (allowed) paintMoveTarget(target);
            else clearMoveTarget();

            return;

        }



        if (e.dataTransfer.types && e.dataTransfer.types.includes('Files')) {

            const folderEl = findFolderAncestor(e.target);

            const folderPath = folderEl ? folderEl.getAttribute('data-path') : null;



            if (currentDragTargetFolder && (folderPath !== dragOverFolderPath)) {

                currentDragTargetFolder.classList.remove('drag-target');

            }



            if (folderPath) {

                currentDragTargetFolder = folderEl;

                currentDragTargetFolder.classList.add('drag-target');

                dragOverFolderPath = folderPath;

            } else {

                if (currentDragTargetFolder) {

                    currentDragTargetFolder.classList.remove('drag-target');

                }

                currentDragTargetFolder = null;

                dragOverFolderPath = null;

            }

        }

    });



    filesList.addEventListener('dragleave', function(e) {

        e.preventDefault();

        e.stopPropagation();



        if (e.relatedTarget && filesList.contains(e.relatedTarget)) {

            return;

        }



        clearMoveTarget();

    });



    filesList.addEventListener('drop', async function(e) {

        e.preventDefault();

        e.stopPropagation();



        // Internal row drag -> MOVE (a folder goes with its whole subtree).
        if (typeof fileActionIsInternalDrag === 'function' && fileActionIsInternalDrag(e.dataTransfer)) {

            const source = draggedRowPath || (typeof fileActionDraggedPath === 'function'
                ? fileActionDraggedPath(e.dataTransfer) : '');

            const destination = dropFolderPathFor(e.target);

            clearMoveTarget();

            draggedRowPath = '';

            // Never swallow the gesture: an unresolved source used to return silently, so
            // a drop that could not be read looked exactly like "drag to move does nothing".
            if (!source) {

                showNotification('Could not read the dragged item - try again', 'error');

                return;

            }

            if (typeof moveFileItem !== 'function') {

                showNotification('Move is unavailable - reload the page', 'error');

                return;

            }

            await moveFileItem(source, destination);

            return;

        }



        if (currentDragTargetFolder) {

            currentDragTargetFolder.classList.remove('drag-target');

        }



        if (e.dataTransfer.types && e.dataTransfer.types.includes('Files') && e.dataTransfer.files.length > 0) {

            const folderEl = findFolderAncestor(e.target);

            const folderPath = folderEl ? folderEl.getAttribute('data-path') : null;



            if (folderPath) {

                let successCount = 0;

                for (const file of Array.from(e.dataTransfer.files)) {

                    const success = await uploadFileToFolder(file, folderPath);

                    if (success) successCount++;

                }

                if (successCount > 0) {

                    loadTree(folderPath);

                }

            } else {

                let addedCount = 0;

                for (const file of Array.from(e.dataTransfer.files)) {

                    if (file.size > 2 * 1024 * 1024 * 1024) {

                        showNotification(`File too large (max 2GB): ${file.name}`, 'error');

                        continue;

                    }

                    const formData = new FormData();

                    formData.append('file', file);

                    try {

                        const r = await fetch('/api/upload', { method: 'POST', body: formData });

                        if (!r.ok) throw new Error();

                        const data = await r.json();

                        COOLEMS.selectedFiles.push({

                            name: file.name,

                            url: data.url,

                            content: data.content || null,

                            is_text: data.is_text,

                            is_image: data.is_image

                        });

                        addedCount++;

                    } catch (err) {

                        showNotification(`Failed to upload ${file.name}`, 'error');

                    }

                }

                if (addedCount > 0) {

                    updateFilePreview();

                    showNotification(`Added ${addedCount} file(s) to prompt`, 'success');

                }

            }



            currentDragTargetFolder = null;

            dragOverFolderPath = null;

            return;

        }



        currentDragTargetFolder = null;

        dragOverFolderPath = null;

    });



    // Remember which row is being dragged (dragstart fires on the row, not on the list).
    document.addEventListener('dragstart', function(e) {

        if (typeof fileActionIsInternalDrag === 'function' && fileActionIsInternalDrag(e.dataTransfer)) {

            draggedRowPath = typeof fileActionDraggedPath === 'function' ? fileActionDraggedPath(e.dataTransfer) : '';

        }

    });

    document.addEventListener('dragend', function() {

        draggedRowPath = '';

        clearMoveTarget();

    });

})();
