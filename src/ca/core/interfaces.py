"""Cross-module contracts. Lead-owned: change only with a recorded decision.

Import direction (no cycles)::

    core  <-  build, parse, analyze, store  <-  pipeline  <-  cli

Module entry points every stage must expose from its package ``__init__``:

``ca.build``
    - ``load_compilation_database(path, repo_root=None) -> list[CompilationUnit]``
    - ``capture_build(command, output, repo_root, jobs=1) -> list[CompilationUnit]``
    - ``reconstruct_kernel_commands(build_dir, repo_root) -> list[CompilationUnit]``
    - ``synthesize_for_files(files, repo_root, language="c", extra_args=()) ->
      list[CompilationUnit]`` (source is marked ``"synthesized"``)

``ca.parse``
    - ``parse_compilation_unit(tu, options=None) -> ParsedUnit``

``ca.analyze``
    - ``analyze_function(unit, function, options=None) -> AnalysisResult``

``ca.store``
    - ``SqliteStore(db_path)`` implementing :class:`RecordStore`

``ca.pipeline``
    - ``run_index(...) -> IndexSummary``; ``ca.cli.main.main(argv=None) -> int``
"""

from __future__ import annotations

from typing import Any, Protocol, Sequence, runtime_checkable

from ca.core.schema import (
    AnalysisResult,
    CallRecord,
    CompilationUnit,
    Diagnostic,
    FieldRecord,
    FunctionRecord,
    IndexSummary,
    ParseResult,
    TypeRecord,
)


@runtime_checkable
class ParsedUnit(Protocol):
    """One parsed TU that still owns live libclang objects.

    Implementations live in ``ca.parse`` and stay inside a worker process; they
    are never pickled. Plain :class:`ParseResult` is what crosses process and
    database boundaries.
    """

    tu: CompilationUnit
    functions: Sequence[FunctionRecord]
    types: Sequence[TypeRecord]
    fields: Sequence[FieldRecord]
    calls: Sequence[CallRecord]
    diagnostics: Sequence[Diagnostic]
    parse_ok: bool
    elapsed_ms: float

    def parse_result(self) -> ParseResult:
        """Detach the records into a picklable result (no live cursors)."""

    def clang_tu(self) -> Any:
        """Return the underlying ``clang.cindex.TranslationUnit``."""

    def definition_cursor(self, function_id: str) -> Any | None:
        """Return the Clang cursor for a function definition/declaration id."""


@runtime_checkable
class Parser(Protocol):
    def parse(self, tu: CompilationUnit) -> ParsedUnit: ...


@runtime_checkable
class Analyzer(Protocol):
    def analyze_function(self, unit: ParsedUnit, function: FunctionRecord) -> AnalysisResult: ...


@runtime_checkable
class RecordStore(Protocol):
    """Persistence + query contract implemented by ``ca.store``.

    Lifecycle: ``open`` -> ``begin`` -> ``add_*`` (streaming) -> ``commit`` ->
    ``finalize`` -> ``close``. ``finalize`` creates indexes and ANALYZEs the
    database once all rows are loaded; queries are only guaranteed after it.
    """

    def open(self) -> None: ...

    def begin(self) -> None: ...

    def add_parse_result(self, result: ParseResult) -> None: ...

    def add_analysis_result(self, result: AnalysisResult) -> None: ...

    def commit(self) -> None: ...

    def finalize(self) -> None: ...

    def close(self) -> None: ...

    # Query methods return plain dicts with stable keys documented in the
    # store module; the CLI and benchmarks rely on those keys.
    def query_functions(self, pattern: str, limit: int = 50) -> list[dict[str, Any]]: ...

    def query_callers(self, symbol: str, limit: int = 200) -> list[dict[str, Any]]: ...

    def query_callees(self, symbol: str, limit: int = 200) -> list[dict[str, Any]]: ...

    def query_function_detail(self, symbol: str) -> dict[str, Any]: ...

    def query_branches(self, symbol: str) -> list[dict[str, Any]]: ...

    def query_assignments(self, symbol: str) -> list[dict[str, Any]]: ...

    def query_returns(self, symbol: str) -> list[dict[str, Any]]: ...

    def query_resource_ops(self, symbol: str) -> list[dict[str, Any]]: ...

    def query_findings(self, kind: str | None = None, limit: int = 200) -> list[dict[str, Any]]: ...

    def stats(self) -> dict[str, Any]: ...

    def iter_records(
        self,
        kinds: tuple[str, ...] | None = None,
        limit: int | None = None,
    ) -> "Iterator[dict[str, Any]]":
        """Stream all persisted records for export and accuracy evaluation.

        ``kinds`` selects record kinds; ``None`` means all persisted kinds:
        ``compilation_unit``, ``function``, ``type``, ``field``, ``call``,
        ``cfg_block``, ``cfg_edge``, ``branch``, ``assignment``, ``return``,
        ``resource_op``, ``finding``.

        Every dict contains ``kind`` and ``schema`` plus the record's table
        columns. Flattening conventions (same as the query methods):

        - a record's source range is ``file, line, col, end_line, end_col``;
        - multi-value fields (``var_names``, ``release_ids``, ``path_blocks``,
          ``evidence``, ``params`` where stored) are decoded to lists, not JSON
          strings;
        - IDs are the stable ``ca.core.ids`` values; paths are repo-relative.
        """


@runtime_checkable
class Indexer(Protocol):
    def run_index(
        self,
        repo_root: str,
        compilation_units: Sequence[CompilationUnit],
        db_path: str,
        *,
        jobs: int = 1,
        max_tus: int | None = None,
    ) -> IndexSummary: ...
