/**
 * syntax/html.js - HTML/CSS/JS syntax highlighter
 * Handles HTML tags, attributes, CSS properties, and JavaScript
 */

class HtmlHighlighter extends BaseHighlighter {
    constructor() {
        super();
        this.name = 'html';
        this.extensions = [
            'html', 'htm', 'css', 'scss', 'sass', 'less',
            'js', 'jsx', 'ts', 'tsx', 'json', 'xml', 'yaml', 'yml'
        ];
        
        // HTML tags
        this.htmlTags = new Set([
            'html', 'head', 'body', 'div', 'span', 'p', 'a', 'img', 'ul', 'ol',
            'li', 'table', 'tr', 'td', 'th', 'form', 'input', 'button', 'select',
            'textarea', 'label', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'header',
            'footer', 'nav', 'section', 'article', 'aside', 'main', 'script',
            'style', 'link', 'meta', 'title', 'br', 'hr', 'strong', 'em', 'b',
            'i', 'code', 'pre', 'blockquote', 'canvas', 'svg', 'video', 'audio'
        ]);
        
        // CSS properties
        this.cssProperties = new Set([
            'color', 'background', 'background-color', 'margin', 'padding',
            'border', 'width', 'height', 'display', 'position', 'top', 'left',
            'right', 'bottom', 'font', 'font-size', 'font-family', 'text-align',
            'text-decoration', 'line-height', 'opacity', 'z-index', 'overflow',
            'flex', 'grid', 'gap', 'align-items', 'justify-content'
        ]);
        
        // JavaScript keywords
        this.jsKeywords = new Set([
            'function', 'const', 'let', 'var', 'if', 'else', 'for', 'while',
            'return', 'class', 'extends', 'super', 'this', 'new', 'delete',
            'typeof', 'instanceof', 'in', 'of', 'switch', 'case', 'break',
            'continue', 'try', 'catch', 'finally', 'throw', 'import', 'export',
            'default', 'async', 'await', 'static', 'get', 'set', 'yield'
        ]);
    }
    
    highlightLine(line, schema) {
        // Detect content type based on extension or content
        const contentType = this.detectContentType(line);
        
        switch (contentType) {
            case 'css':
                return this.highlightCSS(line, schema);
            case 'js':
                return this.highlightJS(line, schema);
            case 'html':
            default:
                return this.highlightHTML(line, schema);
        }
    }
    
    detectContentType(line) {
        // This is called per line - in practice we'd detect based on file extension
        // For simplicity, we'll return 'html' and let the HTML highlighter handle it
        // The actual detection happens in the main PreviewManager based on file extension
        return 'html';
    }
    
