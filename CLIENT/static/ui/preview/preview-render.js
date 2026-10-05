/**
 * preview/preview-render.js - renderPreview branches (pdf/image/highlighted/plain) + panel UI
 *
 * Split from static/ui/preview.js (2026-09-18). Phase 1: verbatim method cut - no behavior change.
 * Entry point of the group: preview-manager.js (loaded as <script type="module"> in index.html).
 */
import { PreviewManager } from './preview-class.js';
import { buildAuthedUrl } from './preview-utils.js?v=20261005a';

Object.assign(PreviewManager.prototype, {
    // SECURITY fix 2026-10-05: ASYNC on purpose - the image/PDF branches must await
    // buildAuthedUrl() (it mints a short-lived media token). Every caller already awaits.
    async renderPreview(content, filename) {
        if (!this.previewContent) return;
        
        const schema = window.SchemaManager ? window.SchemaManager.getCurrentSchema() : null;
        
        let html = '';
        
        // Check if this is an image file or PDF
        const imageExtensions = ['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'ico', 'svg'];
        const ext = filename.split('.').pop().toLowerCase();
        const isImage = imageExtensions.includes(ext);
        const isPdf = this.pdfExtensions.includes(ext);
        
        if (isPdf) {
            // Render PDF as iframe - use the download endpoint with inline=true.
            // SECURITY fix 2026-10-05: ?t=<media token> instead of ?api_key=... (the master
            // key no longer appears in any URL; see preview-utils.js buildAuthedUrl()).
            const pdfUrl = await buildAuthedUrl(`/api/download?path=${encodeURIComponent(this.currentFile.path)}&inline=true`);
            html = `
                <div style="width:100%; height:100%; min-height: 600px;">
                    <iframe src="${pdfUrl}" 
                            style="width:100%; height:700px; border:none; border-radius:8px;" 
                            title="${this.escapeHtml(filename)}"></iframe>
                </div>
            `;
        } else if (isImage) {
            // SECURITY (2026-08-25): /files/ is no longer public - serve images through
            // the authenticated download endpoint. <img> can't send headers, so since 2026-10-05 it
            // carries a short-lived path-bound media token (?t=) minted via header auth -
            // never the master key (which used to travel as ?api_key=...&email=...).
            const imgUrl = await buildAuthedUrl(`/api/download?path=${encodeURIComponent(this.currentFile.path)}&inline=true`);
            html = `
                <div style="width:100%; text-align:center; padding: 20px;">
                    <img src="${imgUrl}" alt="${this.escapeHtml(filename)}" 
                         style="max-width:100%; max-height:80vh; border-radius:8px; border:1px solid var(--border);"
                         onclick="window.open(this.src, '_blank')" 
                         title="Click to open full size">
                    <!-- Rich metadata card (size on disk, resolution, EXIF, GPS + Google Maps link).
                         Filled in asynchronously by loadImageMetadata() - preview-image-meta.js -->
                    <div class="preview-image-resolution img-meta-card" id="previewImageMeta">
                        <span>📐 Image: ${this.escapeHtml(filename)}</span>
                        <span class="img-meta-loading">Loading metadata…</span>
                    </div>
                </div>
            `;
        } else if (this.currentHighlighter && schema) {
            const highlighted = this.currentHighlighter.highlight(content, schema);
            const lines = content.split('\n');
            const lineNumbers = lines.map((_, i) => `<span>${i + 1}</span>`).join('');
            html = `
                <div class="preview-with-line-numbers">
                    <div class="line-numbers">${lineNumbers}</div>
                    <div class="preview-text-display" id="previewTextDisplay">${highlighted}</div>
                </div>
            `;
        } else {
            const lines = content.split('\n');
            const lineNumbers = lines.map((_, i) => `<span>${i + 1}</span>`).join('');
            const escapedContent = this.escapeHtml(content);
            
            html = `
                <div class="preview-with-line-numbers">
                    <div class="line-numbers">${lineNumbers}</div>
                    <div class="preview-text-display" id="previewTextDisplay">${escapedContent.replace(/\n/g, '<br>')}</div>
                </div>
            `;
        }
        
        this.previewContent.innerHTML = html;

        // ADDED (2026-09-18): fetch + render the rich metadata card for images.
        // Fire-and-forget: the <img> above is already visible; a slow/failed request
        // only affects the small row below it (stale-guarded inside).
        if (isImage && this.currentFile) {
            this.loadImageMetadata(this.currentFile.path, filename);
        }
        
        if (schema && this.previewContent) {
            this.previewContent.style.backgroundColor = schema.background;
            this.previewContent.style.color = schema.text;
        }
        
        const display = document.getElementById('previewTextDisplay');
        if (display) {
            display.setAttribute('data-raw-content', content);
        }
    },
    
    /**
     * Reapply syntax highlighting (called when schema changes)
     */
    reapplyHighlighting() {
        if (this.currentContent && this.currentFile && !this.isDiffMode && !this.isEditing) {
            // 2026-10-03: renderPreview is async (media token mint); callers are sync
            // event handlers -> fire-and-forget with a catch.
            void this.renderPreview(this.currentContent, this.currentFile.name).catch(e => console.error('[Preview] reapplyHighlighting render failed:', e));
        }
    },
    
    /**
     * Update preview UI elements
     * @param {string} filename - File name
     */
    updatePreviewUI(filename) {
        if (this.previewFilename) {
            this.previewFilename.textContent = filename;
        }
        
        const editBtn = document.getElementById('previewEditBtn');
        const compareBtn = document.getElementById('previewCompareBtn');
        const downloadBtn = document.getElementById('previewDownloadBtn');
        
        // Check if file is editable text file
        const isTextFile = filename.match(/\.(txt|md|json|xml|yml|yaml|ini|cfg|conf|log|py|js|html|css|java|cpp|c|h|go|rs|ts|jsx|tsx)$/i);
        const isPyFile = filename.toLowerCase().endsWith('.py');
        
        if (editBtn) {
            editBtn.style.display = isTextFile ? 'flex' : 'none';
        }
        if (compareBtn) compareBtn.style.display = 'flex';
        if (downloadBtn) downloadBtn.style.display = 'flex';
        
        // Run button for .py files
        const runBtn = document.getElementById('previewRunBtn');
        if (runBtn) {
            runBtn.style.display = isPyFile ? 'flex' : 'none';
        }
    },
    
    /**
     * Show preview panel
     */
    showPreviewPanel() {
        // BUG FIX (2026-08-27): never fight a close that is in progress - the panel
        // must stay closed until the user explicitly opens another file.
        if (this._closing) return;
        if (this.previewPanel && this.previewPanel.classList.contains('collapsed')) {
            this.previewPanel.classList.remove('collapsed');
            
            const savedWidth = localStorage.getItem('previewPanelWidth');
            if (savedWidth) {
                this.previewPanel.style.width = savedWidth + 'px';
            }
        }
    },
    
    /**
     * Show loading indicator
     */
    showLoading() {
        if (this.previewContent) {
            this.previewContent.innerHTML = `
                <div class="preview-empty">
                    <div class="loading-spinner"></div>
                    <p>Loading file...</p>
                </div>
            `;
        }
    },
    
    /**
     * Show error message
     * @param {string} message - Error message
     */
    showError(message) {
        if (this.previewContent) {
            this.previewContent.innerHTML = `
                <div class="preview-empty">
                    <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1">
                        <circle cx="12" cy="12" r="10"></circle>
                        <line x1="12" y1="8" x2="12" y2="12"></line>
                        <line x1="12" y1="16" x2="12.01" y2="16"></line>
                    </svg>
                    <p style="color: var(--danger);">${this.escapeHtml(message)}</p>
                </div>
            `;
        }
    }
});
