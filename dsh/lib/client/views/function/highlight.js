/* Function detail view — local C/C++ tokenizer.
 *
 * ESM port of the CA web UI tokenizer (src/ca/ui/static/highlight.js). It is a
 * small state-machine lexer: keywords, type names, comments (including
 * multi-line block comments that span lines), string/char literals, numbers and
 * preprocessor lines.
 *
 * Unlike the original it never produces HTML: every token is a plain object
 * `{text, cls}` and the React renderer turns it into an escaped <span>. Source
 * text can therefore never be interpreted as markup.
 *
 *   tokenizeLines(["int main(void) {", "  return 0;", "}"])
 *   -> [[{cls:'type',text:'int'}, {text:' main'}, ...], ...]
 */

const KEYWORDS = new Set([
  "auto", "break", "case", "const", "continue", "default", "do", "else",
  "enum", "extern", "for", "goto", "if", "inline", "register", "restrict",
  "return", "sizeof", "static", "struct", "switch", "typedef", "union",
  "volatile", "while", "_Alignas", "_Alignof", "_Atomic", "_Bool", "_Complex",
  "_Generic", "_Imaginary", "_Noreturn", "_Static_assert", "_Thread_local",
  "alignas", "alignof", "asm", "catch", "class", "constexpr", "const_cast",
  "decltype", "delete", "dynamic_cast", "explicit", "export", "false",
  "friend", "mutable", "namespace", "new", "noexcept", "nullptr", "operator",
  "private", "protected", "public", "reinterpret_cast", "static_assert",
  "static_cast", "template", "this", "throw", "true", "try", "typeid",
  "typename", "using", "virtual", "wchar_t",
]);

const TYPES = new Set([
  "void", "char", "short", "int", "long", "float", "double", "signed",
  "unsigned", "bool", "_Bool", "size_t", "ssize_t", "ptrdiff_t", "int8_t",
  "int16_t", "int32_t", "int64_t", "uint8_t", "uint16_t", "uint32_t",
  "uint64_t", "intptr_t", "uintptr_t", "wchar_t", "va_list", "FILE",
  "off_t", "pid_t", "uid_t", "gid_t", "dev_t", "ino_t", "time_t", "socklen_t",
  "uint", "ulong", "uchar", "ushort",
]);

const IDENTIFIER = /^[A-Za-z_$][A-Za-z0-9_$]*/;
const NUMBER =
  /^(?:0[xX][0-9a-fA-F]+|0[bB][01]+|\d+(?:\.\d*)?(?:[eE][+-]?\d+)?)[uUlLfF]*/;

/** CSS class prefix used by the source renderer; `.cls` is appended to it. */
export const TOKEN_CLASS_PREFIX = "ca-fn-tok-";

/** Token class expected by the function-view stylesheet. */
export const TOKEN_CLASSES = Object.freeze({
  keyword: "ca-fn-tok-keyword",
  type: "ca-fn-tok-type",
  string: "ca-fn-tok-string",
  number: "ca-fn-tok-number",
  comment: "ca-fn-tok-comment",
  preproc: "ca-fn-tok-preproc",
});

/** Map a token `cls` suffix to its stylesheet class ("" stays unscoped). */
export function tokenClass(cls) {
  return cls ? TOKEN_CLASS_PREFIX + cls : "";
}

function classifyIdentifier(word) {
  if (KEYWORDS.has(word)) return "keyword";
  if (TYPES.has(word) || /_t$/.test(word)) return "type";
  return "";
}

function push(tokens, cls, text) {
  if (!text) return;
  tokens.push(cls ? { text, cls } : { text });
}

/**
 * Tokenize one source line.
 *
 * `state` is shared between calls so block comments stay open across lines:
 * `{blockComment: boolean}`.
 */
export function tokenizeLine(line, state) {
  const text = String(line === null || line === undefined ? "" : line);
  const tokens = [];
  const length = text.length;
  let index = 0;
  const lexerState = state || { blockComment: false };

  if (lexerState.blockComment) {
    const end = text.indexOf("*/");
    if (end < 0) {
      push(tokens, "comment", text);
      return tokens;
    }
    push(tokens, "comment", text.slice(0, end + 2));
    index = end + 2;
    lexerState.blockComment = false;
  }

  if (index === 0) {
    const stripped = text.replace(/^\s+/, "");
    if (stripped.startsWith("#")) {
      push(tokens, "preproc", text);
      return tokens;
    }
  }

  while (index < length) {
    const character = text[index];
    const rest = text.slice(index);
    if (character === "/" && rest.startsWith("//")) {
      push(tokens, "comment", rest);
      break;
    }
    if (character === "/" && rest.startsWith("/*")) {
      const end = rest.indexOf("*/", 2);
      if (end < 0) {
        push(tokens, "comment", rest);
        lexerState.blockComment = true;
        break;
      }
      push(tokens, "comment", rest.slice(0, end + 2));
      index += end + 2;
      continue;
    }
    if (character === '"' || character === "'") {
      let cursor = index + 1;
      while (cursor < length) {
        if (text[cursor] === "\\") {
          cursor += 2;
          continue;
        }
        if (text[cursor] === character) {
          cursor += 1;
          break;
        }
        cursor += 1;
      }
      push(tokens, "string", text.slice(index, Math.min(cursor, length)));
      index = Math.min(cursor, length);
      continue;
    }
    if (
      /[0-9]/.test(character) ||
      (character === "." && /[0-9]/.test(text[index + 1] || ""))
    ) {
      const match = NUMBER.exec(rest);
      if (match) {
        push(tokens, "number", match[0]);
        index += match[0].length;
        continue;
      }
    }
    if (/[A-Za-z_$]/.test(character)) {
      const match = IDENTIFIER.exec(rest);
      const word = match ? match[0] : character;
      push(tokens, classifyIdentifier(word), word);
      index += word.length;
      continue;
    }
    // Punctuation, whitespace and anything else stays unstyled (React escapes it).
    let cursor = index + 1;
    while (
      cursor < length &&
      !/[A-Za-z0-9_$"'/]/.test(text[cursor]) &&
      !(text[cursor] === "." && /[0-9]/.test(text[cursor + 1] || ""))
    ) {
      cursor += 1;
    }
    push(tokens, "", text.slice(index, cursor));
    index = cursor;
  }
  return tokens;
}

/** Tokenize a list of lines, keeping block-comment state across them. */
export function tokenizeLines(lines) {
  const state = { blockComment: false };
  return (lines || []).map((line) => tokenizeLine(line, state));
}

/**
 * Build code rows ready for rendering: `{line, text, tokens}` with 1-based line
 * numbers starting at `startLine`.
 */
export function highlightRows(lines, startLine) {
  const first = Number(startLine) > 0 ? Number(startLine) : 1;
  const state = { blockComment: false };
  return (lines || []).map((line, index) => {
    const text = String(line === null || line === undefined ? "" : line);
    return {
      line: first + index,
      text,
      tokens: tokenizeLine(text, state),
    };
  });
}
