/*
     * files-panel.js - split from file-handler.js on 2026-09-18 (verbatim cut/paste).
     * Part of the CLIENT/static/ui/files/ module group. Load order matters:
     * files-state.js must load first; see index.html script tags.
     */

    
// Files panel toggle + panel resize drag logic.

// ===== HEADER ACTIONS RELOCATION =====
// When the Files panel is open the shrinking .main header would squeeze these
// controls into the stats text, so they are moved into the top of the panel
// (#filesPanelTools) and returned to .header when the panel closes.

function _getHeaderActions() { return document.getElementById('headerActions'); }
function _getFilesPanelTools() { return document.getElementById('filesPanelTools'); }
function _getHeader() { return document.querySelector('.header'); }

function relocateHeaderActionsIntoPanel() {
    const actions = _getHeaderActions();
    const tools = _getFilesPanelTools();
    if (!actions || !tools) return;
    actions.classList.add('in-files-panel');
    tools.appendChild(actions);
}

function relocateHeaderActionsToHeader() {
    const actions = _getHeaderActions();
    const header = _getHeader();
    if (!actions || !header) return;
    actions.classList.remove('in-files-panel');
    header.appendChild(actions);
}

// ===== PANEL TOGGLE =====
function toggleFilesPanel() {

    const panel = document.getElementById('filesPanel');

    const btn = document.getElementById('filesToggleBtn');

    COOLEMS.filesPanelOpen = !COOLEMS.filesPanelOpen;

    panel.classList.toggle('collapsed', !COOLEMS.filesPanelOpen);

    btn.classList.toggle('active', COOLEMS.filesPanelOpen);

    // Clear inline width when collapsing so CSS .collapsed { width: 0 } takes effect

    if (!COOLEMS.filesPanelOpen) {

        panel.style.width = '';

        relocateHeaderActionsToHeader();

    } else {

        relocateHeaderActionsIntoPanel();

    }

    if (COOLEMS.filesPanelOpen) loadTree();

    if (window.innerWidth <= 768) panel.classList.toggle('open', COOLEMS.filesPanelOpen);

}

// ===== PANEL RESIZE DRAG LOGIC =====



(function initPanelResize() {

    const MIN_WIDTH = 150;

    const MAX_WIDTH_RATIO = 0.7; // max 70% of viewport



    // --- Preview Panel Resize ---

    const previewPanel = document.getElementById('previewPanel');

    const previewHandle = document.getElementById('previewResizeHandle');



    if (previewPanel && previewHandle) {

        let isResizingPreview = false;

        let startX = 0;

        let startWidth = 0;



        previewHandle.addEventListener('mousedown', function(e) {

            e.preventDefault();

            e.stopPropagation();

            isResizingPreview = true;

            startX = e.clientX;

            startWidth = previewPanel.offsetWidth;

            previewHandle.classList.add('active');

            document.body.classList.add('resizing-panel');

        });



        document.addEventListener('mousemove', function(e) {

            if (!isResizingPreview) return;

            const delta = startX - e.clientX;

            const newWidth = Math.max(MIN_WIDTH, Math.min(startWidth + delta, window.innerWidth * MAX_WIDTH_RATIO));

            previewPanel.style.width = newWidth + 'px';

            localStorage.setItem('previewPanelWidth', String(newWidth));

        });



        document.addEventListener('mouseup', function() {

            if (!isResizingPreview) return;

            isResizingPreview = false;

            previewHandle.classList.remove('active');

            document.body.classList.remove('resizing-panel');

        });

    }



    // --- Files Panel Resize ---

    const filesPanel = document.getElementById('filesPanel');

    const filesHandle = document.getElementById('filesResizeHandle');



    if (filesPanel && filesHandle) {

        let isResizingFiles = false;

        let startX = 0;

        let startWidth = 0;



        filesHandle.addEventListener('mousedown', function(e) {

            e.preventDefault();

            e.stopPropagation();

            isResizingFiles = true;

            startX = e.clientX;

            startWidth = filesPanel.offsetWidth;

            filesHandle.classList.add('active');

            document.body.classList.add('resizing-panel');

        });



        document.addEventListener('mousemove', function(e) {

            if (!isResizingFiles) return;

            const delta = startX - e.clientX;

            const newWidth = Math.max(MIN_WIDTH, Math.min(startWidth + delta, window.innerWidth * MAX_WIDTH_RATIO));

            filesPanel.style.width = newWidth + 'px';

            localStorage.setItem('filesPanelWidth', String(newWidth));

        });



        document.addEventListener('mouseup', function() {

            if (!isResizingFiles) return;

            isResizingFiles = false;

            filesHandle.classList.remove('active');

            document.body.classList.remove('resizing-panel');

        });

    }



    // --- Restore saved widths on load ---

    function restorePanelWidths() {

        const savedPreview = localStorage.getItem('previewPanelWidth');

        if (savedPreview && previewPanel && !previewPanel.classList.contains('collapsed')) {

            previewPanel.style.width = savedPreview + 'px';

        }



        const savedFiles = localStorage.getItem('filesPanelWidth');

        if (savedFiles && filesPanel && !filesPanel.classList.contains('collapsed')) {

            filesPanel.style.width = savedFiles + 'px';

        }

    }



    setTimeout(restorePanelWidths, 100);



    // Also restore when panels are opened (after toggle)

    const origToggleFiles = window.toggleFilesPanel;

    if (origToggleFiles) {

        window.toggleFilesPanel = function() {

            origToggleFiles();

            setTimeout(function() {

                const savedFiles = localStorage.getItem('filesPanelWidth');

                if (savedFiles && filesPanel && !filesPanel.classList.contains('collapsed')) {

                    filesPanel.style.width = savedFiles + 'px';

                }

            }, 350);

        };

    }



    // Restore preview width when a file is previewed

    const origPreviewFile = window.previewFile;

    if (origPreviewFile) {

        window.previewFile = function(path) {

            origPreviewFile(path);

            setTimeout(function() {

                const savedPreview = localStorage.getItem('previewPanelWidth');

                if (savedPreview && previewPanel && !previewPanel.classList.contains('collapsed')) {

                    previewPanel.style.width = savedPreview + 'px';

                }

            }, 350);

        };

    }

})();
