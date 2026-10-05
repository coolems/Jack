/*
     * preview-core.js - split from file-handler.js on 2026-09-18 (verbatim cut/paste).
     * Part of the CLIENT/static/ui/files/ module group. Load order matters:
     * files-state.js must load first; see index.html script tags.
     */

    
// Preview entry point + legacy preview fallback (text/image/pdf).



// ===== PREVIEW FUNCTIONS (with PreviewManager integration) =====







function getFileExtension(filename) {

    return filename.split('.').pop().toLowerCase();

}



function isTextFile(filename) {

    return TEXT_EXTENSIONS.includes(getFileExtension(filename));

}



function isImageFile(filename) {

    return IMAGE_EXTENSIONS.includes(getFileExtension(filename));

}



function isPdfFile(filename) {

    return PDF_EXTENSIONS.includes(getFileExtension(filename));

}



// Main preview function - uses PreviewManager if available

async function previewFile(path) {

    const filename = path.split('/').pop() || path.split('\\').pop();

    

    // Remove previous highlighted preview

    const prev = document.querySelector('.tree-file.previewed');

    if (prev) prev.classList.remove('previewed');

    

    // Highlight current file

    const fileEl = document.querySelector(`.tree-file[data-path="${CSS.escape(path)}"]`);

    if (fileEl) fileEl.classList.add('previewed');

    

    // Use PreviewManager if available (new syntax highlighting system)

    if (window.PreviewManager && typeof window.PreviewManager.loadFile === 'function') {

        window.PreviewManager.loadFile(path, filename);

    } else {

        // Fallback to legacy preview

        legacyPreviewFile(path);

    }

}



// Legacy preview function (fallback if PreviewManager not available)

async function legacyPreviewFile(path) {

    // Open preview panel if collapsed

    const panel = document.getElementById('previewPanel');

    if (panel.classList.contains('collapsed')) {

        panel.classList.remove('collapsed');

    }



    currentPreviewPath = path;

    previewEditing = false;



    // Hide edit/save/cancel buttons by default

    const editBtn = document.getElementById('previewEditBtn');

    const saveBtn = document.getElementById('previewSaveBtn');

    const cancelBtn = document.getElementById('previewCancelBtn');

    const compareBtn = document.getElementById('previewCompareBtn');

    const downloadBtn = document.getElementById('previewDownloadBtn');

    

    if (editBtn) editBtn.style.display = 'none';

    if (saveBtn) saveBtn.style.display = 'none';

    if (cancelBtn) cancelBtn.style.display = 'none';

    if (compareBtn) compareBtn.style.display = 'flex';

    if (downloadBtn) downloadBtn.style.display = 'flex';



    const filename = path.split('/').pop() || path.split('\\').pop();

    const filenameEl = document.getElementById('previewFilename');

    if (filenameEl) filenameEl.textContent = filename;

    

    const contentEl = document.getElementById('previewContent');



    if (isImageFile(filename)) {

        // SECURITY fix 2026-10-05: media token (?t=) via header auth - no key/email in URL.

        // AUDIT FIX (2026-10-05): guarded like the text branch below - a failed mint must
        // not leave the preview panel half-rendered with an unhandled promise rejection.

        let imgUrl;

        try {

            const _imgToken = await window.getMediaToken(path);

            imgUrl = `/api/download?path=${encodeURIComponent(path)}&t=${encodeURIComponent(_imgToken)}`;

        } catch (e) {

            contentEl.innerHTML = `<div class="preview-empty"><p style="color:var(--danger);">Failed to get media token for image</p></div>`;

            return;

        }

        contentEl.innerHTML = `

            <div style="width:100%; text-align:center;">

                <img src="${imgUrl}" alt="${escapeHtml(filename)}" onclick="window.open(this.src, '_blank')" title="Click to open full size"

                     onload="showImageResolution(this)"

                     style="max-width:100%; border-radius:8px; border:1px solid var(--border);">

                <div class="preview-image-resolution" id="previewImageResolution" style="margin-top:10px; font-size:12px; color:var(--text-muted); display:flex; justify-content:center; gap:12px; flex-wrap:wrap;"></div>

            </div>

        `;

        return;

    }



    if (isTextFile(filename)) {

        try {

            const r = await fetch(`/api/file-content?path=${encodeURIComponent(path)}`);

            if (!r.ok) throw new Error('Failed to load');

            const data = await r.json();

            currentPreviewOriginalText = data.content;

            contentEl.innerHTML = `<div class="preview-text-display" id="previewTextDisplay">${escapeHtml(data.content)}</div>`;

            if (editBtn) editBtn.style.display = 'flex';

        } catch (e) {

            contentEl.innerHTML = `<div class="preview-empty"><p style="color:var(--danger);">Failed to load file content</p></div>`;

        }

        return;

    }



    if (isPdfFile(filename)) {

        // SECURITY fix 2026-10-05: media token (?t=) via header auth - no key/email in URL.

        // AUDIT FIX (2026-10-05): guarded like the text branch below - a failed mint must
        // not leave the preview panel half-rendered with an unhandled promise rejection.

        let pdfUrl;

        try {

            const _pdfToken = await window.getMediaToken(path);

            pdfUrl = `/api/download?path=${encodeURIComponent(path)}&inline=true&t=${encodeURIComponent(_pdfToken)}`;

        } catch (e) {

            contentEl.innerHTML = `<div class="preview-empty"><p style="color:var(--danger);">Failed to get media token for PDF</p></div>`;

            return;

        }

        contentEl.innerHTML = `

            <div style="width:100%; height:100%; min-height: 400px;">

                <iframe src="${pdfUrl}#toolbar=0&navpanes=0&scrollbar=1" 

                        style="width:100%; height:600px; border:none; border-radius:8px;" />

            </div>

        `;

        return;

    }



    contentEl.innerHTML = `

        <div class="preview-empty">

            <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1"><path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"></path><polyline points="13 2 13 9 20 9"></polyline></svg>

            <p>Preview not available for this file type</p>

            <button class="file-action-btn" onclick="downloadSingle('${path.replace(/'/g, "\\'")}')">Download to view</button>

        </div>

    `;

}
