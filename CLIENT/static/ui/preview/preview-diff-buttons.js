/**
 * preview/preview-diff-buttons.js - diff mode toolbar buttons (mode switch / sync scroll / exit)
 *
 * Split from static/ui/preview.js (2026-09-18). Phase 1: verbatim method cut - no behavior change.
 * Entry point: preview-manager.js (loaded as <script type="module">).
 */

import { PreviewManager } from './preview-class.js';

Object.assign(PreviewManager.prototype, {
    /**
     * Setup diff mode buttons (mode switching, sync scroll, exit)
     */
    setupDiffModeButtons() {
        const sideBySideBtn = document.getElementById('diffModeSideBySide');
        const unifiedBtn = document.getElementById('diffModeUnified');
        const syncScrollBtn = document.getElementById('diffSyncScroll');
        const exitBtn = document.getElementById('diffExitBtn');
        
        if (sideBySideBtn) {
            sideBySideBtn.onclick = () => {
                if (!this.isDiffMode) return;
                
                const leftContainer = document.getElementById('diffContentLeft');
                const rightContainer = document.getElementById('diffContentRight');
                const unifiedContainer = document.getElementById('diffUnifiedContainer');
                const diffContainer = document.getElementById('diffContainer');
                
                if (leftContainer) leftContainer.style.display = 'block';
                if (rightContainer) rightContainer.style.display = 'block';
                if (unifiedContainer) unifiedContainer.style.display = 'none';
                if (diffContainer) diffContainer.style.display = 'flex';
                
                sideBySideBtn.classList.add('active');
                unifiedBtn.classList.remove('active');
                
                if (window.DiffView) {
                    window.DiffView.setMode('side-by-side', leftContainer, rightContainer);
                }
            };
        }
        
        if (unifiedBtn) {
            unifiedBtn.onclick = () => {
                if (!this.isDiffMode) return;
                
                const leftContainer = document.getElementById('diffContentLeft');
                const rightContainer = document.getElementById('diffContentRight');
                const unifiedContainer = document.getElementById('diffUnifiedContainer');
                const diffContainer = document.getElementById('diffContainer');
                
                if (leftContainer) leftContainer.style.display = 'none';
                if (rightContainer) rightContainer.style.display = 'none';
                if (unifiedContainer) unifiedContainer.style.display = 'block';
                if (diffContainer) diffContainer.style.display = 'flex';
                
                unifiedBtn.classList.add('active');
                sideBySideBtn.classList.remove('active');
                
                if (window.DiffView) {
                    window.DiffView.setMode('unified', unifiedContainer, null);
                }
            };
        }
        
        if (syncScrollBtn) {
            syncScrollBtn.onclick = () => {
                const isEnabled = window.SyncScroll ? window.SyncScroll.toggle() : false;
                syncScrollBtn.classList.toggle('active', isEnabled);
                this.showNotification(isEnabled ? 'Sync scroll enabled' : 'Sync scroll disabled', 'info');
            };
        }
        

        
        // Hide/show unchanged lines toggle (2026-08-24: new row-aligned diff view)
        const toggleUnchangedBtn = document.getElementById('diffToggleUnchanged');
        if (toggleUnchangedBtn && window.DiffView) {
            toggleUnchangedBtn.onclick = () => {
                window.DiffView.toggleShowUnchanged();
            };
        }

        // BUG FIX (2026-08-27): #diffExitBtn is ALREADY bound by DiffUI.init() - it
        // clones the button and adds a click listener -> DiffUI.exit(), which now
        // delegates to PreviewManager.exitDiffMode() when we own the session.
        // Binding it here too caused a DOUBLE exit per click (duplicate cleanup, duplicate
        // toast) and fed the stale-async-load race that re-opened the panel after Close.
        // Only bind as a fallback when DiffUI failed to load.
        if (exitBtn && !(window.DiffUI && window.DiffUI.initialized)) {
            exitBtn.onclick = () => this.exitDiffMode();
        }
        
        if (sideBySideBtn) sideBySideBtn.classList.add('active');
    },
    
    /**
     * Exit diff mode and return to single preview
     */
    exitDiffMode() {
        this.isDiffMode = false;
        
        const previewContent = document.getElementById('previewContent');
        const diffContainer = document.getElementById('diffContainer');
        const diffToolbar = document.getElementById('diffToolbar');
        const diffUnified = document.getElementById('diffUnifiedContainer');
        const diffLeft = document.getElementById('diffContentLeft');
        const diffRight = document.getElementById('diffContentRight');
        
        if (previewContent) previewContent.style.display = 'block';
        if (diffContainer) diffContainer.style.display = 'none';
        if (diffUnified) diffUnified.style.display = 'none';
        if (diffToolbar) diffToolbar.style.display = 'none';
        
        // Clear diff containers
        if (diffLeft) diffLeft.innerHTML = '';
        if (diffRight) diffRight.innerHTML = '';
        
        // Clear stats
        const statsContainer = document.getElementById('diffStats');
        if (statsContainer) statsContainer.innerHTML = '';
        
        if (window.SyncScroll) {
            window.SyncScroll.destroy();
        }
        
        if (window.DiffView) {
            window.DiffView.clear();
        }
        
        // Restore original preview
        if (this.currentFile) {
            this.renderPreview(this.currentContent, this.currentFile.name);
        }
        
        this.showNotification('Exited comparison mode', 'info');
    }
});
