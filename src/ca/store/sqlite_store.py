"""SQLite persistence for CA: :class:`SqliteStore` (implements ``RecordStore``).

Lifecycle (see ``ca.core.interfaces.RecordStore``)::

    store = SqliteStore("path/to/index.db")
    store.open()             # connect + PRAGMAs + create tables if missing
    store.begin()            # optional; add_* lazily start a transaction too
    store.add_parse_result(parse_result)
    store.add_analysis_result(analysis_result)   # one per analyzed function
    store.commit()           # end the current transaction (repeat per batch)
    store.finalize()         # placeholders + secondary indexes + ANALYZE
    store.close()            # idempotent
    # ... queries are guaranteed after finalize()

Ingest uses WAL, ``synchronous=NORMAL``, ``temp_store=MEMORY``, a 256 MiB page
cache and a 64 MiB WAL autocheckpoint threshold (measured: ~1.4x faster bulk
load than SQLite defaults), inserts with ``executemany`` and
``INSERT OR IGNORE`` (function rows prefer definitions over declarations), and
defers secondary index creation to ``finalize()``. Duplicate stable ids are
ignored, so re-ingesting the same header parsed from many translation units is
safe.

Schema versioning: ``meta.schema_version`` tracks the core contract
(``ca.core.schema.SCHEMA_VERSION``) while ``meta.store_schema_version`` tracks
the additive physical layout (``"8"`` adds ``call_targets``, the
``workspaces``/``projects``/``variants`` metadata tables, ``deep_status``, the
cross-TU callback tables ``callback_registrations``/``callback_target_links``,
the backend job queue ``jobs``/``job_events``, virtual-dispatch metadata and
``tu_includes``; v8 also removes the legacy candidate ``confidence`` columns).
Upgrades are
non-destructive: ``open()`` applies ``CREATE TABLE IF NOT EXISTS`` and
``finalize()`` adds indexes, so a phase-2 database stays queryable before
``finalize()`` and gains the new tables/indexes afterwards.

Incremental maintenance (``ca.store.incremental``; used by ``ca update`` /
``ca watch``): ``remove_units(tu_ids) -> {table: deleted_rows}`` cascades one or
more translation units out of every dependent table in one transaction
(tus/functions/types/fields/calls/CFG/branches/assignments/returns/resource_ops/
findings/call_targets/callback registrations+links/deep_status/tu_includes) and
maintains ``functions_fts`` incrementally; ``replace_units(units, *,
deep_ready=True) -> {tu_ids, removed, added, fts_deleted, fts_inserted,
deep_ready}`` deletes and re-inserts TUs in one transaction (units are
``{parse, analyses, callbacks, include_paths}`` mappings or
``(parse_result, analyses[, callbacks[, include_paths]])`` tuples);
``replace_unit(parse_result, analyses=(), *, callbacks=(), include_paths=None,
deep_ready=True)`` is the single-TU wrapper. No ``finalize()`` full rebuild is
required; FTS5 rows are updated with the external-content delete/insert
commands when ``meta.fts_ready='1'``. Unknown TU ids are idempotent no-ops.

Query result keys (CLI/benchmark contract, flattened ``Sloc``):
``file, line, col, end_line, end_col``.

- ``query_functions(pattern, limit=50)`` ->
  ``id, name, qualified_name, kind, file, line, is_definition, is_static, linkage``
  (exact id/name/qualified_name matches win; the FTS5 trigram index serves
  substring search, falling back to ``LIKE`` for 1-2 character patterns or
  databases without a usable FTS5 index).
- ``query_callers(symbol, limit=200)`` ->
  ``caller_id, caller_name, caller_file, caller_line, callee_name, call_file,
  call_line, is_resolved, kind, source, candidate, reason``.
- ``query_callees(symbol, limit=200)`` ->
  ``callee_id, callee_name, callee_file, callee_line, call_file, call_line,
  is_resolved, kind, source, candidate, reason``.
  Both merge direct ``calls`` rows (``source="call"``, ``kind`` = calls.kind)
  with ``call_targets`` candidate edges (``source="call_target"``) and
  query-time ``virtual_candidate`` edges. Candidate targets are a *set* (no
  confidence); duplicate edges are collapsed by natural key
  ``(caller_id, callee_id, callee_name, call_file, call_line)`` with priority
  direct < call_target < virtual_candidate and their audit ``reason`` strings
  joined with ``"; "``. ``query_callers`` also matches unresolved call sites by
  their redundant ``callee_name``. Ordering is ``(call_file, call_line,
  caller/callee id, callee name)`` and limit applies after dedupe.
- ``add_call_targets(records) -> int`` inserts candidate ``CallTargetRecord``
  objects (or field mappings with ``id``) outside an analysis batch, for
  post-processing passes on an existing database; idempotent, returns inserted
  rows. The new edges are visible to ``query_callees``/``query_call_graph``
  immediately (``source="call_target"``, ``candidate=True``).
- ``query_call_targets(symbol, direction="callee"|"caller", limit=200)`` ->
  ``call_id, caller_id, caller_name, callee_id, callee_name, callee_file,
  callee_line, call_file, call_line, kind, reason, source, candidate``
  (candidate edges only; natural-key deduped, no confidence).
- ``query_function_detail(symbol)`` -> ``{function, deep_ready, branches,
  assignments, returns, resource_ops, findings}`` (``deep_ready`` reports whether
  the on-demand deep pass has completed for the function; base facts are always
  returned, deep lists are empty in an L0-only database); ``function`` is a ``FunctionRecord`` dict
  (``id, tu_id, name, qualified_name, usr, kind, file, line, col, end_line,
  end_col, is_definition, return_type, signature, display_name, storage_class,
  linkage, is_static, is_inline, is_variadic, parent_id, params``) or ``None``
  when nothing matches; missing fact lists are empty.
- ``query_branches(symbol)`` -> ``id, function_id, block_id, file, line, col,
  end_line, end_col, kind, condition, var_names, true_block, false_block,
  is_negated, case_value``
- ``query_assignments(symbol)`` -> ``id, function_id, block_id, file, line,
  col, end_line, end_col, lhs, rhs, var_names, is_decl_init, operator,
  type_spelling``
- ``query_returns(symbol)`` -> ``id, function_id, block_id, file, line, col,
  end_line, end_col, expr, var_names, is_implicit``
- ``query_resource_ops(symbol)`` -> ``id, function_id, block_id, file, line,
  col, end_line, end_col, op, resource_kind, api_name, target, target_vars,
  call_id, matcher``
- ``query_findings(kind=None, limit=200)`` -> ``id, function_id, function_name,
  kind, resource_kind, message, severity, file, line`` (file/line come from the
  owning function; ``FindingRecord`` has no source range of its own).
- ``query_cfg(symbol, max_blocks=500, max_edges=1000)`` ->
  ``{function_ids, blocks, edges, branches, truncated}``. Blocks are
  ``cfg_blocks`` rows (``index``, ``kind``, ``terminator``, line range,
  ``statement_count``) with branch annotations (``condition``, ``branch_kind``,
  ``true_block``, ``false_block``) and ``is_entry``/``is_exit`` flags; edges are
  ``cfg_edges`` rows with a display ``label`` (``true``/``false`` for branch
  edges); branches are decoded ``branches`` rows. Read-only and bounded.
- ``stats()`` -> ``{tables: {table: count}, db_bytes, schema_version,
  sqlite_version}``
- ``iter_records(kinds=None, limit=None)`` streams export dicts with ``kind``
  and ``schema`` (``ca.core.schema.SCHEMA_VERSION``); kinds match the
  ``CompilationUnit.KIND``/record ``KIND`` values: ``compilation_unit,
  function, type, field, call, call_target, cfg_block, cfg_edge, branch,
  assignment, return, resource_op, finding``. Envelope records (``parse_result``,
  ``analysis_result``) and parse diagnostics are not persisted.

Symbol resolution matches ``id``/``name``/``qualified_name`` exactly first
(case-sensitive), then performs a substring search: the FTS5 trigram index
(``functions_fts``, built by ``finalize()``) serves substrings of 3+ characters
through an index instead of a full-table ``LIKE``; shorter patterns and databases
without a usable FTS5 module fall back to ``LIKE '%symbol%'`` with wildcards
escaped. Both paths keep the same ordering (definitions first, then file/line/id),
so ``limit`` results are stable. Unresolved callees (calls whose ``callee_id``
has no function row) receive placeholder declaration rows in ``finalize()``
(``is_definition=0``, name/usr taken from the call's redundant columns), so
callers/callees joins are complete.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterator

from ca.core.schema import SCHEMA_VERSION, AnalysisResult, ParseResult
from ca.store import (
    callbacks,
    deep,
    graph,
    incremental,
    jobs as job_queue,
    migrations,
    queries,
    virtual_dispatch,
    workspace,
    writer,
)

_SCHEMA_FILE = Path(__file__).with_name("schema.sql")
_MEMORY_PATHS = {":memory:", "file::memory:", "file::memory:?cache=shared"}


class SqliteStore:
    """SQLite-backed :class:`ca.core.interfaces.RecordStore`."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self._connection: sqlite3.Connection | None = None
        self._queries: queries.QueryEngine | None = None
        self._graph: graph.CallGraph | None = None
        self._registry: workspace.WorkspaceRegistry | None = None
        self._deep: deep.DeepStatusStore | None = None
        self._callbacks: callbacks.CallbackRegistry | None = None
        self._jobs: job_queue.JobQueue | None = None
        self._in_transaction = False

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def open(self) -> None:
        """Connect, tune PRAGMAs and create missing tables (idempotent)."""

        if self._connection is not None:
            return
        if self.db_path not in _MEMORY_PATHS and not self.db_path.startswith("file:"):
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        # WAL: concurrent readers while one writer appends. NORMAL is the right
        # durability level for a rebuildable index. The 256 MiB page cache keeps
        # random TEXT-PK B-tree inserts from thrashing during bulk load; a large
        # autocheckpoint threshold avoids copying the WAL into the main file on
        # every 4 MiB written (both measured, see .tmp bench).
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA temp_store=MEMORY")
        connection.execute("PRAGMA cache_size=-262144")
        connection.execute("PRAGMA wal_autocheckpoint=16384")
        connection.execute("PRAGMA mmap_size=268435456")
        connection.execute("PRAGMA busy_timeout=30000")
        # Referential integrity is a pipeline property, not a per-row check.
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.executescript(_SCHEMA_FILE.read_text(encoding="utf-8"))
        # Additive physical migrations (task-37 virtual dispatch metadata):
        # virtual_overrides/type_bases tables plus functions/types dispatch
        # columns on databases created before schema v7.
        virtual_dispatch.migrate(connection)
        # Schema v8: drop legacy candidate-confidence columns (idempotent; a
        # failure keeps the old column and logs a warning instead of failing).
        migrations.migrate_confidence_columns(connection)
        connection.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
            (SCHEMA_VERSION,),
        )
        # core contract version vs. physical store layout version (additive tables)
        connection.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (writer.STORE_SCHEMA_META_KEY, writer.STORE_SCHEMA_VERSION),
        )
        self._connection = connection
        self._queries = queries.QueryEngine(connection)
        self._graph = graph.CallGraph(self._queries)
        self._registry = workspace.WorkspaceRegistry(connection)
        self._deep = deep.DeepStatusStore(connection, self._queries)
        self._callbacks = callbacks.CallbackRegistry(connection)
        self._jobs = job_queue.JobQueue(connection)

    @property
    def connection(self) -> sqlite3.Connection:
        """Raw connection for diagnostics (EXPLAIN QUERY PLAN, benchmarks)."""

        return self._require_connection()

    @property
    def closed(self) -> bool:
        return self._connection is None

    def close(self) -> None:
        """Commit any open transaction and close the connection (idempotent)."""

        if self._connection is None:
            return
        try:
            if self._in_transaction:
                self._connection.execute("COMMIT")
                self._in_transaction = False
        finally:
            self._connection.close()
            self._connection = None
            self._queries = None
            self._graph = None
            self._registry = None
            self._deep = None
            self._callbacks = None
            self._jobs = None

    def __enter__(self) -> "SqliteStore":
        self.open()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # Write path
    # ------------------------------------------------------------------ #
    def begin(self) -> None:
        """Start an explicit transaction; no-op when one is already open."""

        connection = self._require_connection()
        if self._in_transaction:
            return
        connection.execute("BEGIN")
        self._in_transaction = True

    def add_parse_result(self, result: ParseResult) -> None:
        self._ensure_transaction()
        writer.add_parse_result(self._require_connection(), result)

    def add_analysis_result(self, result: AnalysisResult) -> None:
        self._ensure_transaction()
        writer.add_analysis_result(self._require_connection(), result)

    def add_tu_includes(self, tu_id: str, include_paths: Any) -> int:
        """Insert direct repo-local includes for one TU (idempotent)."""

        self._ensure_transaction()
        return writer.add_tu_includes(
            self._require_connection(), str(tu_id), list(include_paths or [])
        )

    def tu_includes(self, tu_id: str) -> list[str]:
        """Direct repo-relative include paths recorded for one TU."""

        rows = self._require_connection().execute(
            "SELECT include_path FROM tu_includes WHERE tu_id = ? ORDER BY include_path",
            (str(tu_id),),
        ).fetchall()
        return [str(row[0]) for row in rows]

    def has_tu_includes(self) -> bool:
        """True when this index carries any include-dependency rows."""

        try:
            row = self._require_connection().execute(
                "SELECT 1 FROM tu_includes LIMIT 1"
            ).fetchone()
        except Exception:
            return False
        return row is not None

    def dependent_tu_ids(self, changed_files: Any) -> list[str]:
        """TU ids whose direct includes contain any of ``changed_files``."""

        paths = [str(path) for path in changed_files or [] if str(path)]
        if not paths:
            return []
        placeholders = ",".join("?" for _ in paths)
        rows = self._require_connection().execute(
            "SELECT DISTINCT tu_id FROM tu_includes WHERE include_path IN ("
            + placeholders
            + ") ORDER BY tu_id",
            paths,
        ).fetchall()
        return [str(row[0]) for row in rows]

    def dependent_tus(self, changed_files: Any) -> list[dict[str, Any]]:
        """Dependent TU rows (``id,file,directory,repo_root``) for changed includes."""

        paths = [str(path) for path in changed_files or [] if str(path)]
        if not paths:
            return []
        placeholders = ",".join("?" for _ in paths)
        rows = self._require_connection().execute(
            "SELECT DISTINCT tus.id, tus.file, tus.directory, tus.repo_root"
            " FROM tus JOIN tu_includes ON tu_includes.tu_id = tus.id"
            " WHERE tu_includes.include_path IN (" + placeholders + ")"
            " ORDER BY tus.file",
            paths,
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ #
    # Incremental TU maintenance (ca update / ca watch)
    # ------------------------------------------------------------------ #
    def remove_units(self, tu_ids: Any) -> dict[str, int]:
        """Cascade-delete all rows owned by ``tu_ids`` (atomic, idempotent)."""

        return incremental.remove_units(self, tu_ids)

    def replace_units(self, units: Any, *, deep_ready: bool = True) -> dict[str, Any]:
        """Delete + re-insert TUs in one transaction (incremental write-back)."""

        return incremental.replace_units(self, units, deep_ready=deep_ready)

    def replace_unit(
        self,
        parse_result: Any,
        analyses: Any = (),
        *,
        callbacks: Any = (),
        include_paths: Any = None,
        deep_ready: bool = True,
    ) -> dict[str, Any]:
        """Single-TU wrapper around :meth:`replace_units`."""

        return incremental.replace_unit(
            self,
            parse_result,
            analyses,
            callbacks=callbacks,
            include_paths=include_paths,
            deep_ready=deep_ready,
        )

    def add_call_targets(self, records: Any) -> int:
        """Insert candidate call edges directly (post-processing passes)."""

        self._ensure_transaction()
        return writer.add_call_targets(self._require_connection(), records)

    def commit(self) -> None:
        if not self._in_transaction or self._connection is None:
            return
        self._connection.execute("COMMIT")
        self._in_transaction = False

    def rollback(self) -> None:
        """Roll back the current explicit transaction (idempotent no-op)."""

        if not self._in_transaction or self._connection is None:
            return
        try:
            self._connection.execute("ROLLBACK")
        finally:
            self._in_transaction = False

    def finalize(self) -> None:
        """Finish ingest: placeholder callees, secondary indexes, ANALYZE."""

        connection = self._require_connection()
        self.commit()
        writer.finalize(connection)
        # FTS5 readiness changed; let query caches pick up the new index.
        self._require_queries().reset_caches()

    # ------------------------------------------------------------------ #
    # Query API (delegated; key contract in the module docstring)
    # ------------------------------------------------------------------ #
    def query_functions(self, pattern: str, limit: int = 50) -> list[dict[str, Any]]:
        return self._require_queries().query_functions(pattern, limit)

    def query_callers(self, symbol: str, limit: int = 200) -> list[dict[str, Any]]:
        return self._require_queries().query_callers(symbol, limit)

    def query_callees(self, symbol: str, limit: int = 200) -> list[dict[str, Any]]:
        return self._require_queries().query_callees(symbol, limit)

    def query_function_detail(self, symbol: str) -> dict[str, Any]:
        return self._require_queries().query_function_detail(symbol)

    def query_branches(self, symbol: str) -> list[dict[str, Any]]:
        return self._require_queries().query_branches(symbol)

    def query_assignments(self, symbol: str) -> list[dict[str, Any]]:
        return self._require_queries().query_assignments(symbol)

    def query_returns(self, symbol: str) -> list[dict[str, Any]]:
        return self._require_queries().query_returns(symbol)

    def query_resource_ops(self, symbol: str) -> list[dict[str, Any]]:
        return self._require_queries().query_resource_ops(symbol)

    def query_findings(self, kind: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        return self._require_queries().query_findings(kind, limit)

    def query_cfg(
        self, symbol: str, max_blocks: int = 500, max_edges: int = 1000
    ) -> dict[str, Any]:
        return self._require_queries().query_cfg(symbol, max_blocks, max_edges)

    def stats(self) -> dict[str, Any]:
        return self._require_queries().stats()

    def iter_records(
        self,
        kinds: tuple[str, ...] | None = None,
        limit: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        return self._require_queries().iter_records(kinds, limit)

    # ------------------------------------------------------------------ #
    # Call graph (merged direct + candidate edges)
    # ------------------------------------------------------------------ #
    def query_call_targets(
        self, symbol: str, direction: str = "callee", limit: int = 200
    ) -> list[dict[str, Any]]:
        return self._require_queries().query_call_targets(symbol, direction, limit)

    def query_call_graph(
        self,
        symbol: str,
        direction: str = "both",
        depth: int = 1,
        limit: int = 500,
    ) -> dict[str, Any]:
        return self._require_graph().query_call_graph(symbol, direction, depth, limit)

    def query_call_paths(
        self,
        source: str,
        target: str | None = None,
        max_depth: int = 6,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        return self._require_graph().query_call_paths(source, target, max_depth, limit)

    def find_call_cycles(
        self, symbol: str, max_depth: int = 6, limit: int = 20
    ) -> list[dict[str, Any]]:
        return self._require_graph().find_call_cycles(symbol, max_depth, limit)

    # ------------------------------------------------------------------ #
    # Multi-repo workspace / project / variant metadata
    # ------------------------------------------------------------------ #
    def upsert_workspace(
        self,
        path: str,
        name: str = "",
        kind: str = "local",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        return self._require_registry().upsert_workspace(path, name, kind, metadata)

    def register_projects(
        self,
        workspace_id: str,
        projects: Any,
        *,
        replace: bool = False,
    ) -> int:
        return self._require_registry().register_projects(
            workspace_id, projects, replace=replace
        )

    def register_variant(
        self,
        workspace_id: str,
        name: str,
        product: str = "",
        board: str = "",
        version: str = "",
        db_path: str = "",
        metadata: dict[str, Any] | None = None,
        is_active: bool = False,
    ) -> str:
        return self._require_registry().register_variant(
            workspace_id, name, product, board, version, db_path, metadata, is_active
        )

    def get_workspace(self, identifier: str) -> dict[str, Any] | None:
        return self._require_registry().get_workspace(identifier)

    def list_workspaces(self, limit: int = 0) -> list[dict[str, Any]]:
        return self._require_registry().list_workspaces(limit)

    def list_projects(self, workspace_id: str, limit: int = 0) -> list[dict[str, Any]]:
        return self._require_registry().list_projects(workspace_id, limit)

    def list_variants(self, workspace_id: str) -> list[dict[str, Any]]:
        return self._require_registry().list_variants(workspace_id)

    def get_variant(self, variant_id: str) -> dict[str, Any] | None:
        return self._require_registry().get_variant(variant_id)

    def set_active_variant(self, workspace_id: str, variant_id: str) -> str | None:
        return self._require_registry().set_active_variant(workspace_id, variant_id)

    def remove_variant(self, variant_id: str) -> bool:
        return self._require_registry().remove_variant(variant_id)

    def remove_project(self, project_id: str) -> bool:
        return self._require_registry().remove_project(project_id)

    def remove_workspace(self, identifier: str) -> dict[str, int] | None:
        return self._require_registry().remove_workspace(identifier)

    def workspace_summary(self, identifier: str) -> dict[str, Any]:
        return self._require_registry().workspace_summary(identifier)

    def attach_database(self, db_path: str, alias: str) -> None:
        self._require_registry().attach_database(db_path, alias)

    def detach_database(self, alias: str) -> None:
        self._require_registry().detach_database(alias)

    def attached_databases(self) -> list[dict[str, Any]]:
        return self._require_registry().attached_databases()

    # ------------------------------------------------------------------ #
    # Cross-TU callback / function-pointer registration facts
    # ------------------------------------------------------------------ #
    def add_callback_registrations(self, records: Any) -> int:
        return self._require_callbacks().add_callback_registrations(records)

    def query_callbacks(self, **filters: Any) -> list[dict[str, Any]]:
        return self._require_callbacks().query_callbacks(**filters)

    def query_callback_registrations(self, **filters: Any) -> list[dict[str, Any]]:
        return self._require_callbacks().query_callback_registrations(**filters)

    def query_callbacks_by_path(self, prefix: str, limit: int = 200) -> list[dict[str, Any]]:
        return self._require_callbacks().query_callbacks_by_path(prefix, limit)

    def query_callbacks_by_field(self, field: str, limit: int = 200) -> list[dict[str, Any]]:
        return self._require_callbacks().query_callbacks_by_field(field, limit)

    def query_callbacks_by_module(self, module: str, limit: int = 200) -> list[dict[str, Any]]:
        return self._require_callbacks().query_callbacks_by_module(module, limit)

    def get_callback(self, registration_id: str) -> dict[str, Any] | None:
        return self._require_callbacks().get_callback(registration_id)

    def count_callbacks(self) -> int:
        return self._require_callbacks().count_callbacks()

    def callback_stats(self) -> dict[str, Any]:
        return self._require_callbacks().callback_stats()

    def link_callback_targets(self, pairs: Any) -> int:
        return self._require_callbacks().link_callback_targets(pairs)

    def query_callback_targets(
        self, registration_id: str, limit: int = 200
    ) -> list[dict[str, Any]]:
        return self._require_callbacks().query_callback_targets(registration_id, limit)

    def query_registrations_for_target(
        self, call_target_id: str, limit: int = 200
    ) -> list[dict[str, Any]]:
        return self._require_callbacks().query_registrations_for_target(
            call_target_id, limit
        )

    # ------------------------------------------------------------------ #
    # Deep-analysis status (layered indexing / on-demand deep pass)
    # ------------------------------------------------------------------ #
    def mark_deep_ready(self, items: Any) -> int:
        return self._require_deep().mark_deep_ready(items)

    def mark_deep_failed(self, items: Any, *, error: str = "") -> int:
        return self._require_deep().mark_deep_failed(items, error=error)

    def mark_deep_pending(self, items: Any, *, tu_id: str = "") -> int:
        return self._require_deep().mark_deep_pending(items, tu_id=tu_id)

    def get_deep_status(self, symbols: Any, limit: int = 200) -> dict[str, dict[str, Any]]:
        return self._require_deep().get_deep_status(symbols, limit)

    def list_deep_pending(
        self, limit: int | None = None, tu_ids: Any = None
    ) -> list[dict[str, Any]]:
        return self._require_deep().list_deep_pending(limit, tu_ids)

    def list_deep_ready(
        self, limit: int | None = None, tu_ids: Any = None
    ) -> list[dict[str, Any]]:
        return self._require_deep().list_deep_ready(limit, tu_ids)

    def count_deep_status(self) -> dict[str, int]:
        return self._require_deep().count_deep_status()

    def deep_stats(self) -> dict[str, Any]:
        return self._require_deep().deep_stats()

    def invalidate_deep(self, function_ids: Any = None, tu_ids: Any = None) -> int:
        return self._require_deep().invalidate_deep(function_ids, tu_ids)

    # ------------------------------------------------------------------ #
    # Unified-backend job queue + event log
    # ------------------------------------------------------------------ #
    @property
    def jobs(self) -> job_queue.JobQueue:
        """Job queue bound to this store's connection (tables in this DB)."""

        return self._require_jobs()

    def create_job(self, kind: str, payload: Any = None, *, job_id: str | None = None) -> str:
        return self._require_jobs().create_job(kind, payload, job_id=job_id)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        return self._require_jobs().get_job(job_id)

    def list_jobs(
        self,
        *,
        kind: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        return self._require_jobs().list_jobs(
            kind=kind, status=status, limit=limit, offset=offset
        )

    def claim_job(
        self,
        kind: str | None = None,
        *,
        claimed_by: str = "",
        lease_seconds: int = job_queue.DEFAULT_LEASE_SECONDS,
    ) -> dict[str, Any] | None:
        return self._require_jobs().claim_job(
            kind, claimed_by=claimed_by, lease_seconds=lease_seconds
        )

    def complete_job(
        self, job_id: str, result: Any = None, *, claimed_by: str | None = None
    ) -> bool:
        return self._require_jobs().complete_job(job_id, result, claimed_by=claimed_by)

    def fail_job(
        self, job_id: str, error: str = "", *, claimed_by: str | None = None
    ) -> bool:
        return self._require_jobs().fail_job(job_id, error, claimed_by=claimed_by)

    def requeue_job(
        self, job_id: str, *, error: str = "", claimed_by: str | None = None
    ) -> bool:
        return self._require_jobs().requeue_job(
            job_id, error=error, claimed_by=claimed_by
        )

    def renew_job_lease(
        self,
        job_id: str,
        *,
        claimed_by: str = "",
        lease_seconds: int = job_queue.DEFAULT_LEASE_SECONDS,
    ) -> bool:
        return self._require_jobs().renew_job_lease(
            job_id, claimed_by=claimed_by, lease_seconds=lease_seconds
        )

    def recover_running(self) -> int:
        return self._require_jobs().recover_running()

    def requeue_expired(self) -> int:
        return self._require_jobs().requeue_expired()

    def job_stats(self) -> dict[str, Any]:
        return self._require_jobs().job_stats()

    def count_jobs(self, *, kind: str | None = None, status: str | None = None) -> int:
        return self._require_jobs().count_jobs(kind=kind, status=status)

    def list_job_events(
        self,
        *,
        since_id: int = 0,
        job_id: str | None = None,
        limit: int = 100,
        types: Any = None,
    ) -> list[dict[str, Any]]:
        return self._require_jobs().list_job_events(
            since_id=since_id, job_id=job_id, limit=limit, types=types
        )

    def latest_job_event_id(self) -> int:
        return self._require_jobs().latest_job_event_id()

    def append_job_event(self, job_id: str, event_type: str, data: Any = None) -> int:
        return self._require_jobs().append_job_event(job_id, event_type, data)

    def prune_job_events(self, keep_last: int = 10000) -> int:
        return self._require_jobs().prune_job_events(keep_last)

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("SqliteStore is not open; call open() first")
        return self._connection

    def _require_queries(self) -> queries.QueryEngine:
        if self._queries is None:
            raise RuntimeError("SqliteStore is not open; call open() first")
        return self._queries

    def _require_graph(self) -> graph.CallGraph:
        if self._graph is None:
            raise RuntimeError("SqliteStore is not open; call open() first")
        return self._graph

    def _require_jobs(self) -> job_queue.JobQueue:
        if self._jobs is None:
            raise RuntimeError("SqliteStore is not open; call open() first")
        return self._jobs

    def _require_callbacks(self) -> callbacks.CallbackRegistry:
        if self._callbacks is None:
            raise RuntimeError("SqliteStore is not open; call open() first")
        return self._callbacks

    def _require_deep(self) -> deep.DeepStatusStore:
        if self._deep is None:
            raise RuntimeError("SqliteStore is not open; call open() first")
        return self._deep

    def _require_registry(self) -> workspace.WorkspaceRegistry:
        if self._registry is None:
            raise RuntimeError("SqliteStore is not open; call open() first")
        return self._registry

    def _ensure_transaction(self) -> None:
        if not self._in_transaction:
            self.begin()
