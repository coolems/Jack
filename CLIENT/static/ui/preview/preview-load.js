/**
 * preview/preview-load.js - loadFile (stale-async guards) + compare button
 *
 * Split from static/ui/preview.js (2026-09-18). Phase 1: verbatim method cut - no behavior change.
 * Entry point: preview-manager.js (loaded as <script type="module">).
 */

import { PreviewManager } from './preview-class.js';

Object.assign(PreviewManager.prototype, {
    setupCompareButton() {
        const compareBtn = document.getElementById('previewCompareBtn');
        if (compareBtn) {
            compareBtn.onclick = () => this.startComparison();
        }
    },
    
    /**
     * Load and preview a file
     * @param {string} filePath - Path to the file
     * @param {string} filename - Name of the file
     */
    async loadFile(filePath, filename) {
        // BUG FIX (2026-08-27): a new user-initiated open cancels any pending close.
        this._closing = false;
        const loadSeq = ++this._loadSeq;
        this.currentFile = { path: filePath, name: filename };
        
        this.showLoading();
        
        // Exit diff mode if active
        if (this.isDiffMode && window.DiffUI) {
            window.DiffUI.exit();
            this.isDiffMode = false;
        }
        
        try {
            // Check if this is a PDF file
            const ext = filename.split('.').pop().toLowerCase();
            const isPdf = this.pdfExtensions.includes(ext);
            
            if (isPdf) {
                // BUG FIX (2026-08-27): stale async load -> user closed in the meantime.
                if (loadSeq !== this._loadSeq || this._closing) return;
                // For PDFs, render directly as iframe - don't fetch as text content
                this.currentContent = null;
                this.originalContent = null;
                this.currentHighlighter = null;
                await this.renderPreview(null, filename);  // 2026-10-03: async now (media token)
                this.updatePreviewUI(filename);
                this.showPreviewPanel();
                this.isDiffMode = false;
                
                // Hide diff UI elements
                const diffContainer = document.getElementById('diffContainer');
                const diffToolbar = document.getElementById('diffToolbar');
                const diffUnified = document.getElementById('diffUnifiedContainer');
                const previewContent = document.getElementById('previewContent');
                
                if (diffContainer) diffContainer.style.display = 'none';
                if (diffUnified) diffUnified.style.display = 'none';
                if (diffToolbar) diffToolbar.style.display = 'none';
                if (previewContent) previewContent.style.display = 'block';
                return;
            }
            
            const response = await fetch(`/api/file-content?path=${encodeURIComponent(filePath)}`);
            if (!response.ok) throw new Error('Failed to load');
            const data = await response.json();

            // BUG FIX (2026-08-27): the fetch above is awaited, so the user may have
            // clicked Close while it was in flight. A stale result must NOT re-open
            // the panel - that was the "close sometimes reopens" bug.
            if (loadSeq !== this._loadSeq || this._closing) return;

            this.currentContent = data.content;
            this.originalContent = data.content;
            
            if (window.SyntaxRegistry) {
                this.currentHighlighter = window.SyntaxRegistry.getHighlighterForFile(filename);
            }
            
            await this.renderPreview(this.currentContent, filename);  // 2026-10-03: async now (media token)
            this.updatePreviewUI(filename);
            this.showPreviewPanel();
            this.isDiffMode = false;
            
            // Hide diff UI elements
            const diffContainer = document.getElementById('diffContainer');
            const diffToolbar = document.getElementById('diffToolbar');
            const diffUnified = document.getElementById('diffUnifiedContainer');
            const previewContent = document.getElementById('previewContent');
            
            if (diffContainer) diffContainer.style.display = 'none';
            if (diffUnified) diffUnified.style.display = 'none';
            if (diffToolbar) diffToolbar.style.display = 'none';
            if (previewContent) previewContent.style.display = 'block';
            
        } catch (e) {
            console.error('[PreviewManager] Failed to load file:', e);
            this.showError(`Failed to load file: ${filename}`);
        }
    }
});
