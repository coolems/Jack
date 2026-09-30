/**
 * syntax/python.js - Python syntax highlighter
 * Highlights keywords, built-ins, decorators, strings, comments
 * FIXED: Decorator infinite loop, triple-quote strings, f-strings
 */

class PythonHighlighter extends BaseHighlighter {
    constructor() {
        super();
        this.name = 'python';
        this.extensions = ['py', 'pyw', 'pyi'];
        
        // Python keywords
        this.keywords = new Set([
            'and', 'as', 'assert', 'async', 'await', 'break', 'class', 'continue',
            'def', 'del', 'elif', 'else', 'except', 'finally', 'for', 'from',
            'global', 'if', 'import', 'in', 'is', 'lambda', 'nonlocal', 'not',
            'or', 'pass', 'raise', 'return', 'try', 'while', 'with', 'yield',
            'False', 'None', 'True', 'match', 'case'
        ]);
        
        // Built-in functions
        this.builtins = new Set([
            'abs', 'all', 'any', 'ascii', 'bin', 'bool', 'bytearray', 'bytes',
            'callable', 'chr', 'classmethod', 'compile', 'complex', 'delattr',
            'dict', 'dir', 'divmod', 'enumerate', 'eval', 'exec', 'filter',
            'float', 'format', 'frozenset', 'getattr', 'globals', 'hasattr',
            'hash', 'help', 'hex', 'id', 'input', 'int', 'isinstance', 'issubclass',
            'iter', 'len', 'list', 'locals', 'map', 'max', 'min', 'next', 'object',
            'oct', 'open', 'ord', 'pow', 'print', 'property', 'range', 'repr',
            'reversed', 'round', 'set', 'setattr', 'slice', 'sorted', 'staticmethod',
            'str', 'sum', 'super', 'tuple', 'type', 'vars', 'zip', '__import__'
        ]);
    }
    
