/**
 * compare-select-mode.js - In-place "pick second file" mode for file comparison
 * =============================================================================
 * NEW FILE (2026-08-25). Replaces the old popup modal (#diffFileSelector) that
 * listed all files in a flat <select> dropdown where you could not browse the
 * directory tree.
 *
 * HOW IT WORKS
 *  1. PreviewManager.startComparison() calls:
 *         CompareSelectMode.start(pathA, (pathB) => PreviewManager.performComparison(pathB))
 *  2. The FILES PANEL enters "compare-select" mode:
 *       - #filesPanel gets the class `compare-select-active` -> the file list
 *         gets a distinct amber background tint + warning border accent
 *         (CSS lives in static/ui/css/07-diff-rowtable-compare.css, clearly marked).
 *       - A banner with instructions appears ABOVE the file list (#filesList).
 *         The message text fades after ~4 s ("short term"), but the colored
 *         background stays until a file is picked or the mode is cancelled.
 *  3. The user scrolls / expands folders exactly as usual:
 *       - clicking a FOLDER row   -> NOT intercepted, normal toggleFolder()
 *         behavior runs, so directories can be browsed freely;
 *       - clicking a FILE row     -> event consumed in CAPTURE phase on
 *         #filesList (before the inline onclick="previewFile(...)" can fire),
 *         then our callback runs with that file's data-path. Mode ends itself.
 *       - action buttons / checkboxes inside rows are NOT intercepted either.
 *  4. Cancel: press Escape, click "Cancel" in the banner, or click the Compare
 *     button again (toggles the mode off).
 *
 * WHY A SINGLE DELEGATED LISTENER (reliability notes)
 *  - The tree HTML is regenerated on every loadTree()/toggleFolder() call, so
 *    per-row onclick attributes would be wiped and re-attached constantly.
 *    #filesList itself is never replaced -> one capture-phase listener survives
 *    ALL re-renders; no rebinding, no lost scroll/expanded state.
 *  - All state lives in the single `state` object below and every transition
 *    logs with the [CompareSelect] prefix -> easy to trace in DevTools console.
 */
