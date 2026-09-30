/*
     * drop-to-folder.js - split from file-handler.js on 2026-09-18 (verbatim cut/paste).
     * Part of the CLIENT/static/ui/files/ module group. Load order matters:
     * files-state.js must load first; see index.html script tags.
     */

    
// External desktop files dropped onto tree folders.



// ===== FILES LIST DRAG AND DROP (external files into folders) =====



(function initFilesListDrop() {

    const filesList = document.getElementById('filesList');

    if (!filesList) return;



    let currentDragTargetFolder = null;

    let dragOverFolderPath = null;



    function findFolderAncestor(el) {

        while (el && el !== filesList) {

            if (el.classList && el.classList.contains('tree-folder')) {

                return el;

            }

            el = el.parentElement;

        }

        return null;

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



        if (currentDragTargetFolder) {

            currentDragTargetFolder.classList.remove('drag-target');

            currentDragTargetFolder = null;

            dragOverFolderPath = null;

        }

    });



    filesList.addEventListener('drop', async function(e) {

        e.preventDefault();

        e.stopPropagation();



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

})();
