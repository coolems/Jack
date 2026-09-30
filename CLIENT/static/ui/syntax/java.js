/**
 * syntax/java.js - Java syntax highlighter
 * Highlights keywords, annotations, generics, strings, comments
 */

class JavaHighlighter extends BaseHighlighter {
    constructor() {
        super();
        this.name = 'java';
        this.extensions = ['java', 'kt', 'scala', 'groovy'];
        
        // Java keywords
        this.keywords = new Set([
            'abstract', 'assert', 'boolean', 'break', 'byte', 'case', 'catch',
            'char', 'class', 'const', 'continue', 'default', 'do', 'double',
            'else', 'enum', 'extends', 'final', 'finally', 'float', 'for',
            'goto', 'if', 'implements', 'import', 'instanceof', 'int', 'interface',
            'long', 'native', 'new', 'package', 'private', 'protected', 'public',
            'return', 'short', 'static', 'strictfp', 'super', 'switch', 'synchronized',
            'this', 'throw', 'throws', 'transient', 'try', 'void', 'volatile', 'while',
            'true', 'false', 'null'
        ]);
        
        // Primitive types
        this.primitives = new Set([
            'boolean', 'byte', 'char', 'double', 'float', 'int', 'long', 'short', 'void'
        ]);
    }
    
    highlightLine(line, schema) {
        let result = '';
        let i = 0;
        let inString = false;
        let stringChar = '';
        let inAnnotation = false;
        
        while (i < line.length) {
            const char = line[i];
            
            // Handle strings
            if ((char === '"' || char === "'") && !inString) {
                inString = true;
                stringChar = char;
                result += this.wrapToken(this.escapeHtml(char), 'string', schema);
                i++;
                continue;
            }
            
            if (inString && char === stringChar && line[i-1] !== '\\') {
                inString = false;
                result += this.wrapToken(this.escapeHtml(char), 'string', schema);
                i++;
                continue;
            }
            
            if (inString) {
                result += this.wrapToken(this.escapeHtml(char), 'string', schema);
                i++;
                continue;
            }
            
            // Handle single-line comments
            if (char === '/' && line[i+1] === '/') {
                const comment = this.escapeHtml(line.slice(i));
                result += this.wrapToken(comment, 'comment', schema);
                break;
            }
            
            // Handle multi-line comments
            if (char === '/' && line[i+1] === '*') {
                const commentMatch = this.extractComment(line, i);
                result += this.wrapToken(this.escapeHtml(commentMatch.text), 'comment', schema);
                i = commentMatch.nextIndex;
                continue;
            }
            
            // Handle annotations
            if (char === '@') {
                const annotationMatch = this.extractAnnotation(line, i);
                result += this.wrapToken(this.escapeHtml(annotationMatch.text), 'annotation', schema);
                i = annotationMatch.nextIndex;
                continue;
            }
            
            // Handle generics
            if (char === '<') {
                const genericMatch = this.extractGeneric(line, i);
                result += this.wrapToken(this.escapeHtml(genericMatch.text), 'type', schema);
                i = genericMatch.nextIndex;
                continue;
            }
            
            // Handle numbers
            if (this.isDigit(char) || (char === '-' && i + 1 < line.length && this.isDigit(line[i + 1]))) {
                const numMatch = this.extractNumber(line, i);
                result += this.wrapToken(this.escapeHtml(numMatch.text), 'number', schema);
                i = numMatch.nextIndex;
                continue;
            }
            
            // Handle words (keywords, types, identifiers)
            if (this.isLetter(char) || char === '_') {
                const wordMatch = this.extractWord(line, i);
                const word = wordMatch.text;
                let tokenType = 'variable';
                
                if (this.keywords.has(word)) {
                    tokenType = 'keyword';
                } else if (this.primitives.has(word)) {
                    tokenType = 'type';
                } else if (word.length > 0 && word[0] === word[0].toUpperCase() && word[0] !== word[0].toLowerCase()) {
                    tokenType = 'class';
                }
                
                result += this.wrapToken(this.escapeHtml(word), tokenType, schema);
                i = wordMatch.nextIndex;
                continue;
            }
            
            // Handle operators
            if (this.isOperator(char)) {
                const opMatch = this.extractOperator(line, i);
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
    
    extractAnnotation(line, start) {
        let end = start;
        while (end < line.length && (this.isLetter(line[end]) || this.isDigit(line[end]) || line[end] === '_')) {
            end++;
        }
        
        // Handle annotation with parameters
        if (end < line.length && line[end] === '(') {
            let parenCount = 1;
            end++;
            while (end < line.length && parenCount > 0) {
                if (line[end] === '(') parenCount++;
                if (line[end] === ')') parenCount--;
                end++;
            }
        }
        
        return {
            text: line.slice(start, end),
            nextIndex: end
        };
    }
    
    extractGeneric(line, start) {
        let end = start;
        let angleCount = 1;
        end++;
        
        while (end < line.length && angleCount > 0) {
            if (line[end] === '<') angleCount++;
            if (line[end] === '>') angleCount--;
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
        
        if (line[end] === '-') end++;
        
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
    
    extractWord(line, start) {
        let end = start;
        while (end < line.length && (this.isLetter(line[end]) || this.isDigit(line[end]) || line[end] === '_')) {
            end++;
        }
        return {
            text: line.slice(start, end),
            nextIndex: end
        };
    }
    
    extractOperator(line, start) {
        const operators = ['==', '!=', '<=', '>=', '+=', '-=', '*=', '/=', '%=', '&&', '||', '++', '--', '->', '::'];
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
    
    isOperator(char) {
        const operators = new Set(['+', '-', '*', '/', '%', '=', '!', '<', '>', '&', '|', '^', '~', '?', ':', '.']);
        return operators.has(char);
    }
}

// Register with global registry
if (typeof window !== 'undefined') {
    window.JavaHighlighter = JavaHighlighter;
}