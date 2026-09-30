/**
 * diff/diff_view.js - Row-Aligned Diff View (Beyond Compare style)  [REWRITTEN 2026-08-25]
 *
 * Both panes are rendered from ONE rows array produced by DiffCore, so left and right
 * lines ALWAYS stay on the same visual row:
 *   equal    -> text in BOTH panes (same row)
 *   delete   -> red line in LEFT pane, striped gap cell in RIGHT pane
 *   insert   -> green line in RIGHT pane, striped gap cell in LEFT pane
 *   modify   -> old value (left pane) | new value (right pane) on the SAME row
 *
 * LAYOUT: file A lives in its OWN left visual space (#diffContentLeft), file B lives in
 * ITS OWN right visual space (#diffContentRight). Each pane gets a mirror-symmetric
 * table:  [gutter | line-number | text]   and   [text | line-number | gutter].
 * Rows have a fixed em-based height and single-line content, so the two tables are
 * pixel-aligned at any font size / zoom; SyncScroll keeps both panes scrolled together.
 *
 * Features:
 * - Change blocks with block-level move arrows:  ->  push left block to right file,
 *   <-  pull right block to left file (the user's "move a block where it is missing/different").
 * - "Change N of M" labels + prev/next change navigation (buttons and N / P keys).
 * - Hide-unchanged toggle: shows only changed blocks with context lines.
 * - "Files are Identical" banner when the files match (full text still visible below).
 * - Stats (+added / -deleted / ~modified) in the toolbar.
 * - Working Save buttons per side (POST /api/file-content).
 * - Unified mode rendered from the same rows.
 */

class DiffView {
    constructor() {
        this.currentMode = 'side-by-side'; // 'side-by-side' or 'unified'
        this.rows = [];
        this.blocks = [];          // [{start, end}] row index ranges of change blocks
        this.currentFileA = null;
        this.currentFileB = null;
        this.stats = { additions: 0, deletions: 0, modifications: 0, totalChanges: 0 };
        this.identical = false;
        this.syntaxRegistry = null;
        this.schemaManager = null;
        this.initialized = false;
        this.modifiedLeft = false;
        this.modifiedRight = false;
        this.leftContainer = null;
        this.rightContainer = null;
        this.showUnchanged = true;   // when false: only changed blocks + context lines
        this.contextLines = 3;       // context around a change block in compact mode
        this.currentChangeIndex = -1;
        this._boundKeyHandler = null;
        this._boundClickHandler = null;
        this._rowEventContainers = [];
    }

    init(syntaxRegistry, schemaManager) {
        if (this.initialized) return;
        this.syntaxRegistry = syntaxRegistry;
        this.schemaManager = schemaManager;
        this.initialized = true;
        console.log('[DiffView] Initialized');
    }

    // ------------------------------------------------------------------
    // Public API used by preview.js / diff_ui.js / app.js (kept compatible)
    // ------------------------------------------------------------------

    /**
     * Render side-by-side diff.
     * @param {Object} fileA - {path, content, size?}  -> rendered in LEFT pane
     * @param {Object} fileB - {path, content, size?}  -> rendered in RIGHT pane
     */
    renderSideBySide(fileA, fileB, containerLeft, containerRight) {
        console.log('[DiffView] Rendering side-by-side diff', {
            fileA: fileA.path, fileB: fileB.path,
            contentLengthA: (fileA.content || '').length,
            contentLengthB: (fileB.content || '').length
        });

        this.currentFileA = Object.assign({}, fileA);
        this.currentFileB = Object.assign({}, fileB);
        this.currentFileA.originalContent = this.currentFileA.content;
        this.currentFileB.originalContent = this.currentFileB.content;
        this.currentMode = 'side-by-side';
        this.leftContainer = containerLeft;
        this.rightContainer = containerRight;
        this.modifiedLeft = false;
        this.modifiedRight = false;
        this.currentChangeIndex = -1;

        const core = (typeof DiffCore !== 'undefined') ? DiffCore : null;
        if (!core) {
            console.error('[DiffView] DiffCore not loaded');
            return;
        }

        const result = core.computeRows(this.currentFileA.content || '', this.currentFileB.content || '');
        this.rows = result.rows;
        this.stats = result.stats;
        this.identical = result.identical;
        this.blocks = core.groupBlocks(this.rows);

        // Both panes are visible and scrollable: file A in its left space, file B in its right space.
        this._setupSharedScroll(containerLeft, containerRight);

        this._syncToggleButton();
        this.render();
        this.updateHeaders();
        this.displayStats();
    }