    highlightHTML(line, schema) {
        let result = '';
        let i = 0;
        let inTag = false;
        let inString = false;
        let stringChar = '';
        let tagBuffer = '';
        
        while (i < line.length) {
            const char = line[i];
            
            // Handle strings inside attributes
            if (inString) {
                if (char === stringChar && line[i-1] !== '\\') {
                    inString = false;
                    result += this.wrapToken(this.escapeHtml(char), 'string', schema);
                } else {
                    result += this.wrapToken(this.escapeHtml(char), 'string', schema);
                }
                i++;
                continue;
            }
            
            // Handle tag opening
            if (char === '<' && !inTag) {
                if (result) {
                    // Flush any text outside tags
                    const textPart = result;
                    result = this.wrapToken(textPart, 'text', schema);
                }
                result = '';
                inTag = true;
                tagBuffer = '';
                result += this.wrapToken(this.escapeHtml(char), 'tag', schema);
                i++;
                continue;
            }
            
            // Handle tag closing
            if (char === '>' && inTag) {
                result += this.wrapToken(this.escapeHtml(char), 'tag', schema);
                inTag = false;
                i++;
                continue;
            }
            
            // Handle tag name
            if (inTag && !inString) {
                // Check if we're at a tag name
                if (char === '/' || char === '!' || char === '?') {
                    result += this.wrapToken(this.escapeHtml(char), 'punctuation', schema);
                    i++;
                    continue;
                }
                
                // Extract tag name
                const tagMatch = this.extractWord(line, i);
                if (tagMatch.text && this.htmlTags.has(tagMatch.text.toLowerCase())) {
                    result += this.wrapToken(this.escapeHtml(tagMatch.text), 'tag', schema);
                    i = tagMatch.nextIndex;
                    continue;
                }
                
                // Handle attributes
                if (this.isLetter(char) || char === '-') {
                    const attrMatch = this.extractWord(line, i);
                    let tokenType = 'attribute';
                    if (attrMatch.text === 'class' || attrMatch.text === 'id' || attrMatch.text === 'style') {
                        tokenType = 'keyword';
                    }
                    result += this.wrapToken(this.escapeHtml(attrMatch.text), tokenType, schema);
                    i = attrMatch.nextIndex;
                    continue;
                }
                
                // Handle equals sign
                if (char === '=') {
                    result += this.wrapToken(this.escapeHtml(char), 'operator', schema);
                    i++;
                    continue;
                }
                
                // Handle string start in attributes
                if (char === '"' || char === "'") {
                    inString = true;
                    stringChar = char;
                    result += this.wrapToken(this.escapeHtml(char), 'string', schema);
                    i++;
                    continue;
                }
            }
            
            // Regular character
            result += this.escapeHtml(char);
            i++;
        }
        
        return result;
    }
    
    highlightCSS(line, schema) {
        let result = '';
        let i = 0;
        
        while (i < line.length) {
            const char = line[i];
            
            // Handle comments
            if (char === '/' && line[i+1] === '*') {
                const commentMatch = this.extractComment(line, i);
                result += this.wrapToken(this.escapeHtml(commentMatch.text), 'comment', schema);
                i = commentMatch.nextIndex;
                continue;
            }
            
            // Handle selectors (class, id, element)
            if (char === '.' || char === '#' || this.isLetter(char)) {
                const wordMatch = this.extractWord(line, i);
                const word = wordMatch.text;
                let tokenType = 'class';
                
                if (word[0] === '#') {
                    tokenType = 'keyword';
                } else if (word[0] === '.') {
                    tokenType = 'function';
                } else if (this.cssProperties.has(word)) {
                    tokenType = 'keyword';
                } else {
                    tokenType = 'type';
                }
                
                result += this.wrapToken(this.escapeHtml(word), tokenType, schema);
                i = wordMatch.nextIndex;
                continue;
            }
            
            // Handle numbers and units
            if (this.isDigit(char)) {
                const numMatch = this.extractNumberWithUnit(line, i);
                result += this.wrapToken(this.escapeHtml(numMatch.text), 'number', schema);
                i = numMatch.nextIndex;
                continue;
            }
            
            // Handle operators
            if (char === ':' || char === ';' || char === '{' || char === '}' || char === ',') {
                result += this.wrapToken(this.escapeHtml(char), 'operator', schema);
                i++;
                continue;
            }
            
            result += this.escapeHtml(char);
            i++;
        }
        
        return result;
    }
    
