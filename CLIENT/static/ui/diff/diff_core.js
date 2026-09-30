/**
 * diff/diff_core.js - Core line-diff algorithm (no DOM dependencies)
 *
 * Produces a ROW model with perfect left/right alignment:
 *   { type: 'equal' | 'delete' | 'insert' | 'modify',
 *     aLine, bLine        1-based line numbers in file A / file B (null on the missing side)
 *     aText, bText        line text (null on the missing side) }
 *
 * Algorithm:
 *   1. Myers O(ND) diff over the two line arrays -> minimal edit script
 *      (same algorithm family git uses; a moved block shows as one delete +
 *       one insert instead of cascading false modifications).
 *   2. Every changed region is the maximal span between two equal segments. Inside it,
     *      identical lines act as anchors (rendered as context rows, splitting the visual
     *      block) and leftover left/right lines are paired positionally -> 'modify' rows
     *      (old | new on the SAME row); the excess becomes pure delete/insert. */

(function (global) {
    'use strict';

    // ------------------------------------------------------------------
    // Myers O(ND). Returns segments:
    //   [{type:'equal'|'removed'|'added', aStart, bStart, count}]
    //   equal   : A[aStart..+count) == B[bStart..+count)
    //   removed : lines only in A; added: lines only in B.
    // Segments are contiguous and cover both arrays completely.
    // ------------------------------------------------------------------
    function myersSegments(a, b) {
        const n = a.length, m = b.length;
        if (n === 0 && m === 0) return [];

        const maxD = n + m;
        const size = 2 * maxD + 1;
        let prevX = new Int32Array(size).fill(-1);
        let currX = new Int32Array(size).fill(-1);
        // trace[d] stores, for every diagonal reached at iteration d:
        //   x after the greedy snake, and whether we entered from 'down' (B consumed)
        const trace = [];

        let finalD = -1;
        for (let d = 0; d <= maxD && finalD < 0; d++) {
            const rowX = new Int32Array(size).fill(-1);
            const rowFromDown = new Uint8Array(size);
            trace.push({ x: rowX, fromDown: rowFromDown });

            for (let dd = d; dd >= -d; dd -= 2) {
                let x, fromDown;
                if (d === 0) {
                    // First iteration: start at origin, then extend the initial snake.
                    x = 0; fromDown = false;
                } else if (dd === -d || (dd !== d && prevX[dd - 1 + maxD] < prevX[dd + 1 + maxD])) {
                    x = prevX[dd + 1 + maxD];      // down: consume B line
                    fromDown = true;
                } else {
                    x = prevX[dd - 1 + maxD] + 1;  // right: consume A line
                    fromDown = false;
                }
                let y = x - dd;
                while (x < n && y < m && a[x] === b[y]) { x++; y++; }

                currX[dd + maxD] = x;
                rowX[dd + maxD] = x;
                rowFromDown[dd + maxD] = fromDown ? 1 : 0;

                if (x >= n && y >= m) { finalD = d; break; }
            }
            const t = prevX; prevX = currX; currX = t;
        }
        if (finalD < 0) return [];

        // Trace back from (n, m). Each edit's "to" is the END OF THE SNAKE on the
        // previous diagonal -- i.e. the point BEFORE the single step was taken.
        const edits = [];
        let x = n, y = m;
        for (let d = finalD; d > 0; d--) {
            const dd = x - y;
            const fromDown = trace[d].fromDown[dd + maxD] === 1;
            const prevDd = fromDown ? dd + 1 : dd - 1;
            const px = trace[d - 1].x[prevDd + maxD];
            const py = px - prevDd;
            edits.push({ type: fromDown ? 'down' : 'right', toX: px, toY: py });
            x = px; y = py;
        }

        // Walk forward producing segments. Chronological order = reversed edit list.
        const segments = [];
        let ax = 0, by = 0;
        for (let i = edits.length - 1; i >= 0; i--) {
            const e = edits[i];
            if (e.toX > ax || e.toY > by) {
                pushSeg(segments, 'equal', ax, by, Math.min(e.toX - ax, e.toY - by));
            }
            if (e.type === 'right') {
                // single step consumed A line index e.toX
                pushSeg(segments, 'removed', e.toX, e.toY, 1);
                ax = e.toX + 1; by = e.toY;
            } else {
                // single step consumed B line index e.toY
                pushSeg(segments, 'added', e.toX, e.toY, 1);
                ax = e.toX; by = e.toY + 1;
            }
        }
        if (ax < n || by < m) {
            pushSeg(segments, 'equal', ax, by, Math.min(n - ax, m - by));
        }
        return segments;
    }

    function pushSeg(segments, type, aStart, bStart, count) {
        if (count <= 0 && type === 'equal') return;
        const last = segments[segments.length - 1];
        if (last && last.type === type) {
            if (type === 'equal' && last.aStart + last.count === aStart && last.bStart + last.count === bStart) {
                last.count += count; return;
            }
            if (type === 'removed' && last.aStart + last.count === aStart && last.bStart === bStart) {
                last.count += count; return;
            }
            if (type === 'added' && last.bStart + last.count === bStart && last.aStart === aStart) {
                last.count += count; return;
            }
        }
        segments.push({ type, aStart, bStart, count });
    }

    // ------------------------------------------------------------------
    // LCS over two arrays of line texts. Returns [iA, iB] match pairs in order.
    // Used only inside single changed regions (small by nature); guarded for huge ones.
    // ------------------------------------------------------------------
    function lcsPairs(aLines, bLines) {
        const n = aLines.length, m = bLines.length;
        if (n === 0 || m === 0) return [];

        if (n * m > 4_000_000) {
            // Fallback for pathological huge regions: greedy in-order pairing.
            const pairs = [];
            let j = 0;
            for (let i = 0; i < n && j < m; i++) {
                while (j < m && bLines[j] !== aLines[i]) j++;
                if (j < m) { pairs.push([i, j]); j++; }
            }
            return pairs;
        }

        const table = new Array(n + 1);
        for (let i = 0; i <= n; i++) table[i] = new Int32Array(m + 1);
        for (let i = 1; i <= n; i++) {
            const row = table[i], prev = table[i - 1];
            for (let j = 1; j <= m; j++) {
                if (aLines[i - 1] === bLines[j - 1]) row[j] = prev[j - 1] + 1;
                else row[j] = (prev[j] > row[j - 1]) ? prev[j] : row[j - 1];
            }
        }
        const pairs = [];
        let i = n, j = m;
        while (i > 0 && j > 0) {
            if (aLines[i - 1] === bLines[j - 1]) { pairs.push([i - 1, j - 1]); i--; j--; }
            else if (table[i - 1][j] >= table[i][j - 1]) i--;
            else j--;
        }
        pairs.reverse();
        return pairs;
    }

    // ------------------------------------------------------------------
    // Public API
    // ------------------------------------------------------------------

    /** Split content into lines. A trailing newline does NOT create a ghost line:
     *  "a\nb" -> ["a","b"], "a\nb\n" -> ["a","b"] (trailingNewline remembered). */
    function splitLines(content) {
        if (!content) return { lines: [], trailingNewline: false };
        const hasTrail = content.endsWith('\n');
        let body = content;
        // Normalize CRLF/CR to LF so line comparison is stable across platforms.
        body = body.replace(/\r\n/g, '\n').replace(/\r/g, '\n');
        if (hasTrail) body = body.slice(0, -1);
        const lines = body.length ? body.split('\n') : [];
        return { lines, trailingNewline: hasTrail };
    }

    /**
     * Compute aligned diff rows between two file contents.
     * @returns {{rows:Array, stats:Object, identical:boolean}}
     */
    function computeRows(contentA, contentB) {
        const A = splitLines(contentA);
        const B = splitLines(contentB);

        if (contentA === contentB) {
            return {
                rows: makeEqualRows(A.lines),
                stats: emptyStats(A.lines.length, B.lines.length),
                identical: true
            };
        }

        const segments = myersSegments(A.lines, B.lines);

        // Merge consecutive non-equal segments into maximal changed regions.
        // Walk all segments tracking both cursors; a region is the A/B span covered by
        // a maximal run of non-equal segments (removed -> A span, added -> B span).
        const regions = [];
        let openReg = null;
        let ca = 0, cb = 0;   // cursors: next unconsumed index in A / B
        for (const seg of segments) {
            if (seg.type === 'equal') {
                if (openReg) { regions.push(openReg); openReg = null; }
                ca += seg.count; cb += seg.count;
            } else {
                if (!openReg) openReg = { aStart: ca, bStart: cb, aCount: 0, bCount: 0 };
                if (seg.type === 'removed') { openReg.aCount += seg.count; ca += seg.count; }
                else { openReg.bCount += seg.count; cb += seg.count; }
            }
        }
        if (openReg) regions.push(openReg);

        const rows = [];
        let cursorA = 0, cursorB = 0;   // position in A / B already emitted as equal

        for (const reg of regions) {
            // Equal lines before this region.
            while (cursorA < reg.aStart && cursorB < reg.bStart) {
                const k = Math.min(reg.aStart - cursorA, reg.bStart - cursorB);
                for (let s = 0; s < k; s++) {
                    rows.push({
                        type: 'equal',
                        aLine: cursorA + s + 1, bLine: cursorB + s + 1,
                        aText: A.lines[cursorA + s], bText: B.lines[cursorB + s]
                    });
                }
                cursorA += k; cursorB += k;
            }

            // The changed region itself (LCS pairing -> modify/delete/insert rows).
            const areaA = A.lines.slice(reg.aStart, reg.aStart + reg.aCount);
            const areaB = B.lines.slice(reg.bStart, reg.bStart + reg.bCount);
            rows.push(...pairRegion(areaA, areaB, reg.aStart, reg.bStart));

            cursorA = reg.aStart + reg.aCount;
            cursorB = reg.bStart + reg.bCount;
        }
        // Trailing equal lines (defensive: Myers output guarantees they match).
        while (cursorA < A.lines.length || cursorB < B.lines.length) {
            const tA = cursorA < A.lines.length ? A.lines[cursorA] : null;
            const tB = cursorB < B.lines.length ? B.lines[cursorB] : null;
            if (tA !== null && tB !== null && tA === tB) {
                rows.push({ type: 'equal', aLine: cursorA + 1, bLine: cursorB + 1, aText: tA, bText: tB });
                cursorA++; cursorB++;
            } else if (tA !== null && (tB === null || tA !== tB)) {
                rows.push({ type: 'delete', aLine: cursorA + 1, bLine: null, aText: tA, bText: null });
                cursorA++;
            } else {
                rows.push({ type: 'insert', aLine: null, bLine: cursorB + 1, aText: null, bText: tB });
                cursorB++;
            }
        }

        let additions = 0, deletions = 0, modifications = 0;
        for (const r of rows) {
            if (r.type === 'modify') modifications++;
            else if (r.type === 'insert') additions++;
            else if (r.type === 'delete') deletions++;
        }

        return {
            rows,
            stats: {
                additions, deletions, modifications,
                totalChanges: additions + deletions + modifications,
                linesA: A.lines.length, linesB: B.lines.length
            },
            identical: false
        };
    }

    /**
     * Align one changed region's lines (Beyond Compare style):
     *  - identical lines inside the region act as anchors and become context ('equal') rows,
     *    which naturally split the visual change block;
     *  - between anchors, leftover left/right lines are paired positionally -> 'modify' rows
     *    (old | new on the SAME row); the excess on either side becomes pure delete/insert.
     */
    function pairRegion(areaA, areaB, aStart0, bStart0) {
        const pairs = lcsPairs(areaA, areaB);   // anchors [ia, ib] in order
        const usedA = new Set(pairs.map(p => p[0]));
        const usedB = new Set(pairs.map(p => p[1]));
        const rows = [];

        let prevIa = -1, prevIb = -1;

        function emitGap(iaEndExclusive, ibEndExclusive) {
            const la = [], lb = [];
            for (let k = prevIa + 1; k < iaEndExclusive; k++) if (!usedA.has(k)) la.push(k);
            for (let k = prevIb + 1; k < ibEndExclusive; k++) if (!usedB.has(k)) lb.push(k);

            const minLen = Math.min(la.length, lb.length);
            for (let k = 0; k < minLen; k++) {
                rows.push({ type: 'modify', aLine: aStart0 + la[k] + 1, bLine: bStart0 + lb[k] + 1, aText: areaA[la[k]], bText: areaB[lb[k]] });
            }
            for (let k = minLen; k < la.length; k++) {
                rows.push({ type: 'delete', aLine: aStart0 + la[k] + 1, bLine: null, aText: areaA[la[k]], bText: null });
            }
            for (let k = minLen; k < lb.length; k++) {
                rows.push({ type: 'insert', aLine: null, bLine: bStart0 + lb[k] + 1, aText: null, bText: areaB[lb[k]] });
            }
        }

        for (const [ia, ib] of pairs) {
            emitGap(ia, ib);
            rows.push({ type: 'equal', aLine: aStart0 + ia + 1, bLine: bStart0 + ib + 1, aText: areaA[ia], bText: areaB[ib] });
            prevIa = ia; prevIb = ib;
        }
        emitGap(areaA.length, areaB.length);
        return rows;
    }

    function makeEqualRows(lines) {
        const rows = [];
        for (let i = 0; i < lines.length; i++) {
            rows.push({ type: 'equal', aLine: i + 1, bLine: i + 1, aText: lines[i], bText: lines[i] });
        }
        return rows;
    }

    function emptyStats(nA, nB) {
        return { additions: 0, deletions: 0, modifications: 0, totalChanges: 0, linesA: nA, linesB: nB };
    }

    /** Group consecutive non-equal rows into change blocks [{start,end}] (row indices). */
    function groupBlocks(rows) {
        const out = [];
        let s = -1;
        for (let i = 0; i < rows.length; i++) {
            if (rows[i].type !== 'equal') {
                if (s === -1) s = i;
            } else if (s !== -1) {
                out.push({ start: s, end: i - 1 });
                s = -1;
            }
        }
        if (s !== -1) out.push({ start: s, end: rows.length - 1 });
        return out;
    }

    /** Reconstruct file A content from current rows. */
    function contentFromRowsA(rows) {
        const lines = [];
        for (const r of rows) if (r.aText !== null && r.aText !== undefined) lines.push(r.aText);
        return lines.join('\n');
    }

    /** Reconstruct file B content from current rows. */
    function contentFromRowsB(rows) {
        const lines = [];
        for (const r of rows) if (r.bText !== null && r.bText !== undefined) lines.push(r.bText);
        return lines.join('\n');
    }

    /**
     * Apply a change block to one side and renumber all line numbers.
     *   direction 'toB': file B adopts the left-side version of the block
     *                    (delete/modify rows become equal with A's text; insert rows vanish from B)
     *   direction 'toA': file A adopts the right-side version of the block
     * Returns a NEW rows array.
     */
    function applyBlock(rows, blockStart, blockEnd, direction) {
        const out = rows.slice();
        for (let i = blockStart; i <= blockEnd; i++) {
            const r = out[i];
            if (direction === 'toB') {
                if (r.type === 'delete' || r.type === 'modify') {
                    out[i] = { type: 'equal', aLine: null, bLine: null, aText: r.aText, bText: r.aText };
                } else if (r.type === 'insert') {
                    out[i] = { type: 'equal', aLine: null, bLine: null, aText: null, bText: null, __drop: true };
                }
            } else {
                if (r.type === 'insert' || r.type === 'modify') {
                    out[i] = { type: 'equal', aLine: null, bLine: null, aText: r.bText, bText: r.bText };
                } else if (r.type === 'delete') {
                    out[i] = { type: 'equal', aLine: null, bLine: null, aText: null, bText: null, __drop: true };
                }
            }
        }
        const cleaned = out.filter(r => !r.__drop);
        let nA = 0, nB = 0;
        for (const r of cleaned) {
            if (r.aText !== null && r.aText !== undefined) r.aLine = ++nA; else r.aLine = null;
            if (r.bText !== null && r.bText !== undefined) r.bLine = ++nB; else r.bLine = null;
        }
        return cleaned;
    }

    global.DiffCore = {
        computeRows, splitLines, groupBlocks,
        contentFromRowsA, contentFromRowsB, applyBlock
    };
})(typeof window !== 'undefined' ? window : globalThis);