    /** Make BOTH panes the shared-scroll hosts (rows stay aligned by construction). */
    _setupSharedScroll(containerLeft, containerRight) {
        if (!containerLeft || !containerRight) return;
        // Reset any previous state (e.g. after mode switch back and forth).
        containerLeft.classList.add('diff-shared-scroll');
        containerLeft.style.display = 'block';
        containerRight.classList.add('diff-shared-scroll');
        containerRight.style.display = 'block';
    }

    /** Render the current rows into whichever mode is active. */
    render() {
        if (this.currentMode === 'side-by-side') {
            this._renderSideBySide();
        } else {
            this._renderUnified();
        }
    }

    // ------------------------------------------------------------------
    // Side-by-side rendering: file A -> left pane, file B -> right pane
    // ------------------------------------------------------------------

    /** Render side-by-side: file A rows into the LEFT pane table, file B rows into the RIGHT one. */
    _renderSideBySide() {
        const leftContainer = this.leftContainer;
        if (!leftContainer) return;
        const rightContainer = this.rightContainer;

        const schema = this.schemaManager ? this.schemaManager.getCurrentSchema() : null;
        const hlA = this.syntaxRegistry ? this.syntaxRegistry.getHighlighterForFile(this.currentFileA.path) : null;
        const hlB = this.syntaxRegistry ? this.syntaxRegistry.getHighlighterForFile(this.currentFileB.path) : null;

        const visible = this._visibleRowIndices();
        const MAX_ROWS = 8000; // safety cap for huge files in full mode
        let truncatedNote = '';
        let renderedCount = 0;

        const blockByStart = new Map(this.blocks.map((b, i) => [b.start, i]));
        let leftHtml = [];
        let rightHtml = [];

        if (this.identical) {
            const banner = `
                <div class="diff-identical-banner">
                    <span class="diff-identical-icon">&#10004;</span>
                    <span><strong>Files are Identical</strong> &mdash; ${this.escapeHtml(this.getFileName(this.currentFileA.path))} and ${this.escapeHtml(this.getFileName(this.currentFileB.path))} have exactly the same content (${this._formatSize(this.currentFileA.size)}).</span>
                </div>`;
            leftHtml.push(banner);
            rightHtml.push(banner);
        }

        let lastVisible = -2;
        for (let i = 0; i < this.rows.length; i++) {
            if (visible && !visible.has(i)) continue;
            if (!visible && renderedCount >= MAX_ROWS) {
                truncatedNote = `<div class="diff-truncated-note">&#9888; rendering stopped at ${MAX_ROWS} rows (very large file). Use "Hide unchanged" to see only the changes.</div>`;
                break;
            }
            // Ellipsis row for gaps in compact mode.
            if (visible && lastVisible !== -2 && i > lastVisible + 1) {
                const gap = i - lastVisible - 1;
                const ell = `<div class="diff-ellipsis-row" title="${gap} unchanged line(s) hidden"><span class="diff-line-number"></span><span class="diff-ellipsis-text">&hellip;</span><span class="diff-line-number"></span></div>`;
                leftHtml.push(ell);
                rightHtml.push(ell);
            }

            const row = this.rows[i];
            const [l, r] = this._renderRowSides(row, i, blockByStart.get(i), schema, hlA, hlB);
            leftHtml.push(l);
            rightHtml.push(r);
            lastVisible = i;
            renderedCount++;
        }

        const tableStyle = `style="${schema ? 'background:' + schema.background : ''}"`;
        if (leftContainer) {
            leftContainer.innerHTML = `<div class="diff-row-table diff-row-table-left" ${tableStyle}>${leftHtml.join('')}${truncatedNote}</div>`;
        }
        if (rightContainer) {
            rightContainer.innerHTML = `<div class="diff-row-table diff-row-table-right" ${tableStyle}>${rightHtml.join('')}</div>`;
        }

        this._setupRowEvents(leftContainer, rightContainer);
    }

