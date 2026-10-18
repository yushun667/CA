"""Shared AST helpers for the analyzer: source text, locations, operators.

Everything here works on live libclang cursors and a small per-worker file
cache. Reads are lazy and cached by absolute path, so a header shared by many
functions is only read once. All helpers are deterministic: they never depend
on hash iteration order and never format timestamps.
"""

from __future__ import annotations

import os
from typing import Any, Iterator

from clang.cindex import CursorKind, TypeKind

from ca.core.schema import Sloc

ASSIGN_OPS = frozenset(
    {
        "=",
        "+=",
        "-=",
        "*=",
        "/=",
        "%=",
        "&=",
        "|=",
        "^=",
        "<<=",
        ">>=",
        "&&=",
        "||=",
    }
)
INCDEC_OPS = frozenset({"++", "--"})
LOGICAL_OPS = frozenset({"&&", "||"})
_LOGICAL_PREC = {"||": 1, "&&": 2}

STATEMENT_KINDS = frozenset(
    {
        CursorKind.COMPOUND_STMT,
        CursorKind.IF_STMT,
        CursorKind.WHILE_STMT,
        CursorKind.DO_STMT,
        CursorKind.FOR_STMT,
        CursorKind.SWITCH_STMT,
        CursorKind.CASE_STMT,
        CursorKind.DEFAULT_STMT,
        CursorKind.BREAK_STMT,
        CursorKind.CONTINUE_STMT,
        CursorKind.RETURN_STMT,
        CursorKind.GOTO_STMT,
        CursorKind.LABEL_STMT,
        CursorKind.NULL_STMT,
        CursorKind.DECL_STMT,
    }
)

_OPEN = "([{"
_CLOSE = ")]}"


def extent_span(cursor: Any) -> int:
    """Cheap byte length of a cursor extent (no tokenization)."""

    try:
        extent = cursor.extent
        return int(extent.end.offset) - int(extent.start.offset)
    except Exception:  # pragma: no cover - defensive
        return 0


def unwrap(cursor: Any) -> Any:
    """Skip implicit-cast / parenthesis wrappers that carry no control flow."""

    while cursor is not None and cursor.kind in (
        CursorKind.UNEXPOSED_EXPR,
        CursorKind.PAREN_EXPR,
        CursorKind.CSTYLE_CAST_EXPR,
    ):
        children = list(cursor.get_children())
        if cursor.kind == CursorKind.CSTYLE_CAST_EXPR:
            # ``(type)expr``: the type is children[0], the operand is last.
            if not children:
                return cursor
            cursor = children[-1]
            continue
        if len(children) != 1:
            return cursor
        cursor = children[0]
    return cursor


def cursor_path(cursor: Any) -> str:
    """Absolute path of the cursor's spelling location, or ``""``."""

    try:
        location = cursor.extent.start
    except Exception:  # pragma: no cover - defensive against broken cursors
        return ""
    file_obj = location.file
    if file_obj is None:
        try:
            file_obj = cursor.location.file
        except Exception:
            return ""
    return file_obj.name if file_obj is not None else ""


def cursor_sloc(cache: "SourceCache", cursor: Any) -> Sloc:
    """Convert a cursor extent to a repo-relative :class:`Sloc`."""

    try:
        extent = cursor.extent
        start = extent.start
        end = extent.end
    except Exception:  # pragma: no cover
        return Sloc("")
    path = cursor_path(cursor)
    rel = cache.repo_path(path)
    return Sloc(
        file=rel,
        line=int(start.line or 0),
        col=int(start.column or 0),
        end_line=int(end.line or 0),
        end_col=int(end.column or 0),
    )


def normalize_text(text: str, limit: int = 0) -> str:
    """Collapse whitespace and optionally truncate, deterministically.

    For very large spans (macro/template expansions) only a bounded prefix is
    normalized: the output is truncated to ``limit`` anyway, so this avoids
    quadratic-ish whitespace work on multi-hundred-KB source ranges.
    """

    if limit and len(text) > limit * 8:
        text = text[: limit * 8]
    normalized = " ".join(text.split())
    if limit and len(normalized) > limit:
        normalized = normalized[: max(1, limit - 3)].rstrip() + "..."
    return normalized


