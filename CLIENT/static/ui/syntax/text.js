/**
 * syntax/text.js - Plain text highlighter
 * Fallback for unknown file types, preserves original formatting
 */

class TextHighlighter extends BaseHighlighter {
    constructor() {
        super();
        this.name = 'text';
        this.extensions = [
            'txt', 'log', 'csv', 'tsv', 'md', 'markdown',
            'sql', 'sh', 'bash', 'zsh', 'ps1', 'bat', 'cmd',
            'conf', 'cfg', 'ini', 'env', 'lock'
        ];
    }
    
    highlightLine(line, schema) {
        // No syntax highlighting, just escape HTML
        return this.escapeHtml(line);
    }
}

// Register with global registry
if (typeof window !== 'undefined') {
    window.TextHighlighter = TextHighlighter;
}