        _visibleRowIndices() {
        if (this.showUnchanged) return null; // all rows
        const ctx = this.contextLines;
        const visible = new Set();
        const n = this.rows.length;
        const blocks = this.blocks.slice().sort((a, b) => a.start - b.start);

        // BUG FIX (2026-08-27): previously EVERY block got the full context on BOTH sides.
        // When change blocks sit closer than 2*ctx+1 rows apart (very common in real files),
        // the merged contexts covered ALL rows -> compact mode rendered exactly the same as
        // full mode and the toggle reported "Every line differs - there are no unchanged
        // lines to hide" although equal lines existed. Now the context shared across each
        // inter-block gap is capped so that at least one unchanged row stays hidden in every
        // gap (ellipsis) whenever such a row exists; file-edge contexts keep the full ctx.
        for (let bi = 0; bi < blocks.length; bi++) {
            const blk = blocks[bi];

            let leftCtx = ctx;   // full context at the start of the file
            if (bi > 0) {
                const gap = blk.start - blocks[bi - 1].end - 1;      // unchanged rows before this block
                if (gap >= 1) leftCtx = Math.ceil(Math.min(2 * ctx, gap - 1) / 2);
                else leftCtx = 0;                                    // no unchanged row between blocks
            }

            let rightCtx = ctx;  // full context at the end of the file
            if (bi < blocks.length - 1) {
                const gap = blocks[bi + 1].start - blk.end - 1;      // unchanged rows after this block
                if (gap >= 1) rightCtx = Math.floor(Math.min(2 * ctx, gap - 1) / 2);
                else rightCtx = 0;
            }

            const from = Math.max(0, blk.start - leftCtx);
            const to = Math.min(n - 1, blk.end + rightCtx);
            for (let i = from; i <= to; i++) visible.add(i);
        }
        return visible;
    }

    /**
     * Build the two halves of one grid row (mirror-symmetric):
     *   left pane  : [gutter | A line-number | A text]
     *   right pane : [B text | B line-number | gutter]
     * @returns {[string, string]} [leftRowHtml, rightRowHtml]
     */
    _renderRowSides(row, rowIndex, blockIndex, schema, hlA, hlB) {
        const isChange = row.type !== 'equal';
        let rowClass = 'diff-row diff-row-' + row.type;
        if (isChange && this.currentChangeIndex === blockIndex) rowClass += ' diff-row-current-change';

        // ---- left side cell (file A) ----
        let leftNum, leftContentHtml, leftCellClass;
        if (row.aText !== null && row.aText !== undefined) {
            leftNum = `<span class="diff-line-number">${row.aLine}</span>`;
            leftContentHtml = this._highlight(row.aText, hlA, schema);
            leftCellClass = 'diff-cell diff-cell-left';
            if (row.type === 'delete') leftCellClass += ' diff-cell-removed';
            else if (row.type === 'modify') leftCellClass += ' diff-cell-modified';
        } else {
            leftNum = `<span class="diff-line-number"></span>`;
            leftContentHtml = '<span class="diff-placeholder">&nbsp;</span>';
            leftCellClass = 'diff-cell diff-cell-left diff-cell-empty';
            if (row.type === 'insert') leftCellClass += ' diff-cell-gap-insert';
        }

        // ---- right side cell (file B) ----
        let rightNum, rightContentHtml, rightCellClass;
        if (row.bText !== null && row.bText !== undefined) {
            rightNum = `<span class="diff-line-number">${row.bLine}</span>`;
            rightContentHtml = this._highlight(row.bText, hlB, schema);
            rightCellClass = 'diff-cell diff-cell-right';
            if (row.type === 'insert') rightCellClass += ' diff-cell-added';
            else if (row.type === 'modify') rightCellClass += ' diff-cell-modified';
        } else {
            rightNum = `<span class="diff-line-number"></span>`;
            rightContentHtml = '<span class="diff-placeholder">&nbsp;</span>';
            rightCellClass = 'diff-cell diff-cell-right diff-cell-empty';
            if (row.type === 'delete') rightCellClass += ' diff-cell-gap-delete';
        }

        // ---- gutters: block arrow + change label on the first row of a block ----
        let leftGutter = '<span class="diff-gutter"></span>';
        let rightGutter = '<span class="diff-gutter"></span>';
        if (blockIndex !== undefined && isChange) {
            const blk = this.blocks[blockIndex];
            const label = `<span class="diff-change-label" title="Jump to change">C${blockIndex + 1}/${this.blocks.length}</span>`;

            // Left side has content in this block? (delete or modify rows) -> can push to right.
            const leftHasContent = this.rows.slice(blk.start, blk.end + 1).some(r => r.aText !== null && r.aText !== undefined);
            // Right side has content in this block? (insert or modify rows) -> can pull to left.
            const rightHasContent = this.rows.slice(blk.start, blk.end + 1).some(r => r.bText !== null && r.bText !== undefined);

            if (leftHasContent) {
                leftGutter = `<span class="diff-gutter diff-gutter-active">
                    ${label}
                    <button class="diff-block-arrow diff-block-arrow-right" data-change="${blockIndex}" data-side="left"
                        title="Push this block to the right file (right adopts left content)">&rarr;</button>
                </span>`;
            } else {
                leftGutter = `<span class="diff-gutter">${label}</span>`;
            }
            if (rightHasContent) {
                rightGutter = `<span class="diff-gutter diff-gutter-active">
                    ${label}
                    <button class="diff-block-arrow diff-block-arrow-left" data-change="${blockIndex}" data-side="right"
                        title="Pull this block to the left file (left adopts right content)">&larr;</button>
                </span>`;
            } else {
                rightGutter = `<span class="diff-gutter">${label}</span>`;
            }
        }

        const leftRowHtml = `
            <div class="${rowClass}" data-row="${rowIndex}">
                ${leftGutter}
                ${leftNum}
                <div class="${leftCellClass}"><div class="diff-line-content">${leftContentHtml}</div></div>
            </div>`;

        const rightRowHtml = `
            <div class="${rowClass}" data-row="${rowIndex}">
                <div class="${rightCellClass}"><div class="diff-line-content">${rightContentHtml}</div></div>
                ${rightNum}
                ${rightGutter}
            </div>`;

        return [leftRowHtml, rightRowHtml];
    }

