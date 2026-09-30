/**
 * preview/preview-schema.js - schema/theme selector wiring
 *
 * Split from static/ui/preview.js (2026-09-18). Phase 1: verbatim method cut - no behavior change.
 * Entry point: preview-manager.js (loaded as <script type="module">).
 */

import { PreviewManager } from './preview-class.js';

Object.assign(PreviewManager.prototype, {
    setupSchemaSelector() {
        const selector = document.getElementById('schemaSelector');
        if (!selector) return;
        
        const checkSchemaManager = setInterval(() => {
            if (window.SchemaManager) {
                clearInterval(checkSchemaManager);
                this.populateSchemaSelector(selector);
            }
        }, 100);
        
        setTimeout(() => clearInterval(checkSchemaManager), 5000);
    },
    
    populateSchemaSelector(selector) {
        const schemas = window.SchemaManager.getSchemaInfo();
        
        selector.innerHTML = schemas.map(schema => 
            `<option value="${schema.id}" ${schema.id === window.SchemaManager.currentSchema ? 'selected' : ''}>
                🎨 ${schema.name}
            </option>`
        ).join('');
        
        selector.onchange = (e) => {
            window.SchemaManager.setSchema(e.target.value);
            this.reapplyHighlighting();
            
            // Reapply highlighting to diff view if in diff mode
            if (this.isDiffMode && window.DiffView) {
                const leftContainer = document.getElementById('diffContentLeft');
                const rightContainer = document.getElementById('diffContentRight');
                if (leftContainer && rightContainer && window.DiffView.currentMode === 'side-by-side') {
                    window.DiffView.reapplyHighlighting(leftContainer, rightContainer);
                }
            }
            
            this.showNotification(`Schema changed to ${e.target.options[e.target.selectedIndex].text}`, 'info');
        };
        
        selector.style.display = 'inline-block';
        
        window.SchemaManager.addListener(() => {
            this.reapplyHighlighting();
        });
    }
});
