/**
 * diff/sync_scroll.js - Synchronized Scrolling
 * Keeps two diff panels scrolled to the same relative position
 * 
 * Features:
 * - Proper toggle with visual feedback
 * - Debounced scroll events to prevent loops
 * - Programmatic scroll detection
 * - Smooth scroll ratio calculation
 * - Event listener cleanup
 */

class SyncScroll {
    constructor() {
        this.panelLeft = null;
        this.panelRight = null;
        this.isSyncing = false;           // Prevents recursive scroll events
        this.isEnabled = true;             // User preference for sync scroll
        this.programmaticScroll = false;   // NEW: prevents event loops during programmatic scroll
        this.scrollRatio = 1;              // Scroll height ratio between panels
        this.scrollTimeout = null;         // NEW: debounce timeout
        this.resizeObserver = null;        // NEW: observe panel size changes
        this.listeners = [];               // Scroll position listeners
        this.initialized = false;          // Track initialization state
    }
    
    /**
     * Initialize sync scroll between two panels
     * @param {HTMLElement} leftPanel - Left diff content panel
     * @param {HTMLElement} rightPanel - Right diff content panel
     */
    initialize(leftPanel, rightPanel) {
        if (this.initialized) {
            this.destroy(); // Clean up existing before re-initializing
        }
        
        this.panelLeft = leftPanel;
        this.panelRight = rightPanel;
        
        if (!this.panelLeft || !this.panelRight) {
            console.warn('[SyncScroll] Cannot initialize - panels not found');
            return;
        }
        
        this.attachScrollListeners();
        this.calculateScrollRatio();
        this.setupResizeObserver();
        this.updateButtonState();
        
        this.initialized = true;
        console.log('[SyncScroll] Initialized, enabled:', this.isEnabled);
    }
    
    /**
     * Attach scroll event listeners to both panels with proper cleanup
     */
    attachScrollListeners() {
        // Use bound methods so we can remove them later
        this._boundSyncLeftToRight = this.syncScrollLeftToRight.bind(this);
        this._boundSyncRightToLeft = this.syncScrollRightToLeft.bind(this);
        
        this.panelLeft.addEventListener('scroll', this._boundSyncLeftToRight);
        this.panelRight.addEventListener('scroll', this._boundSyncRightToLeft);
    }
    
    /**
     * Sync left panel scroll to right panel (with debounce)
     */
    syncScrollLeftToRight() {
        // Don't sync if disabled, already syncing, or programmatic scroll
        if (!this.isEnabled || this.isSyncing || this.programmaticScroll) {
            return;
        }
        
        // Debounce: clear previous timeout
        if (this.scrollTimeout) {
            clearTimeout(this.scrollTimeout);
        }
        
        this.scrollTimeout = setTimeout(() => {
            this.performSyncLeftToRight();
            this.scrollTimeout = null;
        }, 10);
    }
    
    /**
     * Perform actual left-to-right scroll sync
     */
    performSyncLeftToRight() {
        if (!this.isEnabled || !this.panelLeft || !this.panelRight) {
            return;
        }
        
        this.isSyncing = true;
        
        const leftScrollHeight = this.panelLeft.scrollHeight - this.panelLeft.clientHeight;
        if (leftScrollHeight <= 0) {
            this.isSyncing = false;
            return;
        }
        
        const leftScrollPercent = this.panelLeft.scrollTop / leftScrollHeight;
        
        // Calculate right scroll position based on ratio
        const rightScrollHeight = this.panelRight.scrollHeight - this.panelRight.clientHeight;
        if (rightScrollHeight > 0) {
            const targetScrollTop = leftScrollPercent * rightScrollHeight;
            
            // Mark as programmatic to prevent recursive sync
            this.programmaticScroll = true;
            this.panelRight.scrollTop = targetScrollTop;
            this.programmaticScroll = false;
        }
        
        this.isSyncing = false;
        this.notifyListeners(leftScrollPercent);
    }
    
    /**
     * Sync right panel scroll to left panel (with debounce)
     */
    syncScrollRightToLeft() {
        if (!this.isEnabled || this.isSyncing || this.programmaticScroll) {
            return;
        }
        
        if (this.scrollTimeout) {
            clearTimeout(this.scrollTimeout);
        }
        
        this.scrollTimeout = setTimeout(() => {
            this.performSyncRightToLeft();
            this.scrollTimeout = null;
        }, 10);
    }
    
    /**
     * Perform actual right-to-left scroll sync
     */
    performSyncRightToLeft() {
        if (!this.isEnabled || !this.panelLeft || !this.panelRight) {
            return;
        }
        
        this.isSyncing = true;
        
        const rightScrollHeight = this.panelRight.scrollHeight - this.panelRight.clientHeight;
        if (rightScrollHeight <= 0) {
            this.isSyncing = false;
            return;
        }
        
        const rightScrollPercent = this.panelRight.scrollTop / rightScrollHeight;
        
        // Calculate left scroll position based on ratio
        const leftScrollHeight = this.panelLeft.scrollHeight - this.panelLeft.clientHeight;
        if (leftScrollHeight > 0) {
            const targetScrollTop = rightScrollPercent * leftScrollHeight;
            
            this.programmaticScroll = true;
            this.panelLeft.scrollTop = targetScrollTop;
            this.programmaticScroll = false;
        }
        
        this.isSyncing = false;
        this.notifyListeners(rightScrollPercent);
    }
    
