"""Shared libclang helpers used by the AST extractor.

Responsibilities kept here (see ``src/ca/parse/AGENTS.md``):

- bootstrap the pinned libclang shared library shipped with the Python wheel;
- map libclang's absolute file names to repo-relative POSIX paths and build
  :class:`ca.core.schema.Sloc` values;
- normalize cursor attributes that the Python bindings do not expose directly
  (inline flag, argument count, macro-expansion detection) through the C API;
- map linkage/storage classes and cursor kinds onto the string values used by
  :mod:`ca.core.schema`.

Nothing in this module knows about parse policies (argument filtering, target
inference, failure isolation); those live in :mod:`ca.parse.libclang_parser`.
"""

from __future__ import annotations

import ctypes
import functools
import os
import re
from pathlib import Path
from typing import Any, Mapping

import clang.cindex as ci

from ca.core.schema import Sloc

_LIBRARY_NAMES = ("libclang.dylib", "libclang.so", "libclang.dll")
_bindings: dict[str, Any] = {}


# --------------------------------------------------------------------------- #
# libclang bootstrap
# --------------------------------------------------------------------------- #


LIBRARY_ENV_VARS = ("CA_LIBCLANG_LIBRARY", "CA_LIBCLANG_LIBRARY_FILE")
LIBRARY_PATH_ENV_VARS = ("CA_LIBCLANG_LIBRARY_PATH",)


def libclang_library_override() -> str:
    """Explicit libclang shared library requested by the environment.

    The bundled wheel is the default; a matched-version backend (e.g. the OH
    clang 15 ``libclang.so.15`` used for OpenHarmony acceptance, with matching
    PyPI bindings on ``PYTHONPATH``) is selected with ``CA_LIBCLANG_LIBRARY``.
    The variable is read before any bindings call, so callers must set it before
    importing :mod:`ca.parse`.
    """

    for name in LIBRARY_ENV_VARS:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def ensure_libclang() -> None:
    """Point the Python bindings at the configured libclang shared library.

    Selection order: an explicit ``clang.cindex.Config`` set by the caller, the
    ``CA_LIBCLANG_LIBRARY``/``CA_LIBCLANG_LIBRARY_PATH`` environment override
    (matched-version backend), then the libclang bundled with the pinned wheel.
    ``Config.set_library_file`` must run before any other bindings call, so the
    parser invokes this at the start of every parse; later calls are no-ops.
    """

    if getattr(ci.Config, "loaded", False):
        return
    if getattr(ci.Config, "library_file", None):
        return
    override = libclang_library_override()
    if override:
        ci.Config.set_library_file(override)
        return
    for env_name in LIBRARY_PATH_ENV_VARS:
        directory = os.environ.get(env_name, "").strip()
        if directory:
            ci.Config.set_library_path(directory)
            return
    native_dir = Path(ci.__file__).resolve().parent / "native"
    for name in _LIBRARY_NAMES:
        candidate = native_dir / name
        if candidate.is_file():
            ci.Config.set_library_file(str(candidate))
            return
    # Let the bindings fall back to their platform default search path; a
    # missing library surfaces as clang.cindex.LibclangError on first use.


def index() -> ci.Index:
    """Return a process-wide index (reusing it avoids per-TU index setup cost)."""

    ensure_libclang()
    global _INDEX
    if _INDEX is None:
        _INDEX = ci.Index.create()
    return _INDEX


_INDEX: ci.Index | None = None


def _c_api(name: str, argtypes: list[Any], restype: Any) -> Any:
    """Bind one libclang C function that the Python bindings do not register."""

    fn = _bindings.get(name)
    if fn is None:
        ensure_libclang()
        fn = getattr(ci.conf.lib, name)
        fn.argtypes = argtypes
        fn.restype = restype
        _bindings[name] = fn
    return fn


def is_function_inlined(cursor: ci.Cursor) -> bool:
    """Whether a function declaration carries ``inline`` (C99/C++ inline)."""

    fn = _c_api("clang_Cursor_isFunctionInlined", [ci.Cursor], ctypes.c_uint)
    return bool(fn(cursor))


def is_static_method(cursor: ci.Cursor) -> bool:
    """Whether a C++ member function is ``static``.

    libclang only exposes the ``static`` storage class on the in-class
    declaration; out-of-line definitions report no storage class, so the
    semantic C API is used to keep the flag on the winning definition record.
    """

    if cursor.kind != ci.CursorKind.CXX_METHOD:
        return False
    try:
        fn = _c_api("clang_CXXMethod_isStatic", [ci.Cursor], ctypes.c_uint)
        return bool(fn(cursor))
    except Exception:  # pragma: no cover - binding defensive
        return False


def call_argument_count(cursor: ci.Cursor) -> int:
    """Number of source-level arguments of a ``CALL_EXPR``."""

    fn = _c_api("clang_Cursor_getNumArguments", [ci.Cursor], ctypes.c_int)
    try:
        count = int(fn(cursor))
    except Exception:  # pragma: no cover - defensive against binding changes
        count = -1
    if count >= 0:
        return count
    # Fallback: the first child of a call expression is the callee.
    return max(0, len(list(cursor.get_children())) - 1)


@functools.lru_cache(maxsize=1)
def libclang_version() -> str:
    """Version string of the active libclang shared library (``""`` if unknown)."""

    ensure_libclang()
    try:
        value = ci.conf.lib.clang_getClangVersion()
        return value if isinstance(value, str) else str(value)
    except Exception:  # pragma: no cover - binding defensive
        return ""


def _libclang_major() -> int:
    match = re.search(r"(\d+)\.", libclang_version())
    return int(match.group(1)) if match else 0


def bitfield_width(cursor: ci.Cursor) -> int:
    """Bit width of a ``FIELD_DECL`` bitfield; ``-1`` when not derivable.

    ``clang_getFieldDeclBitWidth`` is used on libclang >= 16. The OH clang 15
    libclang (matched backend for OpenHarmony) crashes on libc++ bitfields whose
    width is an enum constant, so for older backends the width is read from the
    field's source tokens (numeric widths still resolve; symbolic ones return -1
    instead of crashing the worker).
    """

    if _libclang_major() >= 16:
        try:
            return int(cursor.get_bitfield_width())
        except Exception:  # pragma: no cover - binding defensive
            pass
    try:
        tokens = list(cursor.get_tokens())
    except Exception:  # pragma: no cover - defensive
        return -1
    for index, token in enumerate(tokens):
        if token.spelling == ":" and index + 1 < len(tokens):
            candidate = tokens[index + 1].spelling
            try:
                return int(candidate, 0)
            except ValueError:
                return -1
    return -1


def overridden_cursors(cursor: ci.Cursor) -> list[ci.Cursor]:
    """Overridden method declarations of a C++ method cursor.

    The Python bindings expose ``is_virtual_method``/``is_pure_virtual_method``
    but not ``clang_getOverriddenCursors``, so the C API is bound dynamically.
    Returns ``[]`` for non-methods, non-overriding methods or binding failures.
    """

    if cursor.kind != ci.CursorKind.CXX_METHOD:
        return []
    try:
        fn = _c_api(
            "clang_getOverriddenCursors",
            [
                ci.Cursor,
                ctypes.POINTER(ctypes.POINTER(ci.Cursor)),
                ctypes.POINTER(ctypes.c_uint),
            ],
            None,
        )
        dispose = _c_api(
            "clang_disposeOverriddenCursors",
            [ctypes.POINTER(ci.Cursor)],
            None,
        )
    except Exception:  # pragma: no cover - binding defensive
        return []
    pointers = ctypes.POINTER(ci.Cursor)()
    count = ctypes.c_uint(0)
    fn(cursor, ctypes.byref(pointers), ctypes.byref(count))
    total = int(count.value)
    result: list[ci.Cursor] = []
    try:
        for index in range(total):
            try:
                result.append(pointers[index])
            except Exception:  # pragma: no cover - defensive
                break
    finally:
        if total:
            dispose(pointers)
    return result


def is_macro_expansion(cursor: ci.Cursor) -> bool:
    """Whether a cursor comes from a macro expansion.

    libclang reports the expansion location for both ``cursor.location`` and its
    tokens' spelling locations only for real source tokens; tokens produced by a
    macro keep the spelling location inside the ``#define``. Tokenizing the
    extent is comparatively expensive, so the parser only calls this when
    ``ParseOptions.detect_macro_expansions`` is enabled.
    """

    try:
        token = next(iter(cursor.get_tokens()), None)
    except Exception:
        return False
    if token is None:
        return False
    loc = cursor.location
    token_loc = token.location
    token_key = (
        token_loc.file.name if token_loc.file else "",
        token_loc.line,
        token_loc.column,
    )
    cursor_key = (loc.file.name if loc.file else "", loc.line, loc.column)
    return token_key != cursor_key


# --------------------------------------------------------------------------- #
# Paths and source locations
# --------------------------------------------------------------------------- #


def file_name(location: ci.SourceLocation | None) -> str:
    """Absolute file name of a libclang location, or ``""`` when unknown."""

    if location is None:
        return ""
    source_file = location.file
    return source_file.name if source_file is not None else ""


@functools.lru_cache(maxsize=8192)
def _realpath_cached(path: str) -> str:
    """Memoized ``os.path.realpath``; parser workers see the same headers a lot."""

    try:
        return os.path.realpath(path)
    except OSError:  # pragma: no cover - defensive
        return path