class SourceCache:
    """Lazily read source files once per process; slice cursor extents."""

    def __init__(self, repo_root: str = "") -> None:
        self.repo_root = os.path.realpath(repo_root) if repo_root else ""
        self._texts: dict[str, str] = {}
        self._relative: dict[str, str] = {}

    # -- paths -----------------------------------------------------------
    def resolve(self, path: str) -> str:
        """Resolve a cursor path against ``repo_root`` (never the cwd).

        libclang reports header paths relative to the compiler working
        directory (``./include/linux/...``); joining them to the process cwd
        would produce unusable absolute paths in the database. Absolute paths
        keep ``realpath`` semantics (they must match the parser's path mapper);
        relative include paths are normalized lexically, which avoids the
        per-header ``lstat``/``realpath`` syscalls that dominate large TUs.
        """

        if not path:
            return ""
        if os.path.isabs(path):
            lexical = os.path.normpath(path)
            if self.repo_root and (
                lexical == self.repo_root
                or lexical.startswith(self.repo_root + os.sep)
            ):
                return lexical
            try:
                return os.path.realpath(path)
            except OSError:  # pragma: no cover - defensive
                return path
        if not self.repo_root:
            try:
                return os.path.realpath(path)
            except OSError:  # pragma: no cover
                return path
        return os.path.normpath(os.path.join(self.repo_root, path))

    def repo_path(self, path: str) -> str:
        if not path:
            return ""
        cached = self._relative.get(path)
        if cached is not None:
            return cached
        result = self._compute_repo_path(path)
        self._relative[path] = result
        return result

    def _compute_repo_path(self, path: str) -> str:
        real = self.resolve(path)
        if not self.repo_root:
            return real.replace(os.sep, "/")
        try:
            rel = os.path.relpath(real, self.repo_root)
        except ValueError:  # pragma: no cover - different drives
            return ""
        if rel == os.pardir or rel.startswith(os.pardir + os.sep):
            # Files outside the analyzed repository are not addressable by the
            # database; mark them unknown instead of emitting cwd-relative
            # or machine-absolute paths.
            return ""
        return rel.replace(os.sep, "/")

    # -- file text -------------------------------------------------------
    def text(self, path: str) -> str:
        if not path:
            return ""
        resolved = self.resolve(path)
        cached = self._texts.get(resolved)
        if cached is not None:
            return cached
        try:
            with open(resolved, "r", encoding="utf-8", errors="replace") as handle:
                content = handle.read()
        except OSError:
            content = ""
        self._texts[resolved] = content
        return content

    def span_text(self, path: str, start: int, end: int) -> str:
        if not path or start < 0 or end <= start:
            return ""
        content = self.text(path)
        if not content:
            return ""
        return content[start:end]

    # -- cursor text -----------------------------------------------------
    def text_for(self, cursor: Any, limit: int = 0) -> str:
        if cursor is None:
            return ""
        try:
            extent = cursor.extent
            path = cursor_path(cursor)
            if path:
                text = self.span_text(path, extent.start.offset, extent.end.offset)
                if text:
                    return normalize_text(text, limit)
        except Exception:  # pragma: no cover - defensive
            pass
        try:
            fallback = " ".join(
                token.spelling for token in token_list(cursor, 32)
            )
        except Exception:  # pragma: no cover
            fallback = cursor.spelling or ""
        return normalize_text(fallback, limit)


_FILE_NAME_CACHE: dict[str, str] = {}
_MACRO_FLAG_CACHE: dict[int, bool] = {}


def _file_name(location: Any) -> str:
    try:
        file_obj = location.file
    except Exception:  # pragma: no cover - defensive
        return ""
    if file_obj is None:
        return ""
    name = file_obj.name
    normalized = _FILE_NAME_CACHE.get(name)
    if normalized is None:
        normalized = os.path.normpath(name)
        if len(_FILE_NAME_CACHE) < 50000:
            _FILE_NAME_CACHE[name] = normalized
    return normalized


def cursor_is_macro_expansion(cursor: Any) -> bool:
    """True when the cursor's token extent lives in another file/macro body.

    libclang reports the *usage* site through ``cursor.location`` but the
    *spelling* tokens of macro-expanded nodes may come from a header macro
    definition. Slicing source text across those two files produces huge
    garbage strings, so callers must treat such cursors as non-source facts.
    """

    if cursor is None:
        return False
    cached = _MACRO_FLAG_CACHE.get(cursor.hash)
    if cached is not None:
        return cached
    result = False
    try:
        location = cursor.location
        extent = cursor.extent
        location_line = location.line
        start = extent.start
        end = extent.end
        # Cheap checks first (no file objects): macro-expanded nodes either
        # collapse to a zero-width usage-site extent or have spelling tokens
        # on a different line than the expansion location.
        if start.offset == end.offset and start.line == end.line:
            result = True
        elif location_line and start.line and location_line != start.line:
            result = True
        else:
            location_file = _file_name(location)
            start_file = _file_name(start)
            if not start_file:
                result = False
            elif location_file and location_file != start_file:
                result = True
            else:
                end_file = _file_name(end)
                if end_file and end_file != start_file:
                    result = True
    except Exception:  # pragma: no cover - defensive
        result = False
    if len(_MACRO_FLAG_CACHE) < 200000:
        _MACRO_FLAG_CACHE[cursor.hash] = result
    return result