    highlightJS(line, schema) {
        let result = '';
        let i = 0;
        let inString = false;
        let stringChar = '';
        let inRegex = false;
        
        while (i < line.length) {
            const char = line[i];
            
            // Handle strings
            if ((char === '"' || char === "'" || char === '`') && !inRegex) {
                if (!inString) {
                    inString = true;
                    stringChar = char;
                    result += this.wrapToken(this.escapeHtml(char), 'string', schema);
                } else if (char === stringChar) {
                    inString = false;
                    result += this.wrapToken(this.escapeHtml(char), 'string', schema);
                } else {
                    result += this.wrapToken(this.escapeHtml(char), 'string', schema);
                }
                i++;
                continue;
            }
            
            // Handle single-line comments
            if (char === '/' && line[i+1] === '/' && !inString) {
                const comment = this.escapeHtml(line.slice(i));
                result += this.wrapToken(comment, 'comment', schema);
                break;
            }
            
            // Handle multi-line comments
            if (char === '/' && line[i+1] === '*' && !inString) {
                const commentMatch = this.extractComment(line, i);
                result += this.wrapToken(this.escapeHtml(commentMatch.text), 'comment', schema);
                i = commentMatch.nextIndex;
                continue;
            }
            
            // Handle keywords and identifiers
            if (this.isLetter(char) || char === '_' || char === '$') {
                const wordMatch = this.extractWord(line, i);
                const word = wordMatch.text;
                let tokenType = 'variable';
                
                if (this.jsKeywords.has(word)) {
                    tokenType = 'keyword';
                } else if (word.match(/^[A-Z]/)) {
                    tokenType = 'class';
                }
                
                result += this.wrapToken(this.escapeHtml(word), tokenType, schema);
                i = wordMatch.nextIndex;
                continue;
            }
            
            // Handle numbers
            if (this.isDigit(char)) {
                const numMatch = this.extractNumber(line, i);
                result += this.wrapToken(this.escapeHtml(numMatch.text), 'number', schema);
                i = numMatch.nextIndex;
                continue;
            }
            
            // Handle operators
            if (this.isJSOperator(char)) {
                const opMatch = this.extractJSOperator(line, i);
                result += this.wrapToken(this.escapeHtml(opMatch.text), 'operator', schema);
                i = opMatch.nextIndex;
                continue;
            }
            
            result += this.escapeHtml(char);
            i++;
        }
        
        return result;
    }
    
    extractComment(line, start) {
        let end = start + 2;
        while (end < line.length && !(line[end-1] === '*' && line[end] === '/')) {
            end++;
        }
        end++;
        return {
            text: line.slice(start, end),
            nextIndex: end
        };
    }
    
    extractWord(line, start) {
        let end = start;
        while (end < line.length && (this.isLetter(line[end]) || this.isDigit(line[end]) || line[end] === '_' || line[end] === '-' || line[end] === '.')) {
            end++;
        }
        return {
            text: line.slice(start, end),
            nextIndex: end
        };
    }
    
    extractNumber(line, start) {
        let end = start;
        let hasDecimal = false;
        
        while (end < line.length) {
            const char = line[end];
            if (this.isDigit(char)) {
                end++;
            } else if (char === '.' && !hasDecimal) {
                hasDecimal = true;
                end++;
            } else {
                break;
            }
        }
        
        return {
            text: line.slice(start, end),
            nextIndex: end
        };
    }
    
    extractNumberWithUnit(line, start) {
        let end = start;
        
        while (end < line.length && (this.isDigit(line[end]) || line[end] === '.')) {
            end++;
        }
        
        // Extract unit (px, em, rem, %, vh, vw)
        while (end < line.length && this.isLetter(line[end])) {
            end++;
        }
        
        return {
            text: line.slice(start, end),
            nextIndex: end
        };
    }
    
    extractJSOperator(line, start) {
        const operators = ['===', '!==', '==', '!=', '<=', '>=', '+=', '-=', '*=', '/=', '%=', '&&', '||', '++', '--', '??'];
        let end = start + 1;
        
        if (end < line.length) {
            const twoChar = line.slice(start, end + 1);
            if (operators.includes(twoChar)) {
                end++;
            }
        }
        
        return {
            text: line.slice(start, end),
            nextIndex: end
        };
    }
    
    isDigit(char) {
        return char >= '0' && char <= '9';
    }
    
    isLetter(char) {
        return (char >= 'a' && char <= 'z') || (char >= 'A' && char <= 'Z');
    }
    
    isJSOperator(char) {
        const operators = new Set(['+', '-', '*', '/', '%', '=', '!', '<', '>', '&', '|', '?']);
        return operators.has(char);
    }
}

// Register with global registry
if (typeof window !== 'undefined') {
    window.HtmlHighlighter = HtmlHighlighter;
}