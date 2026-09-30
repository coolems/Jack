/**
 * diff/diff_engine.js - Proper Block-Aligned Diff Engine
 * Uses jsdiff library for accurate line-by-line diff with block alignment
 * 
 * Features:
 * - Aligns matching blocks on the same rows
 * - Shows deletions on left, insertions on right
 * - Converts delete+insert pairs to modifications when appropriate
 * - Provides accurate line number tracking
 */

class DiffEngine {
    constructor() {
        // Check if jsdiff is available
        if (typeof window.Diff === 'undefined') {
            console.warn('[DiffEngine] jsdiff library not loaded. Please add <script src="https://cdn.jsdelivr.net/npm/diff@5.1.0/dist/diff.min.js"></script>');
        }
        this.diffLib = window.Diff;
    }

    /**
     * Compute diff between two arrays of lines with proper block alignment
     * Uses jsdiff's diffLines which handles block alignment correctly
     * 
     * @param {string[]} linesA - First array of lines (original file)
     * @param {string[]} linesB - Second array of lines (modified file)
     * @returns {Object[]} Array of diff operations with proper block alignment
     */
    computeDiff(linesA, linesB) {
        // Use jsdiff's line diff which handles block alignment
        const changes = this.diffLib.diffLines(linesA.join('\n'), linesB.join('\n'));
        
        const operations = [];
        let lineNumA = 1;
        let lineNumB = 1;
        
        for (const change of changes) {
            // Split the content into lines, removing trailing empty line
            const lines = change.value.split('\n');
            if (lines[lines.length - 1] === '') {
                lines.pop();
            }
            
            if (change.added) {
                // Insertions - show on right panel, empty space on left
                for (const line of lines) {
                    operations.push({
                        type: 'insert',
                        lineA: null,
                        contentA: null,
                        lineB: lineNumB++,
                        contentB: line
                    });
                }
            } else if (change.removed) {
                // Deletions - show on left panel, empty space on right
                for (const line of lines) {
                    operations.push({
                        type: 'delete',
                        lineA: lineNumA++,
                        contentA: line,
                        lineB: null,
                        contentB: null
                    });
                }
            } else {
                // Equal lines - show on both panels, aligned on same row
                for (const line of lines) {
                    operations.push({
                        type: 'equal',
                        lineA: lineNumA++,
                        contentA: line,
                        lineB: lineNumB++,
                        contentB: line
                    });
                }
            }
        }
        
        // Post-process to optimize delete+insert pairs into modifications
        return this.optimizeToModifications(operations);
    }

    /**
     * Convert adjacent delete+insert pairs to modify operations
     * This makes the diff cleaner by showing changed lines as modifications
     * instead of separate delete and insert operations
     * 
     * @param {Object[]} operations - Raw diff operations
     * @returns {Object[]} Optimized operations with modifications
     */
    optimizeToModifications(operations) {
        const optimized = [];
        let i = 0;
        
        while (i < operations.length) {
            const current = operations[i];
            const next = operations[i + 1];
            
            // Check if this is a delete followed by an insert on the same position
            if (current.type === 'delete' && next && next.type === 'insert') {
                // These could be a modification (line changed)
                // Check if the line content is different
                if (current.contentA !== next.contentB) {
                    optimized.push({
                        type: 'modify',
                        lineA: current.lineA,
                        contentA: current.contentA,
                        lineB: next.lineB,
                        contentB: next.contentB
                    });
                } else {
                    // Same content - this shouldn't happen, but handle it
                    optimized.push({
                        type: 'equal',
                        lineA: current.lineA,
                        contentA: current.contentA,
                        lineB: next.lineB,
                        contentB: next.contentB
                    });
                }
                i += 2;
                continue;
            }
            
            // Check for insert followed by delete (less common)
            if (current.type === 'insert' && next && next.type === 'delete') {
                if (current.contentB !== next.contentA) {
                    optimized.push({
                        type: 'modify',
                        lineA: next.lineA,
                        contentA: next.contentA,
                        lineB: current.lineB,
                        contentB: current.contentB
                    });
                } else {
                    optimized.push({
                        type: 'equal',
                        lineA: next.lineA,
                        contentA: next.contentA,
                        lineB: current.lineB,
                        contentB: current.contentB
                    });
                }
                i += 2;
                continue;
            }
            
            // Keep as-is
            optimized.push(current);
            i++;
        }
        
        return optimized;
    }

    /**
     * Get unified diff format (like git diff)
     * 
     * @param {string[]} linesA - First array of lines
     * @param {string[]} linesB - Second array of lines
     * @param {number} contextLines - Number of context lines around changes
     * @returns {string[]} Array of unified diff lines
     */
    getUnifiedDiff(linesA, linesB, contextLines = 3) {
        const operations = this.computeDiff(linesA, linesB);
        const unified = [];
        let lineNumA = 1;
        let lineNumB = 1;
        let i = 0;
        
        while (i < operations.length) {
            // Find a block of changes with context
            const blockStart = Math.max(0, i - contextLines);
            let blockEnd = i;
            let changeCount = 0;
            
            // Find the end of the change block
            while (blockEnd < operations.length && changeCount < contextLines * 2) {
                if (operations[blockEnd].type !== 'equal') {
                    changeCount = 0;
                } else {
                    changeCount++;
                }
                blockEnd++;
            }
            
            // Ensure we include all changes
            while (blockEnd < operations.length && operations[blockEnd].type !== 'equal') {
                blockEnd++;
            }
            
            // Calculate line numbers for hunk header
            let startA = lineNumA;
            let startB = lineNumB;
            let countA = 0;
            let countB = 0;
            
            // Render the block
            unified.push(`@@ -${startA},? +${startB},? @@`);
            
            for (let j = blockStart; j < blockEnd && j < operations.length; j++) {
                const op = operations[j];
                
                switch (op.type) {
                    case 'equal':
                        unified.push(` ${op.contentA}`);
                        lineNumA++;
                        lineNumB++;
                        countA++;
                        countB++;
                        break;
                    case 'delete':
                        unified.push(`-${op.contentA}`);
                        lineNumA++;
                        countA++;
                        break;
                    case 'insert':
                        unified.push(`+${op.contentB}`);
                        lineNumB++;
                        countB++;
                        break;
                    case 'modify':
                        unified.push(`-${op.contentA}`);
                        unified.push(`+${op.contentB}`);
                        lineNumA++;
                        lineNumB++;
                        countA++;
                        countB++;
                        break;
                }
            }
            
            // Update the hunk header with correct counts
            const hunkIndex = unified.length - (blockEnd - blockStart) - 1;
            unified[hunkIndex] = `@@ -${startA},${countA} +${startB},${countB} @@`;
            
            i = blockEnd;
        }
        
        return unified;
    }