_SYNTHETIC_STMT_KINDS = frozenset(
    {
        CursorKind.IF_STMT,
        CursorKind.WHILE_STMT,
        CursorKind.DO_STMT,
        CursorKind.FOR_STMT,
        CursorKind.SWITCH_STMT,
        CursorKind.CASE_STMT,
        CursorKind.DEFAULT_STMT,
        CursorKind.COMPOUND_STMT,
        CursorKind.LABEL_STMT,
        CursorKind.RETURN_STMT,
    }
)


def statement_is_synthetic(cursor: Any) -> bool:
    """True for control-flow nodes synthesized entirely from a macro body.

    Such nodes have no spelling tokens of their own; their extent is a
    zero-width or macro-definition range while ``location`` points at the
    usage site. They must not become source-level CFG/branch facts.
    """

    if cursor is None or cursor.kind not in _SYNTHETIC_STMT_KINDS:
        return False
    try:
        tokens = cursor.get_tokens()
        return next(tokens, None) is None
    except Exception:  # pragma: no cover - defensive
        return False


# --------------------------------------------------------------------------- #
# Tokens and operators
# --------------------------------------------------------------------------- #


def token_list(cursor: Any, limit: int = 0) -> list[Any]:
    """Tokens of a cursor, optionally capped.

    Macro-expanded nodes can report a tiny extent but return thousands of
    spelling tokens from the macro body; callers that only need an operator
    check cap the iteration to keep analysis linear in expression size.
    """

    try:
        iterator = cursor.get_tokens()
        if not limit:
            return list(iterator)
        tokens: list[Any] = []
        for token in iterator:
            tokens.append(token)
            if len(tokens) >= limit:
                break
        return tokens
    except Exception:  # pragma: no cover - defensive
        return []


def depth_zero_tokens(tokens: list[Any]) -> list[Any]:
    """Tokens outside any bracket nesting."""

    result: list[Any] = []
    depth = 0
    for token in tokens:
        spelling = token.spelling
        if spelling in _OPEN:
            depth += 1
            continue
        if spelling in _CLOSE:
            depth = max(0, depth - 1)
            continue
        if depth == 0:
            result.append(token)
    return result


def _tokens_text(cache: SourceCache, cursor: Any, tokens: list[Any]) -> str:
    if not tokens:
        return ""
    path = cursor_path(cursor)
    first = tokens[0].extent
    last = tokens[-1].extent
    text = cache.span_text(path, first.start.offset, last.end.offset)
    if not text:
        text = " ".join(token.spelling for token in tokens)
    return normalize_text(text)


def logical_operator(cursor: Any) -> str:
    """Return ``"&&"`` / ``"||"`` when the cursor's root operator is logical.

    The root is the depth-zero operator with the lowest precedence; ties are
    resolved rightmost because ``&&``/``||`` are left-associative.
    """

    if cursor is None or cursor.kind != CursorKind.BINARY_OPERATOR:
        return ""
    if extent_span(cursor) > 4096:
        return ""
    tokens = token_list(cursor, 64)
    if len(tokens) >= 64:
        # Pathological macro-expanded expression; do not guess an operator.
        return ""
    candidates: list[tuple[int, int, str]] = []
    for index, token in enumerate(depth_zero_tokens(tokens)):
        spelling = token.spelling
        if spelling in LOGICAL_OPS:
            candidates.append((_LOGICAL_PREC[spelling], index, spelling))
    if not candidates:
        return ""
    candidates.sort(key=lambda item: (item[0], -item[1]))
    return candidates[0][2]


_MAX_TEXT_SPAN = 4096


