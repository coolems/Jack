/**
 * syntax/schema_manager.js - Color schema management
 * Handles schema definitions, switching, and persistence
 */

class SchemaManager {
    constructor() {
        this.schemas = {};
        this.currentSchema = null;
        this.listeners = [];
        this.storageKey = 'coolems_syntax_schema';
        this.initSchemas();
        this.loadSavedSchema();
    }
    
    initSchemas() {
        // Monokai Pro (Default)
        this.schemas.monokai = {
            name: 'Monokai Pro',
            background: '#2d2a2e',
            text: '#fcfcfa',
            keyword: '#ff6188',
            string: '#fc9867',
            comment: '#727072',
            number: '#78dce8',
            operator: '#ff6188',
            function: '#ffd866',
            class: '#a9dc76',
            type: '#78dce8',
            variable: '#fcfcfa',
            preprocessor: '#ff6188',
            annotation: '#ab9df2',
            tag: '#ff6188',
            attribute: '#78dce8',
            punctuation: '#fcfcfa'
        };
        
        // Solarized Dark
        this.schemas.solarized = {
            name: 'Solarized Dark',
            background: '#002b36',
            text: '#839496',
            keyword: '#719e07',
            string: '#2aa198',
            comment: '#586e75',
            number: '#d33682',
            operator: '#93a1a1',
            function: '#268bd2',
            class: '#b58900',
            type: '#268bd2',
            variable: '#839496',
            preprocessor: '#cb4b16',
            annotation: '#6c71c4',
            tag: '#93a1a1',
            attribute: '#2aa198',
            punctuation: '#839496'
        };
        
        // GitHub Dark
        this.schemas.github = {
            name: 'GitHub Dark',
            background: '#0d1117',
            text: '#c9d1d9',
            keyword: '#ff7b72',
            string: '#a5d6ff',
            comment: '#8b949e',
            number: '#79c0ff',
            operator: '#ff7b72',
            function: '#d2a8ff',
            class: '#ffa657',
            type: '#ffa657',
            variable: '#c9d1d9',
            preprocessor: '#ff7b72',
            annotation: '#d2a8ff',
            tag: '#7ee787',
            attribute: '#79c0ff',
            punctuation: '#c9d1d9'
        };
        
        // One Dark Pro
        this.schemas.onedark = {
            name: 'One Dark Pro',
            background: '#282c34',
            text: '#abb2bf',
            keyword: '#c678dd',
            string: '#98c379',
            comment: '#5c6370',
            number: '#d19a66',
            operator: '#c678dd',
            function: '#61afef',
            class: '#e5c07b',
            type: '#e5c07b',
            variable: '#e06c75',
            preprocessor: '#c678dd',
            annotation: '#61afef',
            tag: '#e06c75',
            attribute: '#d19a66',
            punctuation: '#abb2bf'
        };
        
        // Dracula
        this.schemas.dracula = {
            name: 'Dracula',
            background: '#282a36',
            text: '#f8f8f2',
            keyword: '#ff79c6',
            string: '#f1fa8c',
            comment: '#6272a4',
            number: '#bd93f9',
            operator: '#ff79c6',
            function: '#50fa7b',
            class: '#8be9fd',
            type: '#8be9fd',
            variable: '#f8f8f2',
            preprocessor: '#ff79c6',
            annotation: '#bd93f9',
            tag: '#ff79c6',
            attribute: '#50fa7b',
            punctuation: '#f8f8f2'
        };
    }
    
    loadSavedSchema() {
        const saved = localStorage.getItem(this.storageKey);
        if (saved && this.schemas[saved]) {
            this.currentSchema = saved;
        } else {
            this.currentSchema = 'monokai';
        }
        this.applySchemaToDocument();
    }
    
    saveSchema(schemaName) {
        localStorage.setItem(this.storageKey, schemaName);
    }
    
    getCurrentSchema() {
        return this.schemas[this.currentSchema];
    }
    
    getSchemaNames() {
        return Object.keys(this.schemas);
    }
    
    getSchemaInfo() {
        return Object.entries(this.schemas).map(([id, schema]) => ({
            id: id,
            name: schema.name
        }));
    }
    
    setSchema(schemaName) {
        if (this.schemas[schemaName]) {
            this.currentSchema = schemaName;
            this.saveSchema(schemaName);
            this.applySchemaToDocument();
            this.notifyListeners();
            return true;
        }
        return false;
    }
    
    applySchemaToDocument() {
        const schema = this.getCurrentSchema();
        if (!schema) return;
        
        // Apply background to preview content
        const previewContent = document.getElementById('previewContent');
        if (previewContent) {
            previewContent.style.backgroundColor = schema.background;
            previewContent.style.color = schema.text;
        }
        
        // Apply to diff containers if they exist
        const diffLeft = document.getElementById('diffContentLeft');
        const diffRight = document.getElementById('diffContentRight');
        if (diffLeft) diffLeft.style.backgroundColor = schema.background;
        if (diffRight) diffRight.style.backgroundColor = schema.background;
    }
    
    addListener(callback) {
        this.listeners.push(callback);
    }
    
    removeListener(callback) {
        const index = this.listeners.indexOf(callback);
        if (index > -1) this.listeners.splice(index, 1);
    }
    
    notifyListeners() {
        this.listeners.forEach(callback => callback(this.getCurrentSchema()));
    }
    
    getSchemaCSS(schemaId) {
        const schema = this.schemas[schemaId];
        if (!schema) return '';
        
        return `
            background-color: ${schema.background};
            color: ${schema.text};
        `;
    }
}

// Singleton instance
window.SchemaManager = new SchemaManager();