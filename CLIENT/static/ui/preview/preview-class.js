/**
 * preview/preview-class.js - PreviewManager class core
 *
 * Split from static/ui/preview.js (2026-09-18). Phase 1: verbatim method cut - no behavior change.
 * Entry point: preview-manager.js (loaded as <script type="module">).
 */

class PreviewManager {
    constructor() {
        this.currentFile = null;
        this.currentContent = null;
        this.currentHighlighter = null;
        this.fontSize = 14;
        this.isDiffMode = false;
        this.initialized = false;
        this.isEditing = false;
        // BUG FIX (2026-08-27): guard against a stale async loadFile() re-opening the
        // panel AFTER close(). `_closing` is set by close(); `_loadSeq` is bumped on every load so an
        // in-flight fetch that resolves late can no longer act.
        this._closing = false;
        this._loadSeq = 0;
        this.originalContent = null;
        this.previewContent = null;
        this.previewFilename = null;
        this.pdfExtensions = ['pdf'];
    }
    
    init() {
        if (this.initialized) return;
        
        this.cacheElements();
        this.setupEventListeners();
        this.setupFontSizeControls();
        this.setupSchemaSelector();
        this.setupCompareButton();
        this.restoreFontSize();
        
        this.initialized = true;
        console.log('[PreviewManager] Initialized');
    }
    
    cacheElements() {
        this.previewContent = document.getElementById('previewContent');
        this.previewFilename = document.getElementById('previewFilename');
        this.previewPanel = document.getElementById('previewPanel');
    }
    
    setupEventListeners() {
        const editBtn = document.getElementById('previewEditBtn');
        if (editBtn) {
            editBtn.onclick = () => this.toggleEdit();
        }
        
        const saveBtn = document.getElementById('previewSaveBtn');
        if (saveBtn) {
            saveBtn.onclick = () => this.saveEdit();
        }
        
        const cancelBtn = document.getElementById('previewCancelBtn');
        if (cancelBtn) {
            cancelBtn.onclick = () => this.cancelEdit();
        }
        
        const downloadBtn = document.getElementById('previewDownloadBtn');
        if (downloadBtn) {
            downloadBtn.onclick = () => this.download();
        }
        
        const runBtn = document.getElementById('previewRunBtn');
        if (runBtn) {
            runBtn.onclick = () => this.runPythonFile();
        }
        
        // BUG FIX (2026-08-27): the Close X button in index.html calls the global
        // closePreview() (file-handler.js), which is now the SINGLE owner of closing
        // the panel. The old selector binding above never matched that button, so it
        // was dead code - removed.
    }
}

export { PreviewManager };