def assignment_split(
    cache: SourceCache, cursor: Any, limit: int = 0
) -> tuple[str, str, str] | None:
    """Split an assignment cursor into ``(operator, lhs_text, rhs_text)``.

    Returns ``None`` for non-assignment nodes. Comma expressions are ignored
    here (the walker recurses into their children instead).
    """

    if cursor is None or cursor.kind not in (
        CursorKind.BINARY_OPERATOR,
        CursorKind.COMPOUND_ASSIGNMENT_OPERATOR,
    ):
        return None
    if extent_span(cursor) > _MAX_TEXT_SPAN:
        return None
    tokens = token_list(cursor, 256)
    if not tokens or len(tokens) >= 256:
        return None
    zero_tokens = depth_zero_tokens(tokens)
    if any(token.spelling == "," for token in zero_tokens):
        return None
    op_index = -1
    op = ""
    depth = 0
    for index, token in enumerate(tokens):
        spelling = token.spelling
        if spelling in _OPEN:
            depth += 1
        elif spelling in _CLOSE:
            depth = max(0, depth - 1)
        elif depth == 0 and spelling in ASSIGN_OPS:
            op_index = index
            op = spelling
            break
    if op_index < 0:
        return None
    start = tokens[0].extent
    operator = tokens[op_index].extent
    end = tokens[-1].extent
    if end.end.offset - start.start.offset > _MAX_TEXT_SPAN:
        return None
    if len(_token_files(tokens)) > 1:
        return None
    if not tokens_inside_extent(cursor, tokens):
        return None
    path = cursor_path(cursor)
    lhs = cache.span_text(path, start.start.offset, operator.start.offset)
    rhs_start = operator.end.offset
    # Keep the assignment operator out of the RHS: for compound operators the
    # token itself is the operator, for "=" the RHS starts right after it.
    rhs = cache.span_text(path, rhs_start, end.end.offset)
    if not lhs and not rhs:
        return None
    return op, normalize_text(lhs, limit), normalize_text(rhs, limit)


def tokens_inside_extent(cursor: Any, tokens: list[Any]) -> bool:
    """True when every token location lies inside the cursor's reported extent.

    Macro-expanded nodes report a usage-site extent but their spelling tokens
    can come from the macro definition (same file or header). Such nodes must
    not be sliced as ordinary source spans.
    """

    if not tokens:
        return True
    try:
        extent = cursor.extent
        low_line, low_col = extent.start.line, extent.start.column
        high_line, high_col = extent.end.line, extent.end.column
    except Exception:  # pragma: no cover - defensive
        return False
    for token in tokens:
        token_extent = token.extent
        for location in (token_extent.start, token_extent.end):
            line, column = location.line, location.column
            if line < low_line or (line == low_line and column < low_col):
                return False
            if line > high_line or (line == high_line and column > high_col):
                return False
    return True


def _token_files(tokens: list[Any]) -> set[str]:
    files: set[str] = set()
    for token in tokens:
        name = _file_name(token.extent.start)
        if name:
            files.add(os.path.normpath(name))
    return files


def _depth_before(tokens: list[Any], index: int) -> int:
    depth = 0
    for token in tokens[:index]:
        spelling = token.spelling
        if spelling in _OPEN:
            depth += 1
        elif spelling in _CLOSE:
            depth = max(0, depth - 1)
    return depth


def incdec_operator(cursor: Any) -> str:
    """Return ``"++"`` / ``"--"`` for a top-level increment/decrement."""

    if cursor is None or cursor.kind != CursorKind.UNARY_OPERATOR:
        return ""
    if extent_span(cursor) > 256:
        return ""
    tokens = token_list(cursor, 16)
    if len(tokens) >= 16:
        return ""
    for token in depth_zero_tokens(tokens):
        if token.spelling in INCDEC_OPS:
            return token.spelling
    return ""


def decl_init_split(
    cache: SourceCache, var_cursor: Any, limit: int = 0
) -> tuple[str, str] | None:
    """Return ``(lhs_text, rhs_text)`` for a declaration with an initializer."""

    tokens = token_list(var_cursor)
    if not tokens:
        return None
    eq_index = -1
    depth = 0
    for index, token in enumerate(tokens):
        spelling = token.spelling
        if spelling in _OPEN:
            depth += 1
        elif spelling in _CLOSE:
            depth = max(0, depth - 1)
        elif depth == 0 and spelling == "=":
            eq_index = index
            break
    if eq_index < 0:
        return None
    if tokens[-1].extent.end.offset - tokens[0].extent.start.offset > _MAX_TEXT_SPAN:
        return None
    if len(_token_files(tokens)) > 1:
        return None
    if not tokens_inside_extent(var_cursor, tokens):
        return None
    path = cursor_path(var_cursor)
    lhs = cache.span_text(
        path, tokens[0].extent.start.offset, tokens[eq_index].extent.end.offset
    )
    rhs = cache.span_text(
        path,
        tokens[eq_index].extent.end.offset,
        tokens[-1].extent.end.offset,
    )
    return normalize_text(lhs, limit), normalize_text(rhs, limit)