    _highlight(text, highlighter, schema) {
        if (text === null || text === undefined) return '';
        const escaped = this.escapeHtml(text);
        if (!highlighter || !schema || text.trim().length === 0) return escaped;
        try {
            return highlighter.highlightLine(text, schema);
        } catch (e) {
            return escaped;
        }
    }

    // ------------------------------------------------------------------
    // Row events: block arrows + change navigation clicks (both panes)
    // ------------------------------------------------------------------

    _setupRowEvents(leftContainer, rightContainer) {
        if (!this._boundClickHandler) {
            const self = this;
            this._boundClickHandler = function (e) {
                const btn = e.target.closest('.diff-block-arrow');
                if (btn) {
                    e.stopPropagation();
                    const idx = parseInt(btn.dataset.change, 10);
                    const side = btn.dataset.side || null;
                    if (side === 'left' || side === 'right') self.acceptBlockWithDirection(idx, side);
                    else self.acceptBlock(idx);
                    return;
                }
            };
        }

        // Detach from containers of a previous render, then attach to both current panes.
        for (const c of this._rowEventContainers) {
            if (c && c !== leftContainer && c !== rightContainer) {
                c.removeEventListener('click', this._boundClickHandler);
            }
        }
        if (leftContainer) leftContainer.addEventListener('click', this._boundClickHandler);
        if (rightContainer) rightContainer.addEventListener('click', this._boundClickHandler);
        this._rowEventContainers = [leftContainer, rightContainer].filter(Boolean);

        // Keyboard navigation N / P while in diff mode.
        if (!this._boundKeyHandler) {
            const self = this;
            this._boundKeyHandler = function (e) {
                if (!window.DiffView || !window.DiffView.currentFileA) return;
                const tag = (e.target && e.target.tagName) ? e.target.tagName.toLowerCase() : '';
                if (tag === 'input' || tag === 'textarea' || (e.target.isContentEditable)) return;
                if (e.key === 'n' || e.key === 'N') { self.nextChange(); }
                else if (e.key === 'p' || e.key === 'P') { self.prevChange(); }
            };
        }
        document.removeEventListener('keydown', this._boundKeyHandler);
        document.addEventListener('keydown', this._boundKeyHandler);
    }

    // ------------------------------------------------------------------
    // Block acceptance (the "move a block" feature) - REWRITTEN on top of DiffCore rows
    // ------------------------------------------------------------------