(function () {
    'use strict';

    // ===== CONFIG (tweak here) ====================================================
    const PANEL_ID = 'filesPanel';           // files panel <aside>
    const LIST_ID = 'filesList';             // scrollable tree list inside the panel
    const BANNER_ID = 'compareSelectBanner'; // injected above #filesList (once, lazily)
    const MSG_FADE_MS = 4000;                // how long the banner text stays full opacity

    // ===== STATE (single source of truth for the whole mode) ======================
    const state = {
        active: false,      // is selection mode currently on?
        pathA: null,        // file being compared FROM (PreviewManager.currentFile.path)
        onSelect: null,     // callback(pathB) invoked when user clicks a file row
        panelEl: null,      // cached #filesPanel element while active
        listEl: null,       // cached #filesList element while active
        msgTimer: 0         // setTimeout handle for the "short term" message fade
    };

    function log(...args) { console.log('[CompareSelect]', ...args); }

    function escapeHtml(text) {
        const d = document.createElement('div');
        d.textContent = text == null ? '' : String(text);
        return d.innerHTML;
    }

    // ===== BANNER ===================================================================
    /** Create the instruction banner once and insert it ABOVE #filesList. */
    function ensureBanner() {
        let banner = document.getElementById(BANNER_ID);
        if (banner) return banner;

        const listEl = document.getElementById(LIST_ID);
        if (!listEl || !listEl.parentNode) {
            console.error('[CompareSelect] #filesList not found - cannot create banner');
            return null;
        }

        banner = document.createElement('div');
        banner.id = BANNER_ID;
        banner.className = 'compare-select-banner';
        banner.style.display = 'none';
        banner.innerHTML =
            '<span class="compare-select-banner-icon" aria-hidden="true">' +
                '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">' +
                    '<path d="M8 3H5a2 2 0 0 0-2 2v14c0 1.1.9 2 2 2h3M16 3h3a2 2 0 0 1 2 2v14c0 1.1-.9 2-2 2h-3M12 8v8M8 12h8"/>' +
                '</svg>' +
            '</span>' +
            '<span class="compare-select-banner-text" id="compareSelectBannerText"></span>' +
            '<button type="button" class="compare-select-cancel-btn" id="compareSelectCancelBtn" title="Cancel (Esc)">Cancel</button>';

        listEl.parentNode.insertBefore(banner, listEl); // directly above the file area

        const cancelBtn = banner.querySelector('#compareSelectCancelBtn');
        if (cancelBtn) {
            cancelBtn.addEventListener('click', function () {
                log('Cancel button clicked -> ending mode');
                end(false, 'cancelled-by-button');
            });
        }
        return banner;
    }

    // ===== PANEL ====================================================================
    /** Make sure the files panel is actually visible when we enter the mode. */
    function ensurePanelVisible() {
        const panel = document.getElementById(PANEL_ID);
        if (panel && panel.classList.contains('collapsed') && typeof window.toggleFilesPanel === 'function') {
            log('files panel was collapsed -> opening it');
            window.toggleFilesPanel();
        }
    }

    // ===== EVENT HANDLERS =============================================================
    /**
     * Delegated click handler on #filesList (CAPTURE phase).
     *  - folder row / action buttons / checkboxes -> NOT intercepted (normal behavior)
     *  - file row                                  -> consumed, onSelect(pathB) fires
     */
    function onListClick(e) {
        if (!state.active || !state.listEl) return;

        const target = e.target;
        if (!target || typeof target.closest !== 'function') return;

        // 1) Row action buttons (download/delete/run/open) and checkboxes keep working.
        if (target.closest('.tree-action-btn, .tree-checkbox')) {
            log('action button / checkbox click -> not intercepted');
            return;
        }

        const item = target.closest('.tree-item');
        if (!item || !state.listEl.contains(item)) return; // empty space / placeholder text

        const path = item.getAttribute('data-path');
        if (!path) return;

        // 2) Folder row -> let the event continue so toggleFolder() runs normally.
        if (item.classList.contains('tree-folder')) {
            log('folder row clicked -> keeping normal browse behavior:', path);
            return;
        }

        // 3) File row -> select it for comparison. Consumed in CAPTURE phase, i.e.
        //    BEFORE the inline onclick="previewFile(...)" on the row can run.
        e.stopPropagation();
        e.preventDefault();
        log('FILE selected for comparison:', path, '(current file A =', state.pathA + ')');

        if (state.pathA && path === state.pathA) {
            if (typeof window.showNotification === 'function') {
                window.showNotification('Cannot compare a file with itself', 'warning');
            }
            return; // stay in selection mode so the user can pick another file
        }

        const cb = state.onSelect;
        end(true, 'file-selected'); // clean up BEFORE running the diff (idempotent)
        if (typeof cb === 'function') {
            try {
                cb(path);
            } catch (err) {
                console.error('[CompareSelect] onSelect callback threw:', err);
            }
        } else {
            console.warn('[CompareSelect] no onSelect callback registered - selection dropped');
        }
    }

    /** Escape key cancels the mode. */
    function onKeyDown(e) {
        if (!state.active || e.key !== 'Escape') return;
        log('Esc pressed -> cancelling mode');
        end(false, 'cancelled-by-esc');
    }

    // ===== PUBLIC API ==================================================================
    /**
     * Enter compare-select mode (or toggle it OFF when already active).
     * @param {string} pathA            - file being compared FROM
     * @param {function(string):void} onSelect - called with the picked file's path
     * @returns {boolean} true if the mode is now active, false otherwise
     */
    function start(pathA, onSelect) {
        // Compare button clicked again while already picking -> cancel.
        if (state.active) {
            log('start() called while already active -> toggling OFF');
            end(false, 'toggled-off-by-compare-button');
            if (typeof window.showNotification === 'function') {
                window.showNotification('Compare file selection cancelled', 'info');
            }
            return false;
        }

        state.panelEl = document.getElementById(PANEL_ID);
        state.listEl = document.getElementById(LIST_ID);
        if (!state.panelEl || !state.listEl) {
            console.error('[CompareSelect] files panel elements missing - cannot start');
            return false;
        }

        state.pathA = pathA || null;
        state.onSelect = typeof onSelect === 'function' ? onSelect : null;

        ensurePanelVisible();

        const banner = ensureBanner();
        if (banner) {
            const textEl = banner.querySelector('#compareSelectBannerText');
            const fileName = (pathA || '').split('/').pop() || '(unknown file)';
            if (textEl) {
                textEl.classList.remove('faded');
                textEl.innerHTML = 'Pick the <b>second file</b> to compare with <b>' + escapeHtml(fileName) +
                    '</b>. Click a FILE below - folders only expand. Esc / Cancel aborts.';
            }
            banner.style.display = 'flex';
        }

        // "Short term" message: fade the text after MSG_FADE_MS, keep the colored background.
        if (state.msgTimer) clearTimeout(state.msgTimer);
        state.msgTimer = setTimeout(function () {
            if (!state.active) return;
            const textEl = document.getElementById('compareSelectBannerText');
            if (textEl) textEl.classList.add('faded');
        }, MSG_FADE_MS);

        // Visual mode indicator (styles in css/07-diff-rowtable-compare.css).
        state.panelEl.classList.add('compare-select-active');
        try { state.panelEl.dataset.compareSelect = 'active'; } catch (_) { /* non-critical */ }

        // ONE delegated listener survives every tree re-render.
        state.listEl.addEventListener('click', onListClick, true);
        document.addEventListener('keydown', onKeyDown);

        state.active = true;
        log('mode ACTIVE - waiting for user to click a file (pathA =', pathA + ')');

        if (typeof window.showNotification === 'function') {
            window.showNotification('Select the second file in the Files panel', 'info');
        }
        return true;
    }

    /**
     * Leave compare-select mode. Idempotent - safe to call any number of times.
     * @param {boolean} silent - reserved (kept for API symmetry)
     * @param {string}  reason - logged for debugging
     */
    function end(silent, reason) {
        const wasActive = state.active;
        state.active = false;

        if (state.msgTimer) { clearTimeout(state.msgTimer); state.msgTimer = 0; }
        if (state.listEl) state.listEl.removeEventListener('click', onListClick, true);
        document.removeEventListener('keydown', onKeyDown);
        if (state.panelEl) {
            state.panelEl.classList.remove('compare-select-active');
            try { delete state.panelEl.dataset.compareSelect; } catch (_) { /* non-critical */ }
        }
        const banner = document.getElementById(BANNER_ID);
        if (banner) banner.style.display = 'none';

        state.pathA = null;
        state.onSelect = null;

        log(wasActive ? ('mode ENDED (' + (reason || 'unspecified') + ')')
                      : 'end() called while inactive - no-op cleanup');
    }

    // ===== EXPORT =======================================================================
    window.CompareSelectMode = {
        start: start,
        end: end,
        isActive: function () { return state.active; }
    };

    log('module loaded (v2026-08-25)');
})();
