/*
     * preview-edit.js - split from file-handler.js on 2026-09-18 (verbatim cut/paste).
     * Part of the CLIENT/static/ui/files/ module group. Load order matters:
     * files-state.js must load first; see index.html script tags.
     */

    
// Preview edit/save/cancel/close + image resolution display.



function toggleEditPreview() {

    const contentEl = document.getElementById('previewContent');

    const display = document.getElementById('previewTextDisplay');

    if (!display) return;



    previewEditing = true;

    const editBtn = document.getElementById('previewEditBtn');

    const saveBtn = document.getElementById('previewSaveBtn');

    const cancelBtn = document.getElementById('previewCancelBtn');

    

    if (editBtn) editBtn.style.display = 'none';

    if (saveBtn) saveBtn.style.display = 'flex';

    if (cancelBtn) cancelBtn.style.display = 'flex';



    // Replace display with textarea

    contentEl.innerHTML = `<textarea class="preview-text-editor" id="previewTextEditor">${escapeHtml(currentPreviewOriginalText)}</textarea>`;

}



function cancelEditPreview() {

    previewEditing = false;

    const editBtn = document.getElementById('previewEditBtn');

    const saveBtn = document.getElementById('previewSaveBtn');

    const cancelBtn = document.getElementById('previewCancelBtn');

    

    if (editBtn) editBtn.style.display = 'flex';

    if (saveBtn) saveBtn.style.display = 'none';

    if (cancelBtn) cancelBtn.style.display = 'none';



    const contentEl = document.getElementById('previewContent');

    contentEl.innerHTML = `<div class="preview-text-display" id="previewTextDisplay">${escapeHtml(currentPreviewOriginalText)}</div>`;

}



async function savePreviewText() {

    const editor = document.getElementById('previewTextEditor');

    if (!editor) return;



    const newText = editor.value;

    try {

        const r = await fetch('/api/file-content', {

            method: 'POST',

            headers: {'Content-Type': 'application/json'},

            body: JSON.stringify({path: currentPreviewPath, content: newText})

        });

        if (!r.ok) throw new Error('Save failed');

        currentPreviewOriginalText = newText;

        previewEditing = false;



        const editBtn = document.getElementById('previewEditBtn');

        const saveBtn = document.getElementById('previewSaveBtn');

        const cancelBtn = document.getElementById('previewCancelBtn');



        if (editBtn) editBtn.style.display = 'flex';

        if (saveBtn) saveBtn.style.display = 'none';

        if (cancelBtn) cancelBtn.style.display = 'none';



        const contentEl = document.getElementById('previewContent');

        contentEl.innerHTML = `<div class="preview-text-display" id="previewTextDisplay">${escapeHtml(newText)}</div>`;

        showNotification('File saved', 'success');

    } catch (e) {

        showNotification('Failed to save file', 'error');

    }

}



function showImageResolution(imgEl) {

    const resolutionEl = document.getElementById('previewImageResolution');

    if (!resolutionEl) return;



    const width = imgEl.naturalWidth;

    const height = imgEl.naturalHeight;



    if (width && height) {

        resolutionEl.innerHTML = `

            <span style="display:flex; align-items:center; gap:4px;">

                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"></rect><circle cx="8.5" cy="8.5" r="1.5"></circle><polyline points="21 15 16 10 5 21"></polyline></svg>

                ${width} × ${height} px

            </span>

            <span style="display:flex; align-items:center; gap:4px;">

                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline></svg>

                ${(width * height / 1000000).toFixed(1)} MP

            </span>

        `;

    }

}



function closePreview() {

    const panel = document.getElementById('previewPanel');

    currentPreviewPath = '';



    // BUG FIX (2026-08-27): this function is the SINGLE owner of "close preview" -

    // the Close X button in index.html calls it. It must also reset PreviewManager's

    // state so that:

    //   * a stale async loadFile() resolving late can no longer re-expand the panel

    //     (the "closes but opens it back again" bug), and

    //   * the redundant onExit reload wired in app.js has nothing left to reload.

    if (window.PreviewManager) {

        const pm = window.PreviewManager;

        pm._closing = true; // stale async loads from here on are dropped by their guards

                            // (cleared again when the user opens a new file via loadFile())

        pm.currentFile = null;

        pm.currentContent = null;

        pm.originalContent = null;

        pm.currentHighlighter = null;

        pm.isDiffMode = false;

        pm.isEditing = false;



        // Hide + clear all diff UI (covers closing while a comparison is on screen)

        const diffContainer = document.getElementById('diffContainer');

        const diffToolbar = document.getElementById('diffToolbar');

        const diffUnified = document.getElementById('diffUnifiedContainer');

        const diffLeft = document.getElementById('diffContentLeft');

        const diffRight = document.getElementById('diffContentRight');

        const statsContainer = document.getElementById('diffStats');

        if (diffContainer) diffContainer.style.display = 'none';

        if (diffToolbar) diffToolbar.style.display = 'none';

        if (diffUnified) diffUnified.style.display = 'none';

        if (diffLeft) diffLeft.innerHTML = '';

        if (diffRight) diffRight.innerHTML = '';

        if (statsContainer) statsContainer.innerHTML = '';



        try { if (window.SyncScroll) window.SyncScroll.destroy(); } catch (_) {}

        try { if (window.DiffView) window.DiffView.clear(); } catch (_) {}



        const contentEl = document.getElementById('previewContent');

        if (contentEl) {

            contentEl.style.display = 'block';

            contentEl.innerHTML = '<div class="preview-empty">' +

                '<svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"></path><circle cx="12" cy="12" r="3"></circle></svg>' +

                '<p>Click a file to preview</p></div>';

        }

        const filenameEl = document.getElementById('previewFilename');

        if (filenameEl) filenameEl.textContent = '';



        // Closing the preview also cancels an in-flight "pick second file" mode.

        if (window.CompareSelectMode && window.CompareSelectMode.isActive()) {

            window.CompareSelectMode.end(false, 'preview-closed');

        }

    }



    if (panel) {

        panel.classList.add('collapsed');

        panel.style.width = '';

    }



    // Remove highlighted preview

    const prev = document.querySelector('.tree-file.previewed');

    if (prev) prev.classList.remove('previewed');

}



function downloadPreviewFile() {

if (currentPreviewPath) {

    downloadSingle(currentPreviewPath);

}

}
