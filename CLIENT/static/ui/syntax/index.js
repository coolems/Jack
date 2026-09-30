/**
 * syntax/index.js - Syntax Highlighter Registry
 * Auto-discovers and manages all language-specific highlighters
 * 
 * To add a new highlighter:
 * 1. Create a new file in this folder (e.g., ruby.js)
 * 2. Define a class named RubyHighlighter extending BaseHighlighter
 * 3. The registry will auto-discover it on next page load
 */

class SyntaxRegistry {
    constructor() {
        this.highlighters = new Map();
        this.extensionMap = new Map();
        this.initialized = false;
    }
    
    /**
     * Initialize the registry - auto-discover all highlighters
     * Called once on page load
     */
    init() {
        if (this.initialized) return;
        
        // List of highlighter classes to register
        // These are defined in separate files loaded before this one
        const highlighterClasses = [
            window.TextHighlighter,
            window.PythonHighlighter,
            window.JavaHighlighter,
            window.CppHighlighter,
            window.HtmlHighlighter
        ];
        
        for (const HighlighterClass of highlighterClasses) {
            if (HighlighterClass) {
                try {
                    const instance = new HighlighterClass();
                    this.register(instance);
                } catch (e) {
                    console.warn(`[SyntaxRegistry] Failed to register highlighter:`, e);
                }
            }
        }
        
        this.initialized = true;
        console.log(`[SyntaxRegistry] Registered ${this.highlighters.size} highlighters with ${this.extensionMap.size} extensions`);
    }
    
    /**
     * Register a highlighter instance
     */
    register(highlighter) {
        this.highlighters.set(highlighter.name, highlighter);
        
        for (const ext of highlighter.extensions) {
            this.extensionMap.set(ext, highlighter.name);
        }
        
        console.log(`[SyntaxRegistry] Registered ${highlighter.name} (${highlighter.extensions.join(', ')})`);
    }
    
    /**
     * Get highlighter for a filename
     */
    getHighlighterForFile(filename) {
        const ext = this.getExtension(filename);
        const highlighterName = this.extensionMap.get(ext);
        
        if (highlighterName) {
            return this.highlighters.get(highlighterName);
        }
        
        // Fallback to text highlighter
        return this.highlighters.get('text');
    }
    
    /**
     * Get file extension from filename
     */
    getExtension(filename) {
        const lastDot = filename.lastIndexOf('.');
        if (lastDot === -1) return '';
        return filename.slice(lastDot + 1).toLowerCase();
    }
    
    /**
     * Highlight a file's content
     */
    highlight(content, filename, schema) {
        const highlighter = this.getHighlighterForFile(filename);
        if (!highlighter) {
            // Fallback: just escape HTML
            const div = document.createElement('div');
            div.textContent = content;
            return div.innerHTML;
        }
        
        return highlighter.highlight(content, schema);
    }
    
    /**
     * Get all registered highlighter names
     */
    getHighlighterNames() {
        return Array.from(this.highlighters.keys());
    }
    
    /**
     * Get supported extensions
     */
    getSupportedExtensions() {
        return Array.from(this.extensionMap.keys());
    }
}

// Singleton instance
window.SyntaxRegistry = new SyntaxRegistry();

// Auto-initialize after all scripts load
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => window.SyntaxRegistry.init());
} else {
    window.SyntaxRegistry.init();
}