    /**
     * Get line statistics summary
     * 
     * @param {string[]} linesA - First array of lines
     * @param {string[]} linesB - Second array of lines
     * @returns {Object} Statistics about the diff
     */
    getDiffStats(linesA, linesB) {
        const operations = this.computeDiff(linesA, linesB);
        let additions = 0;
        let deletions = 0;
        let modifications = 0;
        
        for (const op of operations) {
            switch (op.type) {
                case 'insert':
                    additions++;
                    break;
                case 'delete':
                    deletions++;
                    break;
                case 'modify':
                    modifications++;
                    additions++;
                    deletions++;
                    break;
            }
        }
        
        return {
            additions,
            deletions,
            modifications,
            totalChanges: additions + deletions,
            linesA: linesA.length,
            linesB: linesB.length
        };
    }

    /**
     * Check if jsdiff is available
     * 
     * @returns {boolean} True if jsdiff is loaded
     */
    isAvailable() {
        return typeof this.diffLib !== 'undefined' && this.diffLib && typeof this.diffLib.diffLines === 'function';
    }

    /**
     * Fallback diff for when jsdiff is not available
     * Simple line-by-line comparison (less accurate but works)
     * 
     * @param {string[]} linesA - First array of lines
     * @param {string[]} linesB - Second array of lines
     * @returns {Object[]} Simple diff operations
     */
    fallbackDiff(linesA, linesB) {
        const operations = [];
        let i = 0, j = 0;
        
        while (i < linesA.length || j < linesB.length) {
            if (i >= linesA.length) {
                // All remaining lines are insertions
                operations.push({
                    type: 'insert',
                    lineA: null,
                    contentA: null,
                    lineB: j + 1,
                    contentB: linesB[j]
                });
                j++;
            } else if (j >= linesB.length) {
                // All remaining lines are deletions
                operations.push({
                    type: 'delete',
                    lineA: i + 1,
                    contentA: linesA[i],
                    lineB: null,
                    contentB: null
                });
                i++;
            } else if (linesA[i] === linesB[j]) {
                // Lines match
                operations.push({
                    type: 'equal',
                    lineA: i + 1,
                    contentA: linesA[i],
                    lineB: j + 1,
                    contentB: linesB[j]
                });
                i++;
                j++;
            } else {
                // Lines differ - try to find matching lines ahead
                let foundInA = -1;
                let foundInB = -1;
                
                // Look ahead in A for current B line
                for (let k = 1; k <= 5 && i + k < linesA.length; k++) {
                    if (linesA[i + k] === linesB[j]) {
                        foundInA = i + k;
                        break;
                    }
                }
                
                // Look ahead in B for current A line
                for (let k = 1; k <= 5 && j + k < linesB.length; k++) {
                    if (linesA[i] === linesB[j + k]) {
                        foundInB = j + k;
                        break;
                    }
                }
                
                if (foundInA !== -1 && foundInA - i <= 3) {
                    // Missing lines in A (deletions)
                    for (let k = i; k < foundInA; k++) {
                        operations.push({
                            type: 'delete',
                            lineA: k + 1,
                            contentA: linesA[k],
                            lineB: null,
                            contentB: null
                        });
                    }
                    i = foundInA;
                } else if (foundInB !== -1 && foundInB - j <= 3) {
                    // Missing lines in B (insertions)
                    for (let k = j; k < foundInB; k++) {
                        operations.push({
                            type: 'insert',
                            lineA: null,
                            contentA: null,
                            lineB: k + 1,
                            contentB: linesB[k]
                        });
                    }
                    j = foundInB;
                } else {
                    // Treat as modification
                    operations.push({
                        type: 'modify',
                        lineA: i + 1,
                        contentA: linesA[i],
                        lineB: j + 1,
                        contentB: linesB[j]
                    });
                    i++;
                    j++;
                }
            }
        }
        
        return operations;
    }

    /**
     * Main entry point for diff computation
     * Automatically uses jsdiff if available, falls back to simple diff
     * 
     * @param {string[]} linesA - First array of lines
     * @param {string[]} linesB - Second array of lines
     * @returns {Object[]} Array of diff operations
     */
    compute(linesA, linesB) {
        if (this.isAvailable()) {
            return this.computeDiff(linesA, linesB);
        } else {
            console.warn('[DiffEngine] jsdiff not available, using fallback diff');
            return this.fallbackDiff(linesA, linesB);
        }
    }
}

// Create singleton instance
window.DiffEngine = new DiffEngine();