    highlightLine(line, schema) {
        let result = '';
        let i = 0;
        const len = line.length;
        
        while (i < len) {
            const char = line[i];
            
            // Handle triple-quoted strings (single quote) '''
            if (char === "'" && i + 2 < len && line[i + 1] === "'" && line[i + 2] === "'") {
                const stringMatch = this.extractTripleString(line, i, "'", schema);
                result += stringMatch.text;
                i = stringMatch.nextIndex;
                continue;
            }
            
            // Handle triple-quoted strings (double quote) """
            if (char === '"' && i + 2 < len && line[i + 1] === '"' && line[i + 2] === '"') {
                const stringMatch = this.extractTripleString(line, i, '"', schema);
                result += stringMatch.text;
                i = stringMatch.nextIndex;
                continue;
            }
            
            // Handle strings (single quote)
            if (char === "'") {
                const stringMatch = this.extractString(line, i, "'", schema);
                result += stringMatch.text;
                i = stringMatch.nextIndex;
                continue;
            }
            
            // Handle strings (double quote)
            if (char === '"') {
                const stringMatch = this.extractString(line, i, '"', schema);
                result += stringMatch.text;
                i = stringMatch.nextIndex;
                continue;
            }
            
            // Handle comments
            if (char === '#') {
                const comment = this.escapeHtml(line.slice(i));
                result += this.wrapToken(comment, 'comment', schema);
                break;
            }
            
            // Handle decorators - FIXED: include @ in the extracted text
            if (char === '@' && (i === 0 || line[i - 1] === ' ' || line[i - 1] === '\n')) {
                const decoratorMatch = this.extractDecorator(line, i);
                result += this.wrapToken(decoratorMatch.text, 'annotation', schema);
                i = decoratorMatch.nextIndex;
                continue;
            }
            
            // Handle numbers - FIXED: only treat '-' as negative at start of expression
            if (this.isDigit(char)) {
                const numberMatch = this.extractNumber(line, i);
                result += this.wrapToken(numberMatch.text, 'number', schema);
                i = numberMatch.nextIndex;
                continue;
            }
            
            // Handle negative numbers only at start of line or after operator/opening paren
            if (char === '-' && i + 1 < len && this.isDigit(line[i + 1]) && 
                (i === 0 || '([{,=+*-/:;'.includes(line[i - 1]))) {
                const numberMatch = this.extractNumber(line, i);
                result += this.wrapToken(numberMatch.text, 'number', schema);
                i = numberMatch.nextIndex;
                continue;
            }
            
            // Handle words (keywords, builtins, identifiers)
            if (this.isLetter(char) || char === '_') {
                const wordMatch = this.extractWord(line, i);
                const word = wordMatch.text;
                let tokenType = 'variable';
                
                if (this.keywords.has(word)) {
                    tokenType = 'keyword';
                } else if (this.builtins.has(word)) {
                    tokenType = 'function';
                } else if (word.length > 2 && word[0] === '_' && word[word.length - 1] === '_') {
                    tokenType = 'keyword';  // dunder methods
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
            
            // Regular character
            result += this.escapeHtml(char);
            i++;
        }
        
        return result;
    }
    
    extractString(line, start, quoteChar, schema) {
        let result = quoteChar;
        let i = start + 1;
        let escaped = false;
        
        while (i < line.length) {
            const char = line[i];
            
            if (escaped) {
                result += char;
                escaped = false;
                i++;
                continue;
            }
            
            if (char === '\\') {
                result += char;
                escaped = true;
                i++;
                continue;
            }
            
            result += char;
            if (char === quoteChar) {
                i++;
                break;
            }
            i++;
        }
        
        return {
            text: this.wrapToken(this.escapeHtml(result), 'string', schema),
            nextIndex: i
        };
    }
    
    // NEW: Extract triple-quoted strings (handles single-line triple quotes)
    extractTripleString(line, start, quoteChar, schema) {
        let result = quoteChar + quoteChar + quoteChar;
        let i = start + 3;
        let escaped = false;
        
        while (i < line.length) {
            const char = line[i];
            
            if (escaped) {
                result += char;
                escaped = false;
                i++;
                continue;
            }
            
            if (char === '\\') {
                result += char;
                escaped = true;
                i++;
                continue;
            }
            
            // Check for closing triple quote
            if (char === quoteChar && i + 2 < line.length && 
                line[i + 1] === quoteChar && line[i + 2] === quoteChar) {
                result += quoteChar + quoteChar + quoteChar;
                i += 3;
                break;
            }
            
            result += char;
            i++;
        }
        
        return {
            text: this.wrapToken(this.escapeHtml(result), 'string', schema),
            nextIndex: i
        };
    }
    
    // FIXED: Now includes @ in the extracted text and advances past it
    extractDecorator(line, start) {
        // Skip the @ symbol
        let end = start + 1;
        // Extract the decorator name (letters, digits, underscores, dots for module paths)
        while (end < line.length && /[a-zA-Z0-9_.]/.test(line[end])) {
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
        let hasExponent = false;
        
        // Handle negative sign
        if (line[end] === '-') end++;
        
        while (end < line.length) {
            const char = line[end];
            if (this.isDigit(char)) {
                end++;
            } else if (char === '.' && !hasDecimal) {
                hasDecimal = true;
                end++;
            } else if ((char === 'e' || char === 'E') && !hasExponent) {
                hasExponent = true;
                end++;
                if (end < line.length && (line[end] === '+' || line[end] === '-')) end++;
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
        const operators = ['==', '!=', '<=', '>=', '+=', '-=', '*=', '/=', '%=', '**=', '//=', '=', '+', '-', '*', '/', '%', '**', '//', '<', '>', '&', '|', '^', '~', '>>', '<<'];
        let end = start + 1;
        
        // Check for two-character operators
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
        const operators = new Set(['+', '-', '*', '/', '%', '=', '<', '>', '&', '|', '^', '~', '!']);
        return operators.has(char);
    }
}

// Register with global registry
if (typeof window !== 'undefined') {
    window.PythonHighlighter = PythonHighlighter;
}