    /**
     * Calculate scroll ratio between panels
     * Called when panel sizes change
     */
    calculateScrollRatio() {
        if (!this.panelLeft || !this.panelRight) return;
        
        const leftScrollHeight = this.panelLeft.scrollHeight - this.panelLeft.clientHeight;
        const rightScrollHeight = this.panelRight.scrollHeight - this.panelRight.clientHeight;
        
        if (leftScrollHeight > 0 && rightScrollHeight > 0) {
            this.scrollRatio = rightScrollHeight / leftScrollHeight;
        } else {
            this.scrollRatio = 1;
        }
    }
    
    /**
     * Setup resize observer to recalculate ratio when panel sizes change
     */
    setupResizeObserver() {
        if (typeof ResizeObserver !== 'undefined') {
            this.resizeObserver = new ResizeObserver(() => {
                this.calculateScrollRatio();
            });
            
            if (this.panelLeft) this.resizeObserver.observe(this.panelLeft);
            if (this.panelRight) this.resizeObserver.observe(this.panelRight);
        }
        
        // Fallback: recalculate on window resize
        this._boundRecalculate = this.calculateScrollRatio.bind(this);
        window.addEventListener('resize', this._boundRecalculate);
    }
    
    /**
     * Enable synchronized scrolling
     */
    enable() {
        this.isEnabled = true;
        this.updateButtonState();
        console.log('[SyncScroll] Enabled');
    }
    
    /**
     * Disable synchronized scrolling
     */
    disable() {
        this.isEnabled = false;
        this.updateButtonState();
        console.log('[SyncScroll] Disabled');
    }
    
    /**
     * Toggle synchronized scrolling
     * @returns {boolean} New state
     */
    toggle() {
        this.isEnabled = !this.isEnabled;
        this.updateButtonState();
        console.log('[SyncScroll] Toggled:', this.isEnabled);
        return this.isEnabled;
    }
    
    /**
     * Update the toggle button UI state
     */
    updateButtonState() {
        const btn = document.getElementById('diffSyncScroll');
        if (btn) {
            if (this.isEnabled) {
                btn.classList.add('active');
                btn.title = 'Sync Scroll: ON';
            } else {
                btn.classList.remove('active');
                btn.title = 'Sync Scroll: OFF';
            }
        }
    }
    
    /**
     * Reset scroll positions to top
     */
    reset() {
        this.programmaticScroll = true;
        if (this.panelLeft) this.panelLeft.scrollTop = 0;
        if (this.panelRight) this.panelRight.scrollTop = 0;
        this.programmaticScroll = false;
        this.calculateScrollRatio();
    }
    
    /**
     * Scroll to a specific line in both panels
     * @param {number} lineNumber - Line number (0-based)
     * @param {string} side - 'left', 'right', or 'both'
     */
    scrollToLine(lineNumber, side = 'both') {
        if (!this.panelLeft || !this.panelRight) return;
        
        const lineHeight = 22; // Approximate line height in pixels
        const targetScroll = lineNumber * lineHeight;
        
        this.programmaticScroll = true;
        
        if (side === 'left' || side === 'both') {
            this.panelLeft.scrollTop = targetScroll;
        }
        
        if (side === 'right' || side === 'both') {
            this.panelRight.scrollTop = targetScroll;
        }
        
        this.programmaticScroll = false;
        
        if (side === 'both') {
            this.calculateScrollRatio();
        }
    }
    
    /**
     * Add scroll position listener
     * @param {Function} callback - Called with scrollPercent (0-1)
     */
    addListener(callback) {
        this.listeners.push(callback);
    }
    
    /**
     * Remove scroll position listener
     * @param {Function} callback - The callback to remove
     */
    removeListener(callback) {
        const index = this.listeners.indexOf(callback);
        if (index > -1) this.listeners.splice(index, 1);
    }
    
    /**
     * Notify listeners of scroll position change
     * @param {number} scrollPercent - Current scroll percentage (0-1)
     */
    notifyListeners(scrollPercent) {
        this.listeners.forEach(callback => {
            try {
                callback(scrollPercent);
            } catch (e) {
                console.warn('[SyncScroll] Listener error:', e);
            }
        });
    }
    
    /**
     * Check if sync scroll is currently enabled
     * @returns {boolean}
     */
    isSyncEnabled() {
        return this.isEnabled;
    }
    
    /**
     * Clean up event listeners and observers
     */
    destroy() {
        if (!this.initialized) return;
        
        // Remove scroll listeners
        if (this.panelLeft && this._boundSyncLeftToRight) {
            this.panelLeft.removeEventListener('scroll', this._boundSyncLeftToRight);
        }
        if (this.panelRight && this._boundSyncRightToLeft) {
            this.panelRight.removeEventListener('scroll', this._boundSyncRightToLeft);
        }
        
        // Remove resize observer
        if (this.resizeObserver) {
            this.resizeObserver.disconnect();
            this.resizeObserver = null;
        }
        
        // Remove window resize listener
        if (this._boundRecalculate) {
            window.removeEventListener('resize', this._boundRecalculate);
        }
        
        // Clear timeout
        if (this.scrollTimeout) {
            clearTimeout(this.scrollTimeout);
            this.scrollTimeout = null;
        }
        
        // Clear references
        this.panelLeft = null;
        this.panelRight = null;
        this._boundSyncLeftToRight = null;
        this._boundSyncRightToLeft = null;
        this._boundRecalculate = null;
        this.listeners = [];
        this.initialized = false;
        
        console.log('[SyncScroll] Destroyed');
    }
}

// Create singleton instance
window.SyncScroll = new SyncScroll();