class PathMapper:
    """Convert absolute libclang paths into repo-relative POSIX paths.

    ``repo_root`` is an absolute path (``CompilationUnit`` keeps absolute paths
    because libclang needs them). Paths outside the repository return ``None``:
    they are ignored by extraction, and their ``Sloc`` becomes empty/unknown.

    A build database may hand out the repo root and source paths through
    different symlink spellings (macOS ``/var`` -> ``/private/var``, ``/tmp``,
    user-facing symlinks). Both the normalized and the fully resolved root are
    therefore accepted, and a path that fails the plain prefix test is retried
    after ``realpath`` resolution.
    """

    def __init__(self, repo_root: str, base_dir: str = "") -> None:
        self.repo_root = os.path.abspath(repo_root)
        # libclang reports some include paths relative to its working directory
        # (we pass -working-directory <tu.directory>), so resolve against it.
        self.base_dir = os.path.abspath(base_dir) if base_dir else os.getcwd()
        self._roots = self._root_variants(self.repo_root)
        self._cache: dict[str, str | None] = {}

    @staticmethod
    def _root_variants(root: str) -> tuple[tuple[str, str], ...]:
        variants: list[tuple[str, str]] = []
        for candidate in (root, _realpath_cached(root)):
            normalized = os.path.normpath(candidate) if candidate else ""
            if normalized and all(normalized != known for known, _ in variants):
                variants.append((normalized, normalized.rstrip(os.sep) + os.sep))
        return tuple(variants)

    def _match(self, path: str) -> str | None:
        for root, prefix in self._roots:
            if path == root:
                return ""
            if path.startswith(prefix):
                return path[len(prefix):].replace(os.sep, "/")
        return None

    def absolute(self, path: str) -> str:
        """Absolute form of a libclang path (relative paths use the TU directory)."""

        if os.path.isabs(path):
            return os.path.normpath(path)
        return os.path.normpath(os.path.join(self.base_dir, path))

    def relative(self, absolute: str) -> str | None:
        cached = self._cache.get(absolute)
        if cached is not None or absolute in self._cache:
            return cached
        normalized = self.absolute(absolute) if absolute else ""
        result = self._match(normalized) if normalized else None
        if result is None and normalized:
            resolved = _realpath_cached(normalized)
            if resolved != normalized:
                result = self._match(resolved)
        if result == "":
            result = None  # the repo root itself is not a file entity
        self._cache[absolute] = result
        return result

    def relative_of_location(self, location: ci.SourceLocation | None) -> str | None:
        name = file_name(location)
        return self.relative(name) if name else None

    def contains(self, location: ci.SourceLocation | None) -> bool:
        return self.relative_of_location(location) is not None

    def sloc(self, start: ci.SourceLocation, end: ci.SourceLocation | None = None) -> Sloc:
        """Build a :class:`Sloc` from a cursor extent (start/end locations)."""

        rel = self.relative_of_location(start)
        if rel is None:
            return Sloc("")
        end_line = end.line if end is not None else 0
        end_col = end.column if end is not None else 0
        return Sloc(
            file=rel,
            line=start.line,
            col=start.column,
            end_line=end_line,
            end_col=end_col,
        )

    def sloc_for_cursor(self, cursor: ci.Cursor) -> Sloc:
        extent = cursor.extent
        return self.sloc(cursor.location, extent.end if extent else None)

    def sloc_for_location(self, location: ci.SourceLocation | None) -> Sloc:
        """Best-effort location for diagnostics; unknown file becomes ``""``."""

        if location is None:
            return Sloc("")
        rel = self.relative_of_location(location)
        if rel is None:
            return Sloc("")
        return Sloc(file=rel, line=location.line, col=location.column)


# --------------------------------------------------------------------------- #
# Cursor attribute normalization
# --------------------------------------------------------------------------- #

FUNCTION_KINDS: frozenset[ci.CursorKind] = frozenset(
    {
        ci.CursorKind.FUNCTION_DECL,
        ci.CursorKind.CXX_METHOD,
        ci.CursorKind.CONSTRUCTOR,
        ci.CursorKind.DESTRUCTOR,
        ci.CursorKind.CONVERSION_FUNCTION,
        # Uninstantiated templates surface only as *_TEMPLATE cursors in
        # libclang; recording them keeps the declaration in the database.
        ci.CursorKind.FUNCTION_TEMPLATE,
    }
)

TYPE_KINDS: frozenset[ci.CursorKind] = frozenset(
    {
        ci.CursorKind.STRUCT_DECL,
        ci.CursorKind.UNION_DECL,
        ci.CursorKind.ENUM_DECL,
        ci.CursorKind.CLASS_DECL,
        ci.CursorKind.CLASS_TEMPLATE,
        ci.CursorKind.TYPEDEF_DECL,
        # C++11 `using X = Y;` aliases (mapped to the schema's typedef kind).
        ci.CursorKind.TYPE_ALIAS_DECL,
    }
)