    /**
     * Accept an entire change block from one side to the other.
     * @param {number} blockIndex - index into this.blocks
     */
    acceptBlock(blockIndex) {
        const blk = this.blocks[blockIndex];
        if (!blk) return;

        // Determine direction: arrow on LEFT pushes to right ('toB'); arrow on RIGHT pulls to left ('toA').
        // The caller (rendered button) knows which side it is on, but acceptBlock may be called
        // programmatically too - so we detect from the block content: if both sides have content
        // and this was invoked via a specific arrow, the dataset tells us. Here we expose both
        // directions explicitly; the rendered buttons call acceptBlockWithDirection().
        console.log(`[DiffView] Accepting change block ${blockIndex + 1}/${this.blocks.length}`);

        // Default: apply to BOTH interpretations is ambiguous - so we pick based on which
        // side has MORE lines (the "source" of the move). Explicit arrows override this.
        const leftLines = this.rows.slice(blk.start, blk.end + 1).filter(r => r.aText !== null && r.aText !== undefined).length;
        const rightLines = this.rows.slice(blk.start, blk.end + 1).filter(r => r.bText !== null && r.bText !== undefined).length;
        const direction = leftLines >= rightLines ? 'toB' : 'toA';
        this._applyBlockDirection(blockIndex, direction);
    }

    /** Explicit-direction entry point used by the rendered arrows. */
    acceptBlockWithDirection(blockIndex, side) {
        // side: 'left' -> push left to right ('toB');  'right' -> pull right to left ('toA')
        const direction = (side === 'left') ? 'toB' : 'toA';
        this._applyBlockDirection(blockIndex, direction);
    }

    _applyBlockDirection(blockIndex, direction) {
        if (typeof DiffCore === 'undefined') return;
        const blk = this.blocks[blockIndex];
        if (!blk) return;

        const beforeA = DiffCore.contentFromRowsA(this.rows);
        const beforeB = DiffCore.contentFromRowsB(this.rows);

        this.rows = DiffCore.applyBlock(this.rows, blk.start, blk.end, direction);
        this.blocks = DiffCore.groupBlocks(this.rows);

        const afterA = DiffCore.contentFromRowsA(this.rows);
        const afterB = DiffCore.contentFromRowsB(this.rows);

        let leftChanged = false, rightChanged = false;
        if (afterA !== beforeA) { this.modifiedLeft = true; leftChanged = true; }
        if (afterB !== beforeB) { this.modifiedRight = true; rightChanged = true; }

        // Update in-memory file contents so Save writes the correct data.
        this.currentFileA.content = afterA;
        this.currentFileB.content = afterB;

        const res = DiffCore.computeRows(afterA, afterB);
        this.stats = res.stats;
        this.identical = res.identical && res.rows.length === this.rows.length ? true : (res.stats.totalChanges === 0);

        // Clamp current change index.
        if (this.currentChangeIndex >= this.blocks.length) this.currentChangeIndex = -1;

        this.render();
        this.updateHeaders();
        this.displayStats();

        if (leftChanged && rightChanged) this.showNotification('Both files were modified', 'info');
        else if (rightChanged) this.showNotification(`Right file updated (${this.getFileName(this.currentFileB.path)}) - press Save on the right to write it`, 'success');
        else if (leftChanged) this.showNotification(`Left file updated (${this.getFileName(this.currentFileA.path)}) - press Save on the left to write it`, 'success');
        else this.showNotification('No changes needed (content already matched)', 'info');

        if (this.identical && res.stats.totalChanges === 0) {
            this.showNotification('Files are now Identical', 'success');
        }
    }

    // ------------------------------------------------------------------
    // Change navigation
    // ------------------------------------------------------------------

    nextChange() {
        if (!this.blocks.length) return;
        this.currentChangeIndex = (this.currentChangeIndex + 1) % this.blocks.length;
        // Refresh the toolbar "Change N/M" label so navigation gives visible feedback.
        this.displayStats();
        this._scrollToCurrentChange();
    }

    prevChange() {
        if (!this.blocks.length) return;
        this.currentChangeIndex = (this.currentChangeIndex - 1 + this.blocks.length) % this.blocks.length;
        // Refresh the toolbar "Change N/M" label so navigation gives visible feedback.
        this.displayStats();
        this._scrollToCurrentChange();
    }

