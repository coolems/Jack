/**
 * preview/preview-image-meta.js - rich metadata card under previewed images
 *
 * ADDED (2026-09-18): when an image is previewed, fetch /api/file-metadata for
 * it and render a compact card below the <img>: file facts (size on disk,
 * modified), image facts (format, resolution) and EXIF camera data. If the
 * photo carries GPS coordinates in its EXIF, a "View on Google Maps" link is
 * shown too. Purely additive - any failure just leaves the placeholder row.
 */

import { PreviewManager } from './preview-class.js';

Object.assign(PreviewManager.prototype, {
    /**
     * Load and render the metadata card for a previewed image.
     * Non-blocking: called right after the <img> is rendered; a slow or failed
     * request never touches the already-visible picture.
     * @param {string} filePath - Relative path of the file inside working_root
     * @param {string} filename - Display name (for the header row)
     */
    async loadImageMetadata(filePath, filename) {
        const container = document.getElementById('previewImageMeta');
        if (!container || !filePath) return;

        // Stale-guard: a newer file may have been opened while this fetch ran.
        const seq = this._loadSeq;
        container.innerHTML = '<div class="img-meta-loading">Loading metadata…</div>';

        try {
            // SECURITY fix 2026-10-05: plain URL - auth travels in HEADERS via the global
            // fetch override (static/ui/api-key.js), never in the query string.
            const url = `/api/file-metadata?path=${encodeURIComponent(filePath)}`;
            const response = await fetch(url);
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            const meta = await response.json();

            // The user opened another file in the meantime - drop this result.
            if (seq !== this._loadSeq) return;

            container.innerHTML = this.renderImageMetaCard(meta, filename);
        } catch (e) {
            console.warn('[PreviewManager] image metadata failed:', e.message || e);
            if (seq !== this._loadSeq) return;
            container.innerHTML = '<div class="img-meta-loading">Metadata unavailable</div>';
        }
    },

    /**
     * Build the HTML for the metadata card from an /api/file-metadata payload.
     * @param {object} meta - Response body of GET /api/file-metadata
     * @param {string} filename - Display name
     */
    renderImageMetaCard(meta, filename) {
        const esc = this.escapeHtml;
        const rows = [];

        // ---- File facts (always present for any file type) -------------------
        const fRows = [];
        if (meta.file && meta.file.size_human) {
            fRows.push(`<span title="${esc(meta.file.size_bytes + ' bytes')}">💾 ${esc(meta.file.size_human)} on disk</span>`);
        }
        if (meta.file && meta.file.modified) {
            fRows.push(`<span>🕒 Modified: ${esc(meta.file.modified)}</span>`);
        }

        // ---- Image facts ------------------------------------------------------
        const iRows = [];
        if (meta.image && meta.image.width && meta.image.height) {
            iRows.push(`<span title="width × height">📐 ${meta.image.width} × ${meta.image.height} px</span>`);
        }
        if (meta.image && meta.image.format) {
            iRows.push(`<span>🖼️ Format: ${esc(meta.image.format)}</span>`);
        }

        // ---- EXIF camera/scene data -------------------------------------------
        const eRows = [];
        for (const item of (meta.exif || [])) {
            eRows.push(`<span>${esc(item.tag)}: <b class="img-meta-value">${esc(item.value)}</b></span>`);
        }

        // ---- GPS / Google Maps --------------------------------------------------
        let gpsRow = '';
        const g = meta.gps;
        if (g && g.lat !== null && g.lng !== null) {
            const coords = `${g.lat.toFixed(5)}, ${g.lng.toFixed(5)}`;
            gpsRow = `<a class="img-meta-gmaps" href="${esc(g.google_maps_url)}" target="_blank" rel="noopener noreferrer" title="Open in Google Maps">📍 View on Google Maps (${esc(coords)})</a>`;
        }

        const sections = [];
        if (fRows.length) sections.push(`<div class="img-meta-row">${fRows.join('')}</div>`);
        if (iRows.length) sections.push(`<div class="img-meta-row">${iRows.join('')}</div>`);
        if (eRows.length) sections.push(`<div class="img-meta-row img-meta-exif">${eRows.join('')}</div>`);

        // No EXIF at all? Say so explicitly instead of showing an empty card.
        const hasExif = eRows.length > 0 || gpsRow;
        if (!hasExif && (meta.exif || []).length === 0) {
            sections.push(`<div class="img-meta-row"><span title="This file carries no EXIF metadata">ℹ️ No EXIF data in this file</span></div>`);
        }

        const errNote = meta.error ? `<div class="img-meta-error" title="${esc(meta.error)}">⚠ ${esc(meta.error)}</div>` : '';

        return `
            <div class="preview-image-resolution img-meta-card">
                <span>📐 Image: ${esc(filename)}</span>
                ${sections.join('')}
                ${gpsRow}
                ${errNote}
            </div>
        `;
    }
});
