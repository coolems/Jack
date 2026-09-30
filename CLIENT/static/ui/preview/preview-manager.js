/**
 * preview/preview-manager.js - entry point (singleton + bootstrap)
 *
 * Split from static/ui/preview.js (2026-09-18). Phase 1: verbatim method cut - no behavior change.
 * Entry point of the preview module group, loaded as <script type="module"> in index.html.
 */

import { PreviewManager } from './preview-class.js';

// Feature modules extend PreviewManager.prototype on import (order-independent).
import './preview-fontsize.js';
import './preview-schema.js';
import './preview-load.js';
import './preview-render.js';
import './preview-image-meta.js';  // ADDED (2026-09-18): EXIF/GPS/size card under images
import './preview-edit-mode.js';
import './preview-compare.js';
import './preview-diff-buttons.js';
import './preview-actions.js';
import './preview-utils.js';

// Create singleton instance (kept as window global - consumers: app.js, index.html init IIFE,
// diff/diff_ui.js, files/preview-core.js all check `window.PreviewManager`)
const previewManagerInstance = new PreviewManager();
if (typeof window !== 'undefined') {
    window.PreviewManager = previewManagerInstance;
}

// Auto-initialize on DOMContentLoaded (module scripts run deferred: after classic scripts,
// before the event fires - same effective timing as the original classic script).
document.addEventListener('DOMContentLoaded', () => {
    if (window.PreviewManager && typeof window.PreviewManager.init === 'function') {
        window.PreviewManager.init();
        console.log('[PreviewManager] Auto-initialized on DOMContentLoaded');
    }
});

export default previewManagerInstance;