_FUNCTION_KIND_NAMES: Mapping[ci.CursorKind, str] = {
    ci.CursorKind.FUNCTION_DECL: "function",
    ci.CursorKind.CXX_METHOD: "method",
    ci.CursorKind.CONSTRUCTOR: "constructor",
    ci.CursorKind.DESTRUCTOR: "destructor",
    ci.CursorKind.CONVERSION_FUNCTION: "conversion",
    ci.CursorKind.FUNCTION_TEMPLATE: "function",
}

_TYPE_KIND_NAMES: Mapping[ci.CursorKind, str] = {
    ci.CursorKind.STRUCT_DECL: "struct",
    ci.CursorKind.UNION_DECL: "union",
    ci.CursorKind.ENUM_DECL: "enum",
    ci.CursorKind.CLASS_DECL: "class",
    ci.CursorKind.CLASS_TEMPLATE: "class",
    ci.CursorKind.TYPEDEF_DECL: "typedef",
    ci.CursorKind.TYPE_ALIAS_DECL: "typedef",
}

_LINKAGE_NAMES: Mapping[str, str] = {
    "EXTERNAL": "external",
    "INTERNAL": "internal",
    "UNIQUE_EXTERNAL": "external",
    "NO_LINKAGE": "none",
    "INVALID": "unknown",
}


def function_kind_name(cursor: ci.Cursor) -> str:
    return _FUNCTION_KIND_NAMES.get(cursor.kind, "unknown")


def type_kind_name(cursor: ci.Cursor) -> str:
    return _TYPE_KIND_NAMES.get(cursor.kind, "unknown")


def linkage_name(cursor: ci.Cursor) -> str:
    try:
        raw = cursor.linkage.name
    except Exception:  # pragma: no cover - defensive
        return "unknown"
    return _LINKAGE_NAMES.get(raw, "unknown")


def storage_class_name(cursor: ci.Cursor) -> str:
    try:
        raw = cursor.storage_class.name
    except Exception:  # pragma: no cover - defensive
        return ""
    return "" if raw == "NONE" else raw.lower()


def access_specifier_name(cursor: ci.Cursor) -> str:
    """C++ access specifier (``public``/``protected``/``private``); empty in C."""

    try:
        raw = cursor.access_specifier.name
    except Exception:  # pragma: no cover - defensive
        return ""
    return "" if raw == "INVALID" else raw.lower()


def is_variadic(cursor: ci.Cursor) -> bool:
    try:
        return bool(cursor.type.is_function_variadic())
    except Exception:  # pragma: no cover - defensive
        return False


def qualified_name(cursor: ci.Cursor, name: str) -> str:
    """``outer::inner::name`` for nested/member entities; plain name in C."""

    parts: list[str] = []
    parent = cursor.semantic_parent
    seen = 0
    while parent is not None and parent.kind not in (
        ci.CursorKind.TRANSLATION_UNIT,
        ci.CursorKind.INVALID_FILE,
    ):
        spelling = parent.spelling
        if parent.kind == ci.CursorKind.NAMESPACE and not spelling:
            # Anonymous namespace: keep the scope visible in the qualified name
            # while its file-scoped USR already keeps ids unique.
            parts.append("(anonymous namespace)")
        elif spelling and parent.kind in (
            ci.CursorKind.NAMESPACE,
            ci.CursorKind.STRUCT_DECL,
            ci.CursorKind.UNION_DECL,
            ci.CursorKind.ENUM_DECL,
            ci.CursorKind.CLASS_DECL,
            ci.CursorKind.CLASS_TEMPLATE,
        ):
            parts.append(spelling)
        seen += 1
        if seen > 32:  # defensive: never loop forever on malformed ASTs
            break
        parent = parent.semantic_parent
    if not parts:
        return name
    parts.reverse()
    parts.append(name)
    return "::".join(parts)


def type_usize(cursor: ci.Cursor) -> tuple[int, int]:
    """``(size_bytes, align_bytes)`` of a type cursor; ``-1`` when unknown."""

    kind = cursor.kind
    try:
        type_value = cursor.type
        size = int(type_value.get_size())
        align = int(type_value.get_align())
    except Exception:  # pragma: no cover - libclang layout errors are ints
        return -1, -1
    if kind == ci.CursorKind.TYPEDEF_DECL and not cursor.is_definition():
        return -1, -1
    return max(-1, size), max(-1, align)


def underlying_type_spelling(cursor: ci.Cursor) -> str:
    try:
        if cursor.kind in (ci.CursorKind.TYPEDEF_DECL, ci.CursorKind.TYPE_ALIAS_DECL):
            return cursor.underlying_typedef_type.spelling
        if cursor.kind == ci.CursorKind.ENUM_DECL:
            return cursor.enum_type.spelling
    except Exception:  # pragma: no cover - defensive
        return ""
    return ""