    _scrollToCurrentChange() {
        const container = this.leftContainer;
        if (!container || this.currentChangeIndex < 0) return;
        // Re-render to move the highlight, then scroll.
        this.render();
        const el = container.querySelector('.diff-row-current-change');
        if (el && el.scrollIntoView) {
            // Jump instantly first so even very far changes land immediately and
            // reliably (smooth-only scrolling can lag/race on long files), then
            // settle smoothly to the vertical center of the pane.
            el.scrollIntoView({ behavior: 'auto', block: 'nearest' });
            requestAnimationFrame(() => {
                el.scrollIntoView({ behavior: 'smooth', block: 'center' });
            });
        }
    }
    /** Keep the toolbar toggle button in sync with the current state.
     *  Label convention: the label names the ACTION a click will perform, so it
     *  must describe what is NOT currently shown (full view -> "Hide Unchanged"). */
    _syncToggleButton() {
        const btn = document.getElementById('diffToggleUnchanged');
        if (!btn) return;
        btn.classList.toggle('active', !this.showUnchanged);
        const span = btn.querySelector('span');
        if (span) span.textContent = this.showUnchanged ? 'Hide Unchanged' : 'Show All';
        btn.title = this.showUnchanged
            ? 'Hide unchanged lines (show only changed blocks with context)'
            : 'Show all lines again';
    }

    /** Toggle between showing all rows and only changed blocks + context.
     *  Guards: if there is nothing to hide (identical files, or every line differs)
     *  the toggle would look like a no-op - explain it with a notification instead. */
    toggleShowUnchanged() {
        if (!this.blocks.length) {
            this.showNotification('Files are identical - there is nothing to hide', 'info');
            return;
        }
        this.showUnchanged = !this.showUnchanged;
        if (!this.showUnchanged) {
            const visibleCount = this._visibleRowIndices().size;
            if (visibleCount >= this.rows.length) {
                // Compact mode would render exactly the same rows as full mode.
                this.showUnchanged = true;
                this.showNotification('Every line differs - there are no unchanged lines to hide', 'info');
                return;
            }
        }
        this._syncToggleButton();
        this.render();
    }

    // ------------------------------------------------------------------
    // Unified mode (same rows, single column)
    // ------------------------------------------------------------------

    _renderUnified() {
        const container = document.getElementById('diffUnifiedContainer');
        if (!container) return;

        const schema = this.schemaManager ? this.schemaManager.getCurrentSchema() : null;
        const hlA = this.syntaxRegistry ? this.syntaxRegistry.getHighlighterForFile(this.currentFileA.path) : null;

        let html = [];
        if (this.identical) {
            html.push(`<div class="diff-identical-banner"><span class="diff-identical-icon">&#10004;</span> <strong>Files are Identical</strong></div>`);
        }
        for (const row of this.rows) {
            if (row.type === 'equal') {
                html.push(`<div class="diff-row diff-row-equal">
                    <span class="diff-line-number">${row.aLine}</span>
                    <span class="diff-line-number">${row.bLine}</span>
                    <div class="diff-cell"><div class="diff-line-content">${this._highlight(row.aText, hlA, schema)}</div></div>
                </div>`);
            } else if (row.type === 'delete') {
                html.push(`<div class="diff-row diff-row-delete">
                    <span class="diff-line-number">${row.aLine}</span>
                    <span class="diff-line-number"></span>
                    <div class="diff-cell diff-cell-removed"><div class="diff-line-content">&minus; ${this._highlight(row.aText, hlA, schema)}</div></div>
                </div>`);
            } else if (row.type === 'insert') {
                html.push(`<div class="diff-row diff-row-insert">
                    <span class="diff-line-number"></span>
                    <span class="diff-line-number">${row.bLine}</span>
                    <div class="diff-cell diff-cell-added"><div class="diff-line-content">&plus; ${this._highlight(row.bText, hlA, schema)}</div></div>
                </div>`);
            } else { // modify: show both on one row (old -> new)
                html.push(`<div class="diff-row diff-row-modify">
                    <span class="diff-line-number">${row.aLine}</span>
                    <span class="diff-line-number">${row.bLine}</span>
                    <div class="diff-cell diff-cell-modified"><div class="diff-line-content">&rarr; ${this._highlight(row.bText, hlA, schema)}</div></div>
                </div>`);
            }
        }
        container.innerHTML = `<div class="diff-row-table" style="${schema ? 'background:' + schema.background : ''}">${html.join('')}</div>`;
    }

