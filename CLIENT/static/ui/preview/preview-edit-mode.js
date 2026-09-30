/**
 * preview/preview-edit-mode.js - full-width edit mode with line numbers
 *
 * Split from static/ui/preview.js (2026-09-18). Phase 1: verbatim method cut - no behavior change.
 * Entry point: preview-manager.js (loaded as <script type="module">).
 */

import { PreviewManager } from './preview-class.js';

Object.assign(PreviewManager.prototype, {
    /**
     * Toggle edit mode for text files - FULL WIDTH with line numbers preserved
     */
    toggleEdit() {
        if (this.isEditing) return;
        
        this.isEditing = true;
        const display = document.getElementById('previewTextDisplay');
        
        if (!display || !this.previewContent) return;
        
        const editBtn = document.getElementById('previewEditBtn');
        const saveBtn = document.getElementById('previewSaveBtn');
        const cancelBtn = document.getElementById('previewCancelBtn');
        
        if (editBtn) editBtn.style.display = 'none';
        if (saveBtn) saveBtn.style.display = 'flex';
        if (cancelBtn) cancelBtn.style.display = 'flex';
        
        // Get current scroll position
        const scrollTop = this.previewContent.scrollTop;
        
        // Get the current line count and content
        const lines = this.currentContent.split('\n');
        const lineCount = lines.length;
        
        // Build line numbers HTML
        let lineNumbersHtml = '';
        for (let i = 1; i <= lineCount; i++) {
            lineNumbersHtml += `<span>${i}</span>`;
        }
        
        // Get current schema for editor styling
        const schema = window.SchemaManager ? window.SchemaManager.getCurrentSchema() : null;
        const bgColor = schema ? schema.background : '#0d0d0d';
        const textColor = schema ? schema.text : '#ffffff';
        
        // Replace with full-width editor that preserves layout
        this.previewContent.innerHTML = `
            <div class="preview-with-line-numbers" style="height: 100%; width: 100%; display: flex;">
                <div class="line-numbers" style="background: var(--bg-secondary); flex-shrink: 0; overflow-y: auto;">${lineNumbersHtml}</div>
                <textarea class="preview-text-editor-full" id="previewTextEditor" 
                          style="flex: 1; width: 100%; min-height: 100%; resize: none; 
                                 font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
                                 font-size: ${this.fontSize}px;
                                 line-height: 1.5;
                                 background: ${bgColor};
                                 color: ${textColor};
                                 border: none;
                                 outline: none;
                                 padding: 0 12px;
                                 margin: 0;
                                 white-space: pre-wrap;
                                 word-wrap: break-word;
                                 overflow-y: auto;">${this.escapeHtml(this.currentContent)}</textarea>
            </div>
        `;
        
        const editor = document.getElementById('previewTextEditor');
        if (editor) {
            editor.focus();
            // Restore scroll position
            setTimeout(() => {
                editor.scrollTop = scrollTop;
            }, 0);
            
            // Sync line numbers as user scrolls
            editor.addEventListener('scroll', () => {
                const lineNumbersDiv = this.previewContent.querySelector('.line-numbers');
                if (lineNumbersDiv) {
                    lineNumbersDiv.scrollTop = editor.scrollTop;
                }
            });
            
            // Update line numbers when content changes
            editor.addEventListener('input', () => {
                const newLines = editor.value.split('\n').length;
                const lineNumbersDiv = this.previewContent.querySelector('.line-numbers');
                if (lineNumbersDiv) {
                    let newHtml = '';
                    for (let i = 1; i <= newLines; i++) {
                        newHtml += `<span>${i}</span>`;
                    }
                    lineNumbersDiv.innerHTML = newHtml;
                }
            });
        }
    },
    
    /**
     * Cancel edit mode
     */
    cancelEdit() {
        this.isEditing = false;
        
        const editBtn = document.getElementById('previewEditBtn');
        const saveBtn = document.getElementById('previewSaveBtn');
        const cancelBtn = document.getElementById('previewCancelBtn');
        
        if (editBtn) editBtn.style.display = 'flex';
        if (saveBtn) saveBtn.style.display = 'none';
        if (cancelBtn) cancelBtn.style.display = 'none';
        
        // Get current scroll position before rendering
        const scrollTop = this.previewContent.scrollTop;
        
        this.renderPreview(this.currentContent, this.currentFile.name);
        
        // Restore scroll position after render
        setTimeout(() => {
            this.previewContent.scrollTop = scrollTop;
        }, 0);
    },
    
    /**
     * Save edited file content
     */
    async saveEdit() {
        const editor = document.getElementById('previewTextEditor');
        if (!editor) return;
        
        const newContent = editor.value;
        
        try {
            const response = await fetch('/api/file-content', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    path: this.currentFile.path,
                    content: newContent
                })
            });
            
            if (!response.ok) throw new Error('Save failed');
            
            this.currentContent = newContent;
            this.originalContent = newContent;
            
            // Get scroll position before closing edit mode
            const scrollTop = this.previewContent.scrollTop;
            
            this.isEditing = false;
            
            const editBtn = document.getElementById('previewEditBtn');
            const saveBtn = document.getElementById('previewSaveBtn');
            const cancelBtn = document.getElementById('previewCancelBtn');
            
            if (editBtn) editBtn.style.display = 'flex';
            if (saveBtn) saveBtn.style.display = 'none';
            if (cancelBtn) cancelBtn.style.display = 'none';
            
            this.renderPreview(this.currentContent, this.currentFile.name);
            
            // Restore scroll position
            setTimeout(() => {
                this.previewContent.scrollTop = scrollTop;
            }, 0);
            
            this.showNotification('File saved successfully', 'success');
            
            // Refresh file tree to show updated file
            if (typeof loadTree === 'function') {
                loadTree(currentTreePath);
            }
            
        } catch (e) {
            this.showNotification('Failed to save file', 'error');
        }
    }
});
