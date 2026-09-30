/**
 * preview/preview-fontsize.js - font size controls + localStorage persistence
 *
 * Split from static/ui/preview.js (2026-09-18). Phase 1: verbatim method cut - no behavior change.
 * Entry point: preview-manager.js (loaded as <script type="module">).
 */

import { PreviewManager } from './preview-class.js';

Object.assign(PreviewManager.prototype, {
    setupFontSizeControls() {
        const increaseBtn = document.getElementById('previewFontIncrease');
        const decreaseBtn = document.getElementById('previewFontDecrease');
        const resetBtn = document.getElementById('previewFontReset');
        const fontSizeDisplay = document.getElementById('previewFontSize');
        
        if (increaseBtn) {
            increaseBtn.onclick = () => {
                if (this.fontSize < 32) {
                    this.fontSize += 2;
                    this.updateFontSize();
                    if (fontSizeDisplay) fontSizeDisplay.textContent = `${this.fontSize}px`;
                }
            };
        }
        
        if (decreaseBtn) {
            decreaseBtn.onclick = () => {
                if (this.fontSize > 8) {
                    this.fontSize -= 2;
                    this.updateFontSize();
                    if (fontSizeDisplay) fontSizeDisplay.textContent = `${this.fontSize}px`;
                }
            };
        }
        
        if (resetBtn) {
            resetBtn.onclick = () => {
                this.fontSize = 14;
                this.updateFontSize();
                if (fontSizeDisplay) fontSizeDisplay.textContent = `${this.fontSize}px`;
            };
        }
    },
    
    restoreFontSize() {
        const saved = localStorage.getItem('coolems_preview_font_size');
        if (saved) {
            this.fontSize = parseInt(saved, 10);
            if (isNaN(this.fontSize)) this.fontSize = 14;
            this.updateFontSize();
            const fontSizeDisplay = document.getElementById('previewFontSize');
            if (fontSizeDisplay) fontSizeDisplay.textContent = `${this.fontSize}px`;
        }
    },
    
    updateFontSize() {
        const previewContent = document.getElementById('previewContent');
        const diffLeft = document.getElementById('diffContentLeft');
        const diffRight = document.getElementById('diffContentRight');
        const diffUnified = document.getElementById('diffUnifiedContainer');
        
        if (previewContent) {
            previewContent.style.fontSize = `${this.fontSize}px`;

            // BUG FIX (2026-09-18): the text preview renders as
            // #previewContent > .preview-with-line-numbers, and that wrapper's CSS rule pins
            // font-size: 13px - so the inline size set on #previewContent alone never reached
            // the visible text (A-/A+ looked dead). Apply the size to the active wrapper too;
            // clearing its line-height keeps gutter (.line-numbers span, 1.5) and text in sync.
            previewContent.querySelectorAll('.preview-with-line-numbers').forEach(wrapper => {
                wrapper.style.fontSize = `${this.fontSize}px`;
                wrapper.style.lineHeight = '';
            });
        }
        if (diffLeft) {
            diffLeft.style.fontSize = `${this.fontSize}px`;
        }
        if (diffRight) {
            diffRight.style.fontSize = `${this.fontSize}px`;
        }
        if (diffUnified) {
            diffUnified.style.fontSize = `${this.fontSize}px`;
        }
        
        localStorage.setItem('coolems_preview_font_size', this.fontSize);
    }
});
