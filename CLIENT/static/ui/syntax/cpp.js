/**
 * syntax/cpp.js - C/C++ syntax highlighter
 * Highlights keywords, preprocessor directives, types, strings, comments
 */

class CppHighlighter extends BaseHighlighter {
    constructor() {
        super();
        this.name = 'cpp';
        this.extensions = ['c', 'cpp', 'cc', 'cxx', 'h', 'hpp', 'hxx', 'ino'];
        
        // C/C++ keywords
        this.keywords = new Set([
            'auto', 'break', 'case', 'char', 'const', 'continue', 'default', 'do',
            'double', 'else', 'enum', 'extern', 'float', 'for', 'goto', 'if',
            'int', 'long', 'register', 'return', 'short', 'signed', 'sizeof',
            'static', 'struct', 'switch', 'typedef', 'union', 'unsigned', 'void',
            'volatile', 'while', 'alignas', 'alignof', 'and', 'and_eq', 'asm',
            'bitand', 'bitor', 'bool', 'catch', 'class', 'compl', 'constexpr',
            'const_cast', 'decltype', 'delete', 'dynamic_cast', 'explicit',
            'export', 'false', 'friend', 'inline', 'mutable', 'namespace',
            'new', 'noexcept', 'not', 'not_eq', 'nullptr', 'operator', 'or',
            'or_eq', 'private', 'protected', 'public', 'reinterpret_cast',
            'static_assert', 'static_cast', 'template', 'this', 'throw', 'true',
            'try', 'typeid', 'typename', 'using', 'virtual', 'xor', 'xor_eq'
        ]);
        
        // Common types
        this.types = new Set([
            'int8_t', 'int16_t', 'int32_t', 'int64_t', 'uint8_t', 'uint16_t',
            'uint32_t', 'uint64_t', 'size_t', 'ptrdiff_t', 'wchar_t', 'char16_t',
            'char32_t', 'string', 'vector', 'list', 'map', 'set', 'queue',
            'stack', 'pair', 'unique_ptr', 'shared_ptr', 'weak_ptr'
        ]);
    }
    
    highlightLine(line, schema) {
        let result = '';
        let i = 0;
        let inString = false;
        let stringChar = '';
        let inChar = false;
        
        while (i < line.length) {
            const char = line[i];
            
            // Handle preprocessor directives
            if (char === '#' && i === 0) {
                const preprocMatch = this.extractPreprocessor(line, i);
                result += this.wrapToken(this.escapeHtml(preprocMatch.text), 'preprocessor', schema);
                break;
            }
            
            // Handle strings
            if (char === '"' && !inString && !inChar) {
                inString = true;
                stringChar = '"';
                result += this.wrapToken(this.escapeHtml(char), 'string', schema);
                i++;
                continue;
            }
            
            if (inString && char === '"' && line[i-1] !== '\\') {
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
            
            // Handle character literals
            if (char === "'" && !inChar && !inString) {
                inChar = true;
                result += this.wrapToken(this.escapeHtml(char), 'string', schema);
                i++;
                continue;
            }
            
            if (inChar && char === "'" && line[i-1] !== '\\') {
                inChar = false;
                result += this.wrapToken(this.escapeHtml(char), 'string', schema);
                i++;
                continue;
            }
            
            if (inChar) {
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
                } else if (this.types.has(word)) {
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
    
    extractPreprocessor(line, start) {
        let end = start;
        while (end < line.length) {
            // Preprocessor extends to end of line
            end++;
        }
        return {
            text: line.slice(start, end),
            nextIndex: end
        };
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
    
    extractNumber(line, start) {
        let end = start;
        let hasDecimal = false;
        let isHex = false;
        
        if (line[end] === '-') end++;
        
        // Check for hex (0x)
        if (end + 1 < line.length && line[end] === '0' && (line[end+1] === 'x' || line[end+1] === 'X')) {
            isHex = true;
            end += 2;
        }
        
        while (end < line.length) {
            const char = line[end];
            if (isHex) {
                if ((char >= '0' && char <= '9') || (char >= 'a' && char <= 'f') || (char >= 'A' && char <= 'F')) {
                    end++;
                } else {
                    break;
                }
            } else if (this.isDigit(char)) {
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
        const operators = ['==', '!=', '<=', '>=', '+=', '-=', '*=', '/=', '%=', '&=', '|=', '^=', '<<=', '>>=', '&&', '||', '++', '--', '->', '::', '<<', '>>'];
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
        const operators = new Set(['+', '-', '*', '/', '%', '=', '!', '<', '>', '&', '|', '^', '~', '?', ':', '.', ',', ';', '(', ')', '[', ']', '{', '}']);
        return operators.has(char);
    }
}

// Register with global registry
if (typeof window !== 'undefined') {
    window.CppHighlighter = CppHighlighter;
}