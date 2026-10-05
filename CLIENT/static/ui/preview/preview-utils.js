/**
 * preview/preview-utils.js - shared helpers for PreviewManager
 *
 * Split from static/ui/preview.js (2026-09-18). Phase 1: verbatim method cut - no behavior change.
 * Entry point: preview-manager.js (loaded as <script type="module">).
 */

import { PreviewManager } from './preview-class.js';

/**
 * Mint a short-lived media token for ONE exact file path (SECURITY fix 2026-10-05).
 *
 * The POST goes through the global fetch override (static/ui/api-key.js), which injects
 * X-API-Key / X-User-Email - so proving identity happens in TLS-protected HEADERS. The
 * returned token is single-use-ish (3 uses), expires in 60 s and is bound to that exact
 * path; the server rejects it against any other file.
 * @param {string} path - working_root-relative file path
 * @returns {Promise<string>} opaque ?t= token
 */
async function getMediaToken(path) {
    const r = await fetch('/api/auth/media-token', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path })
    });
    if (!r.ok) throw new Error('Failed to obtain media token (HTTP ' + r.status + ')');
    const data = await r.json();
    return data.token;
}

/**
 * Build an authenticated /api/download URL using a short-lived media token.
 * <img>/<iframe> can't send headers, so instead of shipping the master key as
 * ?api_key=...&email=... (which leaked it into browser history, Referer headers and
 * logging hops), we mint a path-bound 60 s token via header auth and pass ?t=<token>.
 * SECURITY fix 2026-10-05: the master key no longer appears in ANY URL.
 * @param {string} baseUrl - e.g. `/api/download?path=...&inline=true` (must already contain '?')
 * @returns {Promise<string>} URL with ?t=<token> appended
 */
async function buildAuthedUrl(baseUrl) {
    const m = baseUrl.match(/[?&]path=([^&]*)/);
    if (!m) throw new Error('buildAuthedUrl: baseUrl must carry a path= query param');
    const token = await getMediaToken(decodeURIComponent(m[1]));
    return baseUrl + '&t=' + encodeURIComponent(token);
}

// Expose for the classic (non-module) scripts in static/ui/files/* - they cannot import.
if (typeof window !== 'undefined') {
    window.getMediaToken = getMediaToken;
}

Object.assign(PreviewManager.prototype, {
        /**
     * Escape HTML special characters
     * @param {string} text - Text to escape
     * @returns {string} Escaped HTML
     */
    escapeHtml(text) {
        if (!text) return '';
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    },
    
    /**
     * Format file size
     * @param {number} bytes - File size in bytes
     * @returns {string} Formatted size
     */
    formatFileSize(bytes) {
        if (bytes === 0) return '0 B';
        const k = 1024;
        const sizes = ['B', 'KB', 'MB', 'GB'];
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
    },
    
    /**
     * Show notification
     * @param {string} message - Message to show
     * @param {string} type - Notification type: success/error/info/warning
     */
    showNotification(message, type = 'info') {
        if (typeof window.showNotification === 'function') {
            window.showNotification(message, type);
        } else {
            console.log(`[PreviewManager] ${type}: ${message}`);
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
});

export { buildAuthedUrl, getMediaToken };
