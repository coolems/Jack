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

        // Get current scroll position (view mode: #previewContent is the scroller).
        const scrollTop = this.previewContent.scrollTop;

        // Get the current line count and content.
        // SYNC CONTRACT (2026-07-15): one gutter number per LOGICAL line, and the textarea
        // uses white-space: pre (see 02-tree-preview-editor.css) so a logical line is always
        // exactly one visual row -> the numbers can never drift from their lines.
        const lines = this.currentContent.split('\n');
        const lineCount = lines.length;

        // Build line numbers HTML.
        let lineNumbersHtml = '';
        for (let i = 1; i <= lineCount; i++) {
            lineNumbersHtml += `<span>${i}</span>`;
        }

        // Get current schema for editor styling.
        const schema = window.SchemaManager ? window.SchemaManager.getCurrentSchema() : null;
        const bgColor = schema ? schema.background : '#0d0d0d';
        const textColor = schema ? schema.text : '#ffffff';

        // Replace with full-width editor that preserves layout.
        // The row carries .preview-edit-mode, which pins it to panel height and hides its own
        // overflow - so the TEXTAREA is the single vertical scroller and .line-numbers is a
        // passive mirror whose scrollTop we copy from the textarea (syncGutter below).
        // Layout lives in CSS; only the dynamic values are inline here.
        this.previewContent.innerHTML = `
            <div class="preview-with-line-numbers preview-edit-mode">
                <div class="line-numbers">${lineNumbersHtml}</div>
                <textarea class="preview-text-editor-full" id="previewTextEditor"
                          style="font-size: ${this.fontSize}px; background: ${bgColor}; color: ${textColor};">${this.escapeHtml(this.currentContent)}</textarea>
            </div>
        `;

        const editor = document.getElementById('previewTextEditor');
        if (editor) {
            // Mirror the textarea's scrollTop onto the gutter.
            const syncGutter = () => {
                const lineNumbersDiv = this.previewContent.querySelector('.line-numbers');
                if (lineNumbersDiv) {
                    lineNumbersDiv.scrollTop = editor.scrollTop;
                }
            };

            editor.focus();
            // Restore scroll position, then re-sync the gutter immediately so it is aligned
            // before any user interaction.
            setTimeout(() => {
                editor.scrollTop = scrollTop;
                syncGutter();
            }, 0);

            // Sync line numbers as user scrolls (passive - never blocks scrolling).
            editor.addEventListener('scroll', syncGutter, { passive: true });

            // Update line numbers when content changes (count must always match the file).
            editor.addEventListener('input', () => {
                const newLines = editor.value.split('\n').length;
                const lineNumbersDiv = this.previewContent.querySelector('.line-numbers');
                if (!lineNumbersDiv) return;

                let newHtml = '';
                for (let i = 1; i <= newLines; i++) {
                    newHtml += `<span>${i}</span>`;
                }
                lineNumbersDiv.innerHTML = newHtml;
                // Rebuilding the gutter can reset its scrollTop - re-mirror it.
                syncGutter();
            });
        }
    },

    /**
     * Cancel edit mode
     */
    async cancelEdit() {  // 2026-10-03: async - renderPreview mints a media token
        this.isEditing = false;

        const editBtn = document.getElementById('previewEditBtn');
        const saveBtn = document.getElementById('previewSaveBtn');
        const cancelBtn = document.getElementById('previewCancelBtn');

        if (editBtn) editBtn.style.display = 'flex';
        if (saveBtn) saveBtn.style.display = 'none';
        if (cancelBtn) cancelBtn.style.display = 'none';

        // Get current scroll position before rendering. In edit mode the TEXTAREA is the
        // scroller (#previewContent does not scroll - the row is height:100% overflow:hidden),
        // so read from the textarea when present.
        const editor = document.getElementById('previewTextEditor');
        const scrollTop = editor ? editor.scrollTop : (this.previewContent ? this.previewContent.scrollTop : 0);

        await this.renderPreview(this.currentContent, this.currentFile.name);  // 2026-10-03: async now

        // Restore scroll position after render.
        setTimeout(() => {
            if (this.previewContent) this.previewContent.scrollTop = scrollTop;
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

            // Get scroll position before closing edit mode (textarea is the scroller here).
            const scrollTop = editor.scrollTop;

            this.isEditing = false;

            const editBtn = document.getElementById('previewEditBtn');
            const saveBtn = document.getElementById('previewSaveBtn');
            const cancelBtn = document.getElementById('previewCancelBtn');

            if (editBtn) editBtn.style.display = 'flex';
            if (saveBtn) saveBtn.style.display = 'none';
            if (cancelBtn) cancelBtn.style.display = 'none';

            await this.renderPreview(this.currentContent, this.currentFile.name);  // 2026-10-03: async now

            // Restore scroll position.
            setTimeout(() => {
                if (this.previewContent) this.previewContent.scrollTop = scrollTop;
            }, 0);

            this.showNotification('File saved successfully', 'success');

            // Refresh file tree to show updated file.
            if (typeof loadTree === 'function') {
                loadTree(currentTreePath);
            }

        } catch (e) {
            this.showNotification('Failed to save file', 'error');
        }
    }
});