    // ------------------------------------------------------------------
    // Mode switching / lifecycle (compatible with existing callers)
    // ------------------------------------------------------------------

    setMode(mode, containerLeft, containerRight) {
        if (!this.currentFileA || !this.currentFileB) return;
        this.currentMode = mode;
        if (mode === 'side-by-side') {
            if (containerLeft && containerRight) this._setupSharedScroll(containerLeft, containerRight);
            else {
                this.leftContainer = document.getElementById('diffContentLeft');
                this.rightContainer = document.getElementById('diffContentRight');
                if (this.leftContainer && this.rightContainer) this._setupSharedScroll(this.leftContainer, this.rightContainer);
            }
        }
        this.render();
        this.displayStats();
    }

    reapplyHighlighting(leftContainer, rightContainer) {
        if (!this.currentFileA || !this.currentFileB) return;
        if (leftContainer && rightContainer) {
            this.leftContainer = leftContainer;
            this.rightContainer = rightContainer;
            if (this.currentMode === 'side-by-side') this._setupSharedScroll(leftContainer, rightContainer);
        }
        this.render();
    }

    clear() {
        this.rows = [];
        this.blocks = [];
        this.currentFileA = null;
        this.currentFileB = null;
        this.currentMode = 'side-by-side';
        this.modifiedLeft = false;
        this.modifiedRight = false;
        this.identical = false;
        this.currentChangeIndex = -1;

        const leftContainer = document.getElementById('diffContentLeft');
        const rightContainer = document.getElementById('diffContentRight');
        const unifiedContainer = document.getElementById('diffUnifiedContainer');
        if (leftContainer) { leftContainer.innerHTML = ''; leftContainer.classList.remove('diff-shared-scroll'); }
        if (rightContainer) { rightContainer.innerHTML = ''; rightContainer.classList.remove('diff-shared-scroll'); }
        if (unifiedContainer) unifiedContainer.innerHTML = '';

        for (const c of this._rowEventContainers) {
            if (c) c.removeEventListener('click', this._boundClickHandler);
        }
        this._rowEventContainers = [];

        if (this._boundKeyHandler) {
            document.removeEventListener('keydown', this._boundKeyHandler);
        }
    }

    // ------------------------------------------------------------------
    // Headers, stats, save
    // ------------------------------------------------------------------

    updateHeaders() {
        const headerLeft = document.getElementById('diffHeaderLeft');
        const headerRight = document.getElementById('diffHeaderRight');

        if (headerLeft) {
            headerLeft.innerHTML = `
                <span class="diff-header-path" title="${this.escapeHtml(this.currentFileA ? this.currentFileA.path : '')}">
                    &#128196; ${this.escapeHtml(this.getFileName(this.currentFileA ? this.currentFileA.path : ''))}
                    ${this.modifiedLeft ? '<span class="diff-modified-badge" title="Unsaved changes">&#9679; modified</span>' : ''}
                </span>
                <button class="diff-save-btn" onclick="window.DiffView.saveFile('left')" title="Save left file to disk">&#128190; Save</button>`;
        }
        if (headerRight) {
            headerRight.innerHTML = `
                <span class="diff-header-path" title="${this.escapeHtml(this.currentFileB ? this.currentFileB.path : '')}">
                    &#128196; ${this.escapeHtml(this.getFileName(this.currentFileB ? this.currentFileB.path : ''))}
                    ${this.modifiedRight ? '<span class="diff-modified-badge" title="Unsaved changes">&#9679; modified</span>' : ''}
                </span>
                <button class="diff-save-btn" onclick="window.DiffView.saveFile('right')" title="Save right file to disk">&#128190; Save</button>`;
        }
    }

