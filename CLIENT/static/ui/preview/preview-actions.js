/**
 * preview/preview-actions.js - download / run-python / close
 *
 * Split from static/ui/preview.js (2026-09-18). Phase 1: verbatim method cut - no behavior change.
 * Entry point of the group: preview-manager.js (loaded as <script type="module"> in index.html).
 */
import { PreviewManager } from './preview-class.js';
import { buildAuthedUrl } from './preview-utils.js';

Object.assign(PreviewManager.prototype, {
    /**
     * Download current file
     */
    download() {
        if (this.currentFile && typeof window.downloadSingle === 'function') {
            window.downloadSingle(this.currentFile.path);
        } else if (this.currentFile) {
            const a = document.createElement('a');
                // SECURITY (2026-08-25): /files/ is no longer public - use the
                // authenticated download endpoint with key/email query params.
                const dlUrl = buildAuthedUrl(`/api/download?path=${encodeURIComponent(this.currentFile.path)}`);
                a.href = dlUrl;
            a.download = '';
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
        }
    },
    
    /**
     * Run current Python file
     */
    async runPythonFile() {
        if (!this.currentFile) return;
        
        const filename = this.currentFile.name;
        const path = this.currentFile.path;
        
        // Show loading in preview
        const contentEl = document.getElementById('previewContent');
        if (contentEl) {
            contentEl.innerHTML = '<div class="preview-empty"><div class="loading-spinner"></div><p>Running ' + this.escapeHtml(filename) + '...</p></div>';
        }
        
        try {
            const response = await fetch('/api/run-file', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ path: path })
            });
            
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            
            const data = await response.json();
            
            // Show output
            const hasOutput = data.stdout && data.stdout.trim().length > 0;
            const hasErrors = data.stderr && data.stderr.trim().length > 0;
            
            let statusClass = data.returncode === 0 ? 'status-success' : 'status-error';
            let statusText = data.returncode === 0 ? '✓ Completed' : `✗ Exit code: ${data.returncode}`;
            if (data.status === 'timeout') {
                statusClass = 'status-error';
                statusText = '⏱ Timed out';
            }
            
            let html = '<div class="python-output-container">';
            html += `<div class="python-output-status ${statusClass}">${statusText}</div>`;
            
            if (hasOutput || hasErrors) {
                html += '<div class="python-output-content">';
                if (hasOutput) {
                    html += '<div class="python-output-section"><div class="python-output-label">STDOUT</div><pre class="python-output-text">' + this.escapeHtml(data.stdout) + '</pre></div>';
                }
                if (hasErrors) {
                    html += '<div class="python-output-section python-output-error"><div class="python-output-label">STDERR</div><pre class="python-output-text">' + this.escapeHtml(data.stderr) + '</pre></div>';
                }
                html += '</div>';
            } else {
                html += '<div class="python-output-empty">No output</div>';
            }
            html += '</div>';
            
            if (contentEl) contentEl.innerHTML = html;
            
        } catch (e) {
            if (contentEl) contentEl.innerHTML = '<div class="preview-empty"><p style="color:var(--danger);">Failed to run: ' + this.escapeHtml(e.message) + '</p></div>';
            this.showNotification('Failed to run Python file', 'error');
        }
    },
    
    /**
     * Close preview panel
     */
    close() {
        // BUG FIX (2026-08-27): delegate to the global closePreview() - the single
        // owner of closing the panel (it also resets this manager's state and clears
        // all diff UI). Fallback below only runs if file-handler.js failed to load.
        if (typeof window.closePreview === 'function') {
            window.closePreview();
            return;
        }

        // mark closing BEFORE touching the DOM so any stale async loadFile() that
        // resolves after this point is dropped by its guards.
        this._closing = true;
        if (this.previewPanel) {
            this.previewPanel.classList.add('collapsed');
            this.previewPanel.style.width = '';
        }
        
        this.currentFile = null;
        this.currentContent = null;
        this.currentHighlighter = null;
        this.isDiffMode = false;
        this.isEditing = false;
        
        if (this.previewContent) {
            this.previewContent.innerHTML = `
                <div class="preview-empty">
                    <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"></path><circle cx="12" cy="12" r="3"></circle></svg>
                    <p>Click a file to preview</p>
                </div>
            `;
        }
        
        if (this.previewFilename) {
            this.previewFilename.textContent = '';
        }
    }
});