def _referenced_is_function(decl_ref: Any) -> bool:
    try:
        referenced = decl_ref.referenced
    except Exception:  # pragma: no cover - external decls
        referenced = None
    if referenced is not None:
        return referenced.kind in (
            CursorKind.FUNCTION_DECL,
            CursorKind.FUNCTION_TEMPLATE,
            CursorKind.CXX_METHOD,
        )
    try:
        return decl_ref.type.kind == TypeKind.FUNCTIONPROTO
    except Exception:  # pragma: no cover
        return False


def collect_var_names(cursor: Any, limit: int = 16) -> tuple[str, ...]:
    """Recursively collect referenced variable names, in source order."""

    names: list[str] = []
    seen: set[str] = set()

    def visit(node: Any) -> None:
        if len(names) >= limit:
            return
        if node.kind == CursorKind.DECL_REF_EXPR:
            name = node.spelling
            if name and name not in seen and not _referenced_is_function(node):
                seen.add(name)
                names.append(name)
            return
        for child in node.get_children():
            visit(child)
            if len(names) >= limit:
                return

    visit(cursor)
    return tuple(names)


def var_names_in_text(names: tuple[str, ...], text: str, limit: int = 16) -> tuple[str, ...]:
    """Order ``names`` as they first appear in ``text`` (stable and cheap)."""

    if not names:
        return ()
    positions: dict[str, int] = {}
    for name in names:
        index = text.find(name)
        positions[name] = index if index >= 0 else len(text) + len(names)
    ordered = sorted(names, key=lambda item: (positions[item], names.index(item)))
    return tuple(ordered[:limit])


def safe_text_for(
    cache: SourceCache, cursor: Any, limit: int = 0, macro_limit: int = 64
) -> str:
    """Extent text that never returns macro-body garbage across files.

    Normal cursors use the source slice (whitespace-normalized and truncated).
    Macro-expanded cursors fall back to their spelling or token spellings,
    bounded by ``macro_limit`` so DB rows stay small.
    """

    if cursor is None:
        return ""
    if cursor_is_macro_expansion(cursor):
        spelling = cursor.spelling or ""
        if not spelling:
            tokens = token_list(cursor, 16)
            spelling = " ".join(token.spelling for token in tokens[:8])
        return normalize_text(spelling, macro_limit or limit)
    return cache.text_for(cursor, limit)


def first_child(cursor: Any) -> Any:
    for child in cursor.get_children():
        return child
    return None


def unwrapped_children(cursor: Any) -> list[Any]:
    return [unwrap(child) for child in cursor.get_children()]


def call_callee_name(call_cursor: Any, cache: SourceCache, limit: int = 120) -> str:
    """Best-effort callee name for a CALL_EXPR, macro calls included."""

    spelling = call_cursor.spelling
    if spelling:
        return spelling
    children = list(call_cursor.get_children())
    if children:
        candidate = unwrap(children[0])
        if candidate.spelling:
            return candidate.spelling
        text = cache.text_for(children[0], limit)
        head = text.split("(", 1)[0].strip()
        if head:
            return head.split()[-1]
    return ""


def call_arguments(call_cursor: Any) -> list[Any]:
    children = list(call_cursor.get_children())
    return [unwrap(child) for child in children[1:]]


def find_child_stmt(cursor: Any) -> Any:
    """The compound statement that forms a function definition body."""

    for child in cursor.get_children():
        if child.kind == CursorKind.COMPOUND_STMT:
            return child
    return None


def iter_descendants(cursor: Any) -> Iterator[Any]:
    for child in cursor.get_children():
        yield child
        yield from iter_descendants(child)


__all__ = [
    "ASSIGN_OPS",
    "INCDEC_OPS",
    "LOGICAL_OPS",
    "STATEMENT_KINDS",
    "SourceCache",
    "assignment_split",
    "call_arguments",
    "call_callee_name",
    "collect_var_names",
    "cursor_is_macro_expansion",
    "cursor_path",
    "cursor_sloc",
    "decl_init_split",
    "depth_zero_tokens",
    "extent_span",
    "find_child_stmt",
    "first_child",
    "incdec_operator",
    "iter_descendants",
    "logical_operator",
    "normalize_text",
    "safe_text_for",
    "statement_is_synthetic",
    "token_list",
    "tokens_inside_extent",
    "unwrap",
    "unwrapped_children",
    "var_names_in_text",
]
