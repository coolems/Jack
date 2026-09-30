/**
 * preview/preview-compare.js - start comparison + performComparison
 *
 * Split from static/ui/preview.js (2026-09-18). Phase 1: verbatim method cut - no behavior change.
 * Entry point: preview-manager.js (loaded as <script type="module">).
 */

import { PreviewManager } from './preview-class.js';

Object.assign(PreviewManager.prototype, {
    /**
         * Start comparison mode (2026-08-25 rework).
         * No popup anymore: the FILES PANEL enters "compare-select" mode (amber tint +
         * instruction banner above the file list) and the user picks the second file
         * by clicking a FILE row in the tree. All of that logic lives in its own module:
         *   static/ui/compare-select-mode.js  ->  window.CompareSelectMode
         */
        startComparison() {
            if (!this.currentFile) {
                this.showNotification('No file open to compare', 'warning');
                return;
            }

            // New in-place selection mode (preferred path).
            if (window.CompareSelectMode && typeof window.CompareSelectMode.start === 'function') {
                const started = window.CompareSelectMode.start(this.currentFile.path, async (fileBPath) => {
                    await this.performComparison(fileBPath);
                });
                return; // CompareSelectMode handles the "toggle off" case itself
            }

            // Defensive fallback: module failed to load - nothing usable left (old popup was removed).
            console.error('[PreviewManager] CompareSelectMode not available - compare selection disabled');
            this.showNotification('Compare select mode is unavailable', 'error');
        },
    
    /**
     * Perform file comparison
     * @param {string} fileBPath - Path to second file
     */
    async performComparison(fileBPath) {
        this.showLoading();
        
        try {
            const response = await fetch('/api/diff', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    path_a: this.currentFile.path,
                    path_b: fileBPath
                })
            });
            
            if (!response.ok) {
                const errorText = await response.text();
                throw new Error(`Failed to fetch files: ${response.status} ${errorText}`);
            }
            
            const data = await response.json();
            
            if (data.status !== 'ok') {
                throw new Error(data.message || 'Unknown error');
            }
            
            this.isDiffMode = true;
            
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
                    { path: data.file_a.path, name: data.file_a.name, size: data.file_a.size, content: data.file_a.content },
                    { path: data.file_b.path, name: data.file_b.name, size: data.file_b.size, content: data.file_b.content },
                    leftContainer,
                    rightContainer
                );
            } else {
                console.error('DiffView or dependencies not loaded');
                this.showError('Diff module not loaded properly');
                return;
            }
            
            // Initialize sync scroll
            const leftPanel = document.getElementById('diffContentLeft');
            const rightPanel = document.getElementById('diffContentRight');
            
            if (window.SyncScroll) {
                window.SyncScroll.initialize(leftPanel, rightPanel);
                const syncScrollBtn = document.getElementById('diffSyncScroll');
                if (syncScrollBtn) syncScrollBtn.classList.add('active');
            }
            
            // Setup diff mode buttons (only once)
            if (!this._diffButtonsSetup) {
                this.setupDiffModeButtons();
                this._diffButtonsSetup = true;
            }
            
            this.showNotification(`Comparing: ${this.currentFile.name} ↔ ${data.file_b.name}`, 'info');
            
        } catch (e) {
            console.error('Comparison failed:', e);
            this.showNotification(`Failed to compare files: ${e.message}`, 'error');
            this.isDiffMode = false;
        }
    }
});
