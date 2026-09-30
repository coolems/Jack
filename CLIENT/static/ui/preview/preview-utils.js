/**
 * preview/preview-utils.js - shared helpers for PreviewManager
 *
 * Split from static/ui/preview.js (2026-09-18). Phase 1: verbatim method cut - no behavior change.
 * Entry point: preview-manager.js (loaded as <script type="module">).
 */

import { PreviewManager } from './preview-class.js';

/**
 * Build an authenticated /api/download URL.
 * <img>/<iframe> can't send headers, so key/email travel as query params;
 * still validated server-side (SECURITY 2026-08-25: /files/ is no longer public).
 * @param {string} baseUrl - e.g. `/api/download?path=...&inline=true`
 * @returns {string} URL with api_key/email appended when present in localStorage
 */
function buildAuthedUrl(baseUrl) {
    const apiKey = localStorage.getItem('coolems_api_key') || '';
    const userEmail = localStorage.getItem('coolems_email') || '';
    let url = baseUrl;
    if (apiKey) url += '&api_key=' + encodeURIComponent(apiKey);
    if (userEmail) url += '&email=' + encodeURIComponent(userEmail);
    return url;
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
     * @param {number} bytes - Size in bytes
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
     * @param {string} message - Notification message
     * @param {string} type - 'success', 'error', 'info', 'warning'
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

export { buildAuthedUrl };
