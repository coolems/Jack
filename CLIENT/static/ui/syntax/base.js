/**
 * syntax/base.js - Base highlighter class
 * Provides common utilities and abstract methods for language-specific highlighters
 */

class BaseHighlighter {
    constructor() {
        this.name = 'base';
        this.extensions = [];
    }

    /**
     * Escape HTML special characters
     */
    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    /**
     * Apply highlighting to a line of code
     * Override in child classes
     */
    highlightLine(line, schema) {
        return this.escapeHtml(line);
    }

    /**
     * Highlight entire code block
     * Each line is wrapped in a <div> for proper line break rendering
     * and alignment with line numbers
     */
    highlight(code, schema) {
        const lines = code.split('\n');
        // BUG FIX (2026-07-15): an empty source line must still occupy one full row.
        // <div></div> has height 0, which collapsed blank lines and made the gutter
        // numbers drift out of alignment with the text after the first blank line.
        const highlighted = lines.map(line => {
            const inner = line.length > 0 ? this.highlightLine(line, schema) : '&nbsp;';
            return `<div>${inner}</div>`;
        });
        return highlighted.join('');
    }

    /**
     * Check if this highlighter supports a file extension
     */
    supportsExtension(ext) {
        return this.extensions.includes(ext.toLowerCase());
    }

    /**
     * Token wrapper with schema-aware coloring
     */
    wrapToken(text, tokenType, schema) {
        const color = schema[tokenType] || schema.text;
        return `<span style="color: ${color}">${text}</span>`;
    }

    /**
     * Simple regex-based tokenization helper
     * Override for complex tokenization
     */
    tokenizeLine(line, schema) {
        return this.escapeHtml(line);
    }
}

// Export for module system
if (typeof module !== 'undefined' && module.exports) {
    module.exports = { BaseHighlighter };
}
