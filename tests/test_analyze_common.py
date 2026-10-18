"""Shared helpers for analyze tests: a protocol-compatible fake ParsedUnit.

Production ``ca.analyze`` only consumes the ``ParsedUnit`` protocol from
``ca.core.interfaces``. The parse module owns the real implementation; until
it is available these tests build an equivalent object directly with libclang
over ``tests/fixtures/df_*.c``. Keeping the helper here also proves the
analyzer works against the documented protocol rather than parser internals.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any

import clang.cindex as c

from ca.analyze import AnalyzeOptions, analyze_function
from ca.core import ids as core_ids
from ca.core.schema import (
    AnalysisResult,
    CompilationUnit,
    FunctionRecord,
    Sloc,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
LIBRARY_NAMES = ("libclang.dylib", "libclang.so", "libclang.dll")
DEFAULT_ARGS = ("-std=c11",)


def _configure_libclang() -> None:
    """Load the libclang shipped with the *active* Python environment.

    The test suite runs on macOS and Linux (server) with different venvs, so the
    library is discovered through the installed ``clang`` package instead of a
    hard-coded repo path. ``ca.parse.cursor_utils.ensure_libclang`` is the
    production loader; it no-ops when the bindings already have a library (for
    example when ``clang`` resolved ``libclang.so`` automatically). Setting an
    explicit file is only a fallback and only when the file exists, so a broken
    repo-local guess can never poison the process-wide libclang configuration.
    """

    if getattr(_configure_libclang, "_done", False):
        return
    configured = False
    try:
        from ca.parse.cursor_utils import ensure_libclang

        ensure_libclang()
        configured = True
    except Exception:  # pragma: no cover - parse module unavailable
        configured = False
    if not configured:
        try:
            spec = importlib.util.find_spec("clang")
            native_dir = (
                Path(spec.origin).resolve().parent / "native"
                if spec is not None and spec.origin
                else None
            )
            if native_dir is not None:
                for name in LIBRARY_NAMES:
                    candidate = native_dir / name
                    if candidate.is_file():
                        c.Config.set_library_file(str(candidate))
                        break
        except Exception:  # pragma: no cover - defensive
            pass
    _configure_libclang._done = True  # type: ignore[attr-defined]


def repo_rel(path: str) -> str:
    real = os.path.realpath(path)
    rel = os.path.relpath(real, str(REPO_ROOT))
    return rel.replace(os.sep, "/")


def sloc_for(cursor: Any) -> Sloc:
    path = cursor.location.file.name if cursor.location.file is not None else ""
    extent = cursor.extent
    return Sloc(
        file=repo_rel(path) if path else "",
        line=int(extent.start.line or 0),
        col=int(extent.start.column or 0),
        end_line=int(extent.end.line or 0),
        end_col=int(extent.end.column or 0),
    )


class FakeParsedUnit:
    """Minimal ``ParsedUnit`` protocol implementation backed by libclang."""

    def __init__(
        self,
        source_path: str | os.PathLike[str],
        args: tuple[str, ...] = DEFAULT_ARGS,
        repo_root: str | os.PathLike[str] = REPO_ROOT,
    ) -> None:
        _configure_libclang()
        self.source_path = Path(source_path).resolve()
        self.repo_root = str(Path(repo_root).resolve())
        index = c.Index.create()
        self._clang_tu = index.parse(
            str(self.source_path), args=list(args)
        )
        self.parse_ok = True
        self.elapsed_ms = 0.0
        self.diagnostics: list[Any] = []
        self.types: list[Any] = []
        self.fields: list[Any] = []
        self.calls: list[Any] = []
        relative = os.path.relpath(str(self.source_path), self.repo_root).replace(os.sep, "/")
        self.tu = CompilationUnit(
            id=core_ids.tu_id(relative, str(self.source_path.parent), args),
            file=relative,
            directory=str(self.source_path.parent),
            repo_root=self.repo_root,
            arguments=tuple(args),
            language="c",
            source="synthetic",
        )
        self._cursors: dict[str, Any] = {}
        self.functions: list[FunctionRecord] = []
        self._collect_functions()

    # -- protocol --------------------------------------------------------
    def clang_tu(self) -> Any:
        return self._clang_tu

    def definition_cursor(self, function_id: str) -> Any | None:
        return self._cursors.get(function_id)

    def parse_result(self) -> Any:  # pragma: no cover - not needed by analyze
        raise NotImplementedError

    # -- test conveniences ----------------------------------------------
    def _collect_functions(self) -> None:
        for cursor in _walk(self._clang_tu.cursor):
            if cursor.kind != c.CursorKind.FUNCTION_DECL:
                continue
            try:
                if not cursor.is_definition():
                    continue
            except Exception:  # pragma: no cover
                continue
            file_obj = cursor.location.file
            if file_obj is None or os.path.realpath(file_obj.name) != str(self.source_path):
                continue
            usr = cursor.get_usr()
            linkage = "external" if cursor.linkage == c.LinkageKind.EXTERNAL else "internal"
            sloc = sloc_for(cursor)
            function_id = core_ids.function_id(
                name=cursor.spelling, usr=usr, sloc=sloc, linkage=linkage
            )
            record = FunctionRecord(
                id=function_id,
                tu_id=self.tu.id,
                name=cursor.spelling,
                qualified_name=cursor.spelling,
                usr=usr,
                kind="function",
                sloc=sloc,
                is_definition=True,
                return_type=cursor.result_type.spelling,
                display_name=cursor.spelling,
                linkage=linkage,
            )
            self.functions.append(record)
            self._cursors[function_id] = cursor

    def function(self, name: str) -> FunctionRecord:
        for record in self.functions:
            if record.name == name:
                return record
        raise KeyError(f"function {name!r} not found; have {[f.name for f in self.functions]}")

    def analyze(self, name: str, options: Any = None) -> AnalysisResult:
        return analyze_function(self, self.function(name), options)

    def analyze_all(self, options: Any = None) -> dict[str, AnalysisResult]:
        return {
            record.name: analyze_function(self, record, options)
            for record in self.functions
        }


def _walk(cursor: Any):
    for child in cursor.get_children():
        yield child
        yield from _walk(child)


_UNIT_CACHE: dict[tuple[str, tuple[str, ...]], FakeParsedUnit] = {}


def unit_for_fixture(
    name: str, args: tuple[str, ...] = DEFAULT_ARGS
) -> FakeParsedUnit:
    path = FIXTURES / name
    key = (str(path), tuple(args))
    cached = _UNIT_CACHE.get(key)
    if cached is None:
        cached = FakeParsedUnit(path, args=args)
        _UNIT_CACHE[key] = cached
    return cached


def analyze_fixture(
    name: str, function_name: str, options: Any = None
) -> AnalysisResult:
    return unit_for_fixture(name).analyze(function_name, options)


def record_sorted_by_position(records: list[Any]) -> bool:
    key = [(record.sloc.line, record.sloc.col) for record in records]
    return key == sorted(key)


__all__ = [
    "DEFAULT_ARGS",
    "FIXTURES",
    "FakeParsedUnit",
    "REPO_ROOT",
    "analyze_fixture",
    "record_sorted_by_position",
    "repo_rel",
    "sloc_for",
    "unit_for_fixture",
]