    /** Display stats in the toolbar - shows "Files are Identical" when no changes. */
    displayStats() {
        const statsContainer = document.getElementById('diffStats');
        if (!statsContainer) return;

        let html = '';
        if (this.identical || this.stats.totalChanges === 0) {
            html += `<span class="diff-stat identical" title="Files are identical">&#9989; Files are Identical</span>`;
        } else {
            if (this.stats.additions > 0) html += `<span class="diff-stat added" title="Added lines">+${this.stats.additions}</span>`;
            if (this.stats.deletions > 0) html += `<span class="diff-stat removed" title="Deleted lines">-${this.stats.deletions}</span>`;
            if (this.stats.modifications > 0) html += `<span class="diff-stat modified" title="Modified lines">~${this.stats.modifications}</span>`;
            html += `<span class="diff-stat total" title="Total changed rows">${this.stats.totalChanges} changes</span>`;
        }

        // Change navigation buttons.
        if (this.blocks.length > 0) {
            const cur = this.currentChangeIndex >= 0 ? ` ${this.currentChangeIndex + 1}/${this.blocks.length}` : '';
            html += `<button class="diff-nav-btn" onclick="window.DiffView.prevChange()" title="Previous change (P key)">&#8593; Prev</button>`;
            html += `<span class="diff-stat total">Change${cur}</span>`;
            html += `<button class="diff-nav-btn" onclick="window.DiffView.nextChange()" title="Next change (N key)">Next &#8595;</button>`;
        }

        statsContainer.innerHTML = html;
    }

    /** Save one side to disk. */
    async saveFile(side) {
        const file = side === 'left' ? this.currentFileA : this.currentFileB;
        if (!file) return;
        const updatedContent = (side === 'left')
            ? DiffCore.contentFromRowsA(this.rows)
            : DiffCore.contentFromRowsB(this.rows);

        if (updatedContent === file.originalContent) {
            if (side === 'left') this.modifiedLeft = false; else this.modifiedRight = false;
            this.updateHeaders();
            this.showNotification(`No changes to save: ${this.getFileName(file.path)}`, 'info');
            return;
        }

        try {
            const response = await fetch('/api/file-content', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ path: file.path, content: updatedContent })
            });
            if (!response.ok) {
                let detail = '';
                try { detail = (await response.json()).detail || ''; } catch (_) {}
                throw new Error(detail || `HTTP ${response.status}`);
            }

            file.content = updatedContent;
            file.originalContent = updatedContent;
            if (side === 'left') this.modifiedLeft = false; else this.modifiedRight = false;

            // Re-diff from the now-saved state so line numbers / stats stay truthful.
            const res = DiffCore.computeRows(this.currentFileA.content, this.currentFileB.content);
            this.rows = res.rows;
            this.stats = res.stats;
            this.identical = res.stats.totalChanges === 0;
            this.blocks = DiffCore.groupBlocks(this.rows);
            if (this.currentChangeIndex >= this.blocks.length) this.currentChangeIndex = -1;

            this.render();
            this.updateHeaders();
            this.displayStats();
            this.showNotification(`Saved: ${this.getFileName(file.path)}`, 'success');

            if (typeof window.loadTree === 'function') { try { window.loadTree(); } catch (_) {} }
        } catch (e) {
            console.error('[DiffView] Save failed:', e);
            this.showNotification(`Failed to save ${this.getFileName(file.path)}: ${e.message}`, 'error');
        }
    }

    // ------------------------------------------------------------------
    // Helpers
    // ------------------------------------------------------------------

    getFileName(path) {
        if (!path) return '';
        const parts = String(path).split(/[\\/]/);
        return parts[parts.length - 1] || path;
    }

    _formatSize(bytes) {
        if (bytes === undefined || bytes === null) return '';
        if (bytes < 1024) return `${bytes} B`;
        const k = ['KB', 'MB', 'GB'];
        let v = bytes, i = -1;
        while (v >= 1024 && i < k.length - 1) { v /= 1024; i++; }
        return `${v.toFixed(1)} ${k[i]}`;
    }

    showNotification(message, type = 'info') {
        if (typeof window.showNotification === 'function') window.showNotification(message, type);
        else console.log(`[DiffView] ${type}: ${message}`);
    }

    escapeHtml(text) {
        if (text === null || text === undefined) return '';
        const div = document.createElement('div');
        div.textContent = String(text);
        return div.innerHTML;
    }
}

// NOTE (2026-08-27): `class DiffView` creates a lexical global that SHADOWS this property for bare identifiers, so inline handlers MUST use the explicit window. prefix (onclick="window.DiffView.nextChange()" etc.) - otherwise they resolve to the CLASS and fail with "DiffView.nextChange is not a function".
// Create singleton instance
window.DiffView = new DiffView();
