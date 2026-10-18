/* CA web UI — local C/C++ syntax highlighting (no CDN, no dependencies).
 *
 * Shared by both code viewers (the fixed top source of the opened function and
 * the graph-node source panel). It is a small state-machine tokenizer:
 * keywords, type names, comments (including multi-line block comments that
 * span rendered lines), string/char literals, numbers and preprocessor lines.
 * Every non-token span is HTML-escaped, so the output is safe to insert.
 *
 *   window.CA_HIGHLIGHT.highlightLines(["int main(void) {", "  return 0;", "}"])
 *   -> ["<span class=\"tok-type\">int</span> main(...", ...]
 */
"use strict";

(() => {
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
  const NUMBER = /^(?:0[xX][0-9a-fA-F]+|0[bB][01]+|\d+(?:\.\d*)?(?:[eE][+-]?\d+)?)[uUlLfF]*/;

  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, (character) => {
      switch (character) {
        case "&":
          return "&amp;";
        case "<":
          return "&lt;";
        case ">":
          return "&gt;";
        case '"':
          return "&quot;";
        default:
          return "&#39;";
      }
    });
  }

  function classifyIdentifier(word) {
    if (KEYWORDS.has(word)) return "tok-keyword";
    if (TYPES.has(word) || /_t$/.test(word)) return "tok-type";
    return "";
  }

  function span(kind, text) {
    return kind ? `<span class="${kind}">${escapeHtml(text)}</span>` : escapeHtml(text);
  }

  function highlightLine(line, state) {
    let index = 0;
    let html = "";
    const length = line.length;
    if (state.blockComment) {
      const end = line.indexOf("*/");
      if (end < 0) {
        return span("tok-comment", line);
      }
      html += span("tok-comment", line.slice(0, end + 2));
      index = end + 2;
      state.blockComment = false;
    }
    if (index === 0) {
      const stripped = line.replace(/^\s+/, "");
      if (stripped.startsWith("#")) {
        return span("tok-preproc", line);
      }
    }
    while (index < length) {
      const character = line[index];
      const rest = line.slice(index);
      if (character === "/" && rest.startsWith("//")) {
        html += span("tok-comment", rest);
        break;
      }
      if (character === "/" && rest.startsWith("/*")) {
        const end = rest.indexOf("*/", 2);
        if (end < 0) {
          html += span("tok-comment", rest);
          state.blockComment = true;
          break;
        }
        html += span("tok-comment", rest.slice(0, end + 2));
        index += end + 2;
        continue;
      }
      if (character === '"' || character === "'") {
        let cursor = index + 1;
        while (cursor < length) {
          if (line[cursor] === "\\") {
            cursor += 2;
            continue;
          }
          if (line[cursor] === character) {
            cursor += 1;
            break;
          }
          cursor += 1;
        }
        html += span("tok-string", line.slice(index, Math.min(cursor, length)));
        index = Math.min(cursor, length);
        continue;
      }
      if (/[0-9]/.test(character) || (character === "." && /[0-9]/.test(line[index + 1] || ""))) {
        const match = NUMBER.exec(rest);
        if (match) {
          html += span("tok-number", match[0]);
          index += match[0].length;
          continue;
        }
      }
      if (/[A-Za-z_$]/.test(character)) {
        const match = IDENTIFIER.exec(rest);
        const word = match ? match[0] : character;
        html += span(classifyIdentifier(word), word);
        index += word.length;
        continue;
      }
      html += escapeHtml(character);
      index += 1;
    }
    return html;
  }

  function highlightLines(lines) {
    const state = { blockComment: false };
    return (lines || []).map((line) => highlightLine(String(line || ""), state));
  }

  window.CA_HIGHLIGHT = { highlightLines };
})();
