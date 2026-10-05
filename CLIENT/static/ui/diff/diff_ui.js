/**
 * diff/diff_ui.js - Diff UI Controls
 * Manages file selection, diff mode buttons, and sync scroll integration
 * 
 * UPDATED: Removed Accept Left/Right buttons
 *          Integrated with per-line arrow system
 */

class DiffUI {
    constructor() {
        this.isActive = false;
        this.currentFileA = null;
        this.currentFileB = null;
        this.availableFiles = [];
        this.onFileSelectedCallback = null;
        this.onExitCallback = null;
        this.initialized = false;
    }
    
    /**
     * Initialize diff UI controls
     */
    init() {
        if (this.initialized) return;
        
        this.setupEventListeners();
        this.loadAvailableFiles();
        this.initialized = true;
        console.log('[DiffUI] Initialized');
    }
    
    /**
     * Setup event listeners for diff controls
     */
    setupEventListeners() {
        // Diff mode buttons
        const sideBySideBtn = document.getElementById('diffModeSideBySide');
        const unifiedBtn = document.getElementById('diffModeUnified');
        
        if (sideBySideBtn) {
            // Remove existing listeners to prevent duplicates
            const newSideBySide = sideBySideBtn.cloneNode(true);
            sideBySideBtn.parentNode.replaceChild(newSideBySide, sideBySideBtn);
            newSideBySide.addEventListener('click', () => this.setMode('side-by-side'));
        }
        
        if (unifiedBtn) {
            const newUnified = unifiedBtn.cloneNode(true);
            unifiedBtn.parentNode.replaceChild(newUnified, unifiedBtn);
            newUnified.addEventListener('click', () => this.setMode('unified'));
        }
        
        // Sync scroll toggle
        const syncScrollBtn = document.getElementById('diffSyncScroll');
        if (syncScrollBtn) {
            const newSyncScroll = syncScrollBtn.cloneNode(true);
            syncScrollBtn.parentNode.replaceChild(newSyncScroll, syncScrollBtn);
            newSyncScroll.addEventListener('click', () => this.toggleSyncScroll());
        }
        
        // Exit button
        const exitBtn = document.getElementById('diffExitBtn');
        if (exitBtn) {
            const newExitBtn = exitBtn.cloneNode(true);
            exitBtn.parentNode.replaceChild(newExitBtn, exitBtn);
            newExitBtn.addEventListener('click', () => this.exit());
        }
        
        // Compare button in file selector
        const compareBtn = document.getElementById('diffCompareBtn');
        if (compareBtn) {
            const newCompareBtn = compareBtn.cloneNode(true);
            compareBtn.parentNode.replaceChild(newCompareBtn, compareBtn);
            newCompareBtn.addEventListener('click', () => this.startComparison());
        }
        
        // Close file selector
        const closeSelectorBtn = document.getElementById('diffCloseSelector');
        if (closeSelectorBtn) {
            const newCloseBtn = closeSelectorBtn.cloneNode(true);
            closeSelectorBtn.parentNode.replaceChild(newCloseBtn, closeSelectorBtn);
            newCloseBtn.addEventListener('click', () => this.hideFileSelector());
        }
        
        // Close on Escape key
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                const selector = document.getElementById('diffFileSelector');
                if (selector && selector.style.display === 'block') {
                    this.hideFileSelector();
                }
                if (this.isActive) {
                    this.exit();
                }
            }
        });
    }
    
    /**
     * Load available files from working root directory
     */
    async loadAvailableFiles() {
        try {
            const response = await fetch('/api/tree');
            const data = await response.json();
            
            this.availableFiles = this.flattenTree(data.items, '');
            this.populateFileSelect();
            console.log('[DiffUI] Loaded', this.availableFiles.length, 'files');
        } catch (e) {
            console.error('[DiffUI] Failed to load files:', e);
        }
    }
    
    /**
     * Flatten tree structure to list of files
     */
    flattenTree(items, currentPath) {
        let files = [];
        
        for (const item of items) {
            const fullPath = currentPath ? `${currentPath}/${item.name}` : item.name;
            
            if (item.type === 'file') {
                // Only include text files that can be diffed
                const ext = item.name.split('.').pop().toLowerCase();
                const textExtensions = ['txt', 'md', 'py', 'js', 'html', 'css', 'json', 'xml', 'java', 'c', 'cpp', 'h', 'csv', 'log', 'yaml', 'yml', 'go', 'rs', 'ts', 'jsx', 'tsx'];
                if (textExtensions.includes(ext)) {
                    files.push({
                        name: item.name,
                        path: fullPath,
                        size: item.size
                    });
                }
            } else if (item.type === 'folder' && item.items) {
                files = files.concat(this.flattenTree(item.items, fullPath));
            }
        }
        
        return files;
    }
    
    /**
     * Populate file select dropdown
     */
    populateFileSelect() {
        const select = document.getElementById('diffFileSelect');
        if (!select) return;
        
        select.innerHTML = '<option value="">Select file to compare...</option>';
        
        for (const file of this.availableFiles) {
            const option = document.createElement('option');
            option.value = file.path;
            option.textContent = `${file.name} (${this.formatFileSize(file.size)})`;
            select.appendChild(option);
        }
    }
    
    /**
     * Show file selector and start comparison flow
     */
    showFileSelector(currentFilePath) {
        this.currentFileA = currentFilePath;
        
        // Refresh file list to ensure it's up to date
        this.loadAvailableFiles().then(() => {
            const selector = document.getElementById('diffFileSelector');
            if (selector) {
                selector.style.display = 'block';
                
                // Disable the current file in the dropdown
                const select = document.getElementById('diffFileSelect');
                if (select) {
                    for (let i = 0; i < select.options.length; i++) {
                        const opt = select.options[i];
                        if (opt.value === currentFilePath) {
                            opt.disabled = true;
                            opt.textContent = opt.textContent + ' (current)';
                        } else {
                            opt.disabled = false;
                        }
                    }
                }
            }
        });
    }
    
    /**
     * Hide file selector
     */
    hideFileSelector() {
        const selector = document.getElementById('diffFileSelector');
        if (selector) {
            selector.style.display = 'none';
        }
        // Clear the current file A reference (don't clear, keep for context)
    }
    
    /**
     * Start comparison between two files
     */
    async startComparison() {
        const select = document.getElementById('diffFileSelect');
        const fileBPath = select ? select.value : null;
        
        if (!this.currentFileA || !fileBPath) {
            this.showNotification('Please select a file to compare', 'warning');
            return;
        }
        
        if (this.currentFileA === fileBPath) {
            this.showNotification('Cannot compare a file with itself', 'warning');
            return;
        }
        
        // Use callback if provided (for PreviewManager integration)
        if (this.onFileSelectedCallback) {
            this.onFileSelectedCallback(fileBPath);
            this.hideFileSelector();
        } else {
            // Fallback: fetch and compare directly
            try {
                const [contentA, contentB] = await Promise.all([
                    this.fetchFileContent(this.currentFileA),
                    this.fetchFileContent(fileBPath)
                ]);
                
                this.currentFileB = fileBPath;
                this.enterDiffMode(this.currentFileA, contentA, this.currentFileB, contentB);
                this.hideFileSelector();
                
            } catch (e) {
                console.error('[DiffUI] Failed to load files:', e);
                this.showNotification('Failed to load files for comparison', 'error');
            }
        }
    }
    
    /**
     * Fetch file content from server
     */
    async fetchFileContent(path) {
        const response = await fetch(`/api/file-content?path=${encodeURIComponent(path)}`);
        if (!response.ok) throw new Error('Failed to fetch');
        const data = await response.json();
        return data.content;
    }
    
    /**
     * Enter diff mode and show diff view
     */
    enterDiffMode(pathA, contentA, pathB, contentB) {
        this.isActive = true;
        
        // Hide single preview, show diff view
        const previewContent = document.getElementById('previewContent');
        const diffContainer = document.getElementById('diffContainer');
        const diffToolbar = document.getElementById('diffToolbar');
        const schemaSelector = document.getElementById('schemaSelector');
        const diffUnified = document.getElementById('diffUnifiedContainer');
        
        if (previewContent) previewContent.style.display = 'none';
        if (diffContainer) diffContainer.style.display = 'flex';
        if (diffUnified) diffUnified.style.display = 'none';
        if (diffToolbar) diffToolbar.style.display = 'flex';
        if (schemaSelector) schemaSelector.style.display = 'inline-block';
        
        // Initialize DiffView if not already
        if (window.DiffView && window.SyntaxRegistry && window.SchemaManager) {
            window.DiffView.init(window.SyntaxRegistry, window.SchemaManager);
            
            const leftContainer = document.getElementById('diffContentLeft');
            const rightContainer = document.getElementById('diffContentRight');
            
            window.DiffView.renderSideBySide(
                { path: pathA, content: contentA },
                { path: pathB, content: contentB },
                leftContainer,
                rightContainer
            );
        } else {
            console.error('[DiffUI] DiffView or dependencies not loaded');
            this.showError('Diff module not loaded properly');
            return;
        }
        
        // Initialize sync scroll
        const leftPanel = document.getElementById('diffContentLeft');
        const rightPanel = document.getElementById('diffContentRight');
        
        if (window.SyncScroll) {
            window.SyncScroll.initialize(leftPanel, rightPanel);
            // Enable sync scroll by default
            const syncScrollBtn = document.getElementById('diffSyncScroll');
            if (syncScrollBtn) syncScrollBtn.classList.add('active');
        }
        
        // Set default mode button state
        const sideBySideBtn = document.getElementById('diffModeSideBySide');
        const unifiedBtn = document.getElementById('diffModeUnified');
        if (sideBySideBtn) sideBySideBtn.classList.add('active');
        if (unifiedBtn) unifiedBtn.classList.remove('active');
        
        this.showNotification(`Comparing: ${pathA.split('/').pop()} ↔ ${pathB.split('/').pop()}`, 'info');
    }
    
    /**
     * Set diff display mode
     */
    setMode(mode) {
        if (!this.isActive) return;
        
        const leftContainer = document.getElementById('diffContentLeft');
        const rightContainer = document.getElementById('diffContentRight');
        const unifiedContainer = document.getElementById('diffUnifiedContainer');
        const diffContainer = document.getElementById('diffContainer');
        
        if (mode === 'side-by-side') {
            if (leftContainer) leftContainer.style.display = 'block';
            if (rightContainer) rightContainer.style.display = 'block';
            if (unifiedContainer) unifiedContainer.style.display = 'none';
            if (diffContainer) diffContainer.style.display = 'flex';
            
            if (window.DiffView) {
                window.DiffView.setMode('side-by-side', leftContainer, rightContainer);
            }
            
        } else if (mode === 'unified') {
            if (leftContainer) leftContainer.style.display = 'none';
            if (rightContainer) rightContainer.style.display = 'none';
            if (unifiedContainer) unifiedContainer.style.display = 'block';
            if (diffContainer) diffContainer.style.display = 'flex';
            
            if (window.DiffView) {
                window.DiffView.setMode('unified', unifiedContainer, null);
            }
        }
        
        // Update button states
        const sideBySideBtn = document.getElementById('diffModeSideBySide');
        const unifiedBtn = document.getElementById('diffModeUnified');
        
        if (sideBySideBtn) sideBySideBtn.classList.toggle('active', mode === 'side-by-side');
        if (unifiedBtn) unifiedBtn.classList.toggle('active', mode === 'unified');
    }
    
    /**
     * Toggle synchronized scrolling
     */
    toggleSyncScroll() {
        const isEnabled = window.SyncScroll ? window.SyncScroll.toggle() : false;
        const btn = document.getElementById('diffSyncScroll');
        
        if (btn) {
            btn.classList.toggle('active', isEnabled);
            btn.title = isEnabled ? 'Sync Scroll: ON' : 'Sync Scroll: OFF';
        }
        
        this.showNotification(isEnabled ? 'Sync scroll enabled' : 'Sync scroll disabled', 'info');
    }
    
    /**
     * Exit diff mode and return to single preview
     */
    exit() {
        // BUG FIX (2026-08-27): when PreviewManager owns the compare session it has
        // its own full cleanup + re-render (exitDiffMode). Running this whole path too
        // caused a DOUBLE exit per click: duplicate 'Exited comparison mode' toast AND -
        // via the onExit callback wiring in app.js/index.html - a redundant async
        // loadFile() that could resolve AFTER the user clicked Close and re-opened the
        // preview panel (the 'close sometimes reopens' bug).
        if (window.PreviewManager && window.PreviewManager.isDiffMode) {
            this.isActive = false;
            void window.PreviewManager.exitDiffMode().catch(e => console.error('[Preview] exitDiffMode failed:', e));
            return;
        }
        this.isActive = false;
        
        const previewContent = document.getElementById('previewContent');
        const diffContainer = document.getElementById('diffContainer');
        const diffToolbar = document.getElementById('diffToolbar');
        const schemaSelector = document.getElementById('schemaSelector');
        const diffUnified = document.getElementById('diffUnifiedContainer');
        
        if (previewContent) previewContent.style.display = 'block';
        if (diffContainer) diffContainer.style.display = 'none';
        if (diffUnified) diffUnified.style.display = 'none';
        if (diffToolbar) diffToolbar.style.display = 'none';
        if (schemaSelector) schemaSelector.style.display = 'inline-block';
        
        // Clear sync scroll
        if (window.SyncScroll) {
            window.SyncScroll.destroy();
        }
        
        // Clear diff view
        if (window.DiffView) {
            window.DiffView.clear();
        }
        
        // Clear the diff containers
        const leftContainer = document.getElementById('diffContentLeft');
        const rightContainer = document.getElementById('diffContentRight');
        if (leftContainer) leftContainer.innerHTML = '';
        if (rightContainer) rightContainer.innerHTML = '';
        
        // Clear stats
        const statsContainer = document.getElementById('diffStats');
        if (statsContainer) statsContainer.innerHTML = '';
        
        // Reload original preview if callback provided
        if (this.onExitCallback) {
            this.onExitCallback();
        }
        
        // Reset current files
        this.currentFileA = null;
        this.currentFileB = null;
        
        this.showNotification('Exited comparison mode', 'info');
    }
    
    /**
     * Set callback for when file is selected for comparison
     */
    onFileSelected(callback) {
        this.onFileSelectedCallback = callback;
    }
    
    /**
     * Set callback for when exit is triggered
     */
    onExit(callback) {
        this.onExitCallback = callback;
    }
    
    /**
     * Show error message in diff view
     */
    showError(message) {
        const leftContainer = document.getElementById('diffContentLeft');
        if (leftContainer) {
            leftContainer.innerHTML = `
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
    
    /**
     * Format file size
     */
    formatFileSize(bytes) {
        if (bytes === 0) return '0 B';
        const k = 1024;
        const sizes = ['B', 'KB', 'MB', 'GB'];
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
    }
    
    /**
     * Show notification
     */
    showNotification(message, type = 'info') {
        if (typeof window.showNotification === 'function') {
            window.showNotification(message, type);
        } else {
            console.log(`[DiffUI] ${type}: ${message}`);
            // Fallback notification
            const notification = document.createElement('div');
            notification.className = `notification notification-${type}`;
            notification.textContent = message;
            notification.style.position = 'fixed';
            notification.style.top = '20px';
            notification.style.right = '20px';
            notification.style.zIndex = '10001';
            document.body.appendChild(notification);
            setTimeout(() => notification.remove(), 3000);
        }
    }
    
    /**
     * Escape HTML
     */
    escapeHtml(text) {
        if (!text) return '';
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
}

// Singleton instance
window.DiffUI = new DiffUI();