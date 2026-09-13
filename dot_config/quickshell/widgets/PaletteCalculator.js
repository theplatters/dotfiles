// Small, deliberately local expression parser for the command palette.
// The function is kept in the global QML JavaScript scope so it can be used
// with: import "PaletteCalculator.js" as Calculator

var MAX_INPUT_LENGTH = 512;
var MAX_WORK = 1024;
var MAX_TOKENS = 256;
var MAX_DEPTH = 64;

var NUMBER_PATTERN = /^(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/;

function finiteNumber(value) {
    return typeof value === "number" && isFinite(value);
}

function hasDigit(value) {
    return /[0-9]/.test(value);
}

function isPathLike(value) {
    if (/^(?:\/|\.\/|\.\.\/|~\/|[A-Za-z]:[\\/])/.test(value)) return true;
    if (value.indexOf("://") >= 0) return true;

    // A slash surrounded by word characters is much more likely to be a
    // path/search query than division. Whitespace around it remains usable
    // for expressions such as "12 / 4".
    if (value.indexOf("/") >= 0 && /[A-Za-z]/.test(value) && !/\s[+\-*\/%^]|[+\-*\/%^]\s/.test(value))
        return true;
    return false;
}

function isArithmeticTokenSequence(value) {
    var position = 0;
    var sawNumber = false;
    while (position < value.length) {
        var character = value.charAt(position);
        if (/\s/.test(character)) {
            position++;
            continue;
        }
        if (/[+\-*\/%^()]/.test(character)) {
            position++;
            continue;
        }
        if (/[0-9.eE]/.test(character)) {
            sawNumber = true;
            // Consume a number-shaped token, including malformed forms. The
            // parser will provide the useful syntax error for those forms;
            // this pass only separates math punctuation from filenames.
            while (position < value.length && /[0-9.eE]/.test(value.charAt(position)))
                position++;
            continue;
        }
        return false;
    }
    return sawNumber;
}

function isFilenameLike(value) {
    if (/\s/.test(value) || !/[A-Za-z]/.test(value)) return false;
    return /^[A-Za-z0-9_.-]+$/.test(value) && !isArithmeticTokenSequence(value);
}

function startsExpression(value) {
    return /^[+\-.(0-9]/.test(value);
}

function looksLikeArithmetic(value) {
    var text = value.trim();
    if (!text || !hasDigit(text) || isPathLike(text)) return false;
    if (isFilenameLike(text)) return false;

    var hasOperator = /[+\-*\/%^]/.test(text);
    var hasParenthesis = /[()]/.test(text);
    if (!hasOperator && !hasParenthesis) {
        // A malformed number such as "1e" or "1..2" is still a useful
        // calculator error when it clearly starts like a number.
        return startsExpression(text) && /[.eE]/.test(text) &&
            (!NUMBER_PATTERN.test(text) || !finiteNumber(Number(text)));
    }

    // The sign in a scientific literal is not an arithmetic operator. This
    // keeps a bare literal such as "2e-3" in the normal search results.
    if (NUMBER_PATTERN.test(text)) return false;
    if (!startsExpression(text)) return false;
    return true;
}

function Parser(source) {
    this.source = source;
    this.position = 0;
    this.work = 0;
    this.tokens = 0;
    this.depth = 0;
    this.error = "";
}

Parser.prototype.fail = function(message) {
    if (!this.error) this.error = message;
    return false;
};

Parser.prototype.enter = function() {
    this.depth++;
    if (this.depth > MAX_DEPTH) {
        this.depth--;
        return this.fail("Expression is too deeply nested");
    }
    return true;
};

Parser.prototype.leave = function() {
    this.depth--;
};

Parser.prototype.step = function() {
    this.work++;
    if (this.work > MAX_WORK) return this.fail("Expression is too complex");
    return !this.error;
};

Parser.prototype.skipWhitespace = function() {
    while (this.position < this.source.length && /\s/.test(this.source.charAt(this.position))) {
        if (!this.step()) return false;
        this.position++;
    }
    return !this.error;
};

Parser.prototype.peek = function() {
    return this.source.charAt(this.position);
};

Parser.prototype.take = function(character) {
    if (!this.step()) return false;
    if (this.peek() !== character) return false;
    this.position++;
    this.tokens++;
    if (this.tokens > MAX_TOKENS) {
        this.fail("Expression has too many terms");
        return false;
    }
    return true;
};

Parser.prototype.parseNumber = function() {
    if (!this.enter()) return NaN;
    var start = this.position;
    var digits = 0;

    while (/[0-9]/.test(this.peek())) {
        if (!this.step()) { this.leave(); return NaN; }
        this.position++;
        digits++;
    }
    if (this.peek() === ".") {
        if (!this.step()) { this.leave(); return NaN; }
        this.position++;
        while (/[0-9]/.test(this.peek())) {
            if (!this.step()) { this.leave(); return NaN; }
            this.position++;
            digits++;
        }
    }
    if (digits === 0) {
        this.fail("Expected a number or '('");
        this.leave();
        return NaN;
    }

    if (this.peek() === "e" || this.peek() === "E") {
        if (!this.step()) { this.leave(); return NaN; }
        this.position++;
        if (this.peek() === "+" || this.peek() === "-") {
            if (!this.step()) { this.leave(); return NaN; }
            this.position++;
        }
        var exponentDigits = 0;
        while (/[0-9]/.test(this.peek())) {
            if (!this.step()) { this.leave(); return NaN; }
            this.position++;
            exponentDigits++;
        }
        if (exponentDigits === 0) {
            this.fail("Scientific notation needs exponent digits");
            this.leave();
            return NaN;
        }
    }

    this.tokens++;
    if (this.tokens > MAX_TOKENS) {
        this.fail("Expression has too many terms");
        this.leave();
        return NaN;
    }
    var value = Number(this.source.slice(start, this.position));
    if (!finiteNumber(value)) this.fail("Number is not finite");
    this.leave();
    return value;
};

Parser.prototype.parsePrimary = function() {
    if (!this.enter()) return NaN;
    this.skipWhitespace();
    var value;
    if (/[0-9]/.test(this.peek()) || (this.peek() === "." && /[0-9]/.test(this.source.charAt(this.position + 1)))) {
        value = this.parseNumber();
    } else if (this.peek() === "(") {
        this.take("(");
        value = this.parseAdditive();
        this.skipWhitespace();
        if (!this.error && !this.take(")")) this.fail("Missing closing ')'");
    } else {
        this.fail("Expected a number or '('");
        value = NaN;
    }
    this.leave();
    return value;
};

Parser.prototype.parsePower = function() {
    if (!this.enter()) return NaN;
    var left = this.parsePrimary();
    this.skipWhitespace();
    if (!this.error && this.peek() === "^") {
        this.take("^");
        var right = this.parseUnary();
        if (!this.error) left = this.calculate(left, right, "^");
    }
    this.leave();
    return left;
};

Parser.prototype.parseUnary = function() {
    if (!this.enter()) return NaN;
    this.skipWhitespace();
    var sign = 1;
    if (this.peek() === "+" || this.peek() === "-") {
        if (this.peek() === "-") sign = -1;
        this.take(this.peek());
        var signed = this.parseUnary();
        if (!this.error) signed *= sign;
        this.leave();
        return signed;
    }
    var value = this.parsePower();
    if (!this.error) value *= sign;
    this.leave();
    return value;
};

Parser.prototype.parseMultiplicative = function() {
    if (!this.enter()) return NaN;
    var value = this.parseUnary();
    while (!this.error) {
        this.skipWhitespace();
        var operator = this.peek();
        if (operator !== "*" && operator !== "/" && operator !== "%") break;
        this.take(operator);
        var right = this.parseUnary();
        if (!this.error) value = this.calculate(value, right, operator);
    }
    this.leave();
    return value;
};

Parser.prototype.parseAdditive = function() {
    if (!this.enter()) return NaN;
    var value = this.parseMultiplicative();
    while (!this.error) {
        this.skipWhitespace();
        var operator = this.peek();
        if (operator !== "+" && operator !== "-") break;
        this.take(operator);
        var right = this.parseMultiplicative();
        if (!this.error) value = this.calculate(value, right, operator);
    }
    this.leave();
    return value;
};

Parser.prototype.calculate = function(left, right, operator) {
    var value;
    if ((operator === "/" || operator === "%") && right === 0) {
        this.fail("Division by zero");
        return NaN;
    }

    if (operator === "+") value = left + right;
    else if (operator === "-") value = left - right;
    else if (operator === "*") value = left * right;
    else if (operator === "/") value = left / right;
    else if (operator === "%") value = left % right;
    else value = Math.pow(left, right);

    if (!finiteNumber(value)) {
        this.fail("Result is not finite");
        return NaN;
    }
    return value;
};

function formatResult(value) {
    if (value === 0) return "0";
    // Fifteen significant digits removes the usual binary floating point
    // noise while retaining useful calculator precision.
    var rounded = Number(value.toPrecision(15));
    if (rounded === 0) return "0";
    return String(rounded);
}

function calculate(input) {
    if (typeof input !== "string") return {matched: false, value: "", error: ""};
    // Inspect only a bounded prefix before doing any trimming or matching so
    // an accidentally pasted multi-megabyte query cannot occupy the UI.
    if (input.length > MAX_INPUT_LENGTH) {
        var prefix = input.slice(0, MAX_INPUT_LENGTH).trim();
        if (!looksLikeArithmetic(prefix)) return {matched: false, value: "", error: ""};
        return {matched: true, value: "", error: "Expression is too long"};
    }
    var text = input.trim();
    if (!looksLikeArithmetic(text)) return {matched: false, value: "", error: ""};

    var parser = new Parser(text);
    var value = parser.parseAdditive();
    parser.skipWhitespace();
    if (!parser.error && parser.position < text.length)
        parser.fail("Unexpected character '" + parser.peek() + "'");
    if (parser.error) return {matched: true, value: "", error: parser.error};
    if (!finiteNumber(value)) return {matched: true, value: "", error: "Result is not finite"};
    return {matched: true, value: formatResult(value), error: ""};
}
