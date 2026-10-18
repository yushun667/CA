"""Bulk writer and one-shot finalize step for the SQLite store.

Design choices that keep ingest fast:

- rows are inserted with ``executemany`` inside the transaction the caller
  opened through ``SqliteStore.begin()/commit()``;
- duplicate ids are dropped by the primary key (``INSERT OR IGNORE``), except
  for functions where a later *definition* row replaces an earlier declaration
  row (``ON CONFLICT DO UPDATE ... WHERE excluded.is_definition > ...``), so
  repeated parsing of a header cannot hide the body's source location;
- no secondary indexes are maintained during load; :func:`finalize` builds
  them once, repairs unresolved callees, rebuilds the optional FTS5 trigram
  symbol index (:func:`build_fts`), then runs ``ANALYZE``.

Record fields map one-to-one onto columns; see ``schema.sql``.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import Any, Sequence

from ca.core.schema import AnalysisResult, ParseResult

# --------------------------------------------------------------------------- #
# Column definitions (schema.sql must list the same names, in any order)
# --------------------------------------------------------------------------- #

TUS_COLUMNS = ("id", "file", "directory", "repo_root", "arguments", "language", "source", "driver")

FUNCTION_COLUMNS = (
    "id",
    "tu_id",
    "name",
    "qualified_name",
    "usr",
    "kind",
    "file",
    "line",
    "col",
    "end_line",
    "end_col",
    "is_definition",
    "return_type",
    "signature",
    "display_name",
    "storage_class",
    "linkage",
    "is_static",
    "is_inline",
    "is_variadic",
    "parent_id",
    "params_json",
)

TYPE_COLUMNS = (
    "id",
    "tu_id",
    "name",
    "qualified_name",
    "usr",
    "kind",
    "file",
    "line",
    "col",
    "end_line",
    "end_col",
    "is_definition",
    "size_bytes",
    "align_bytes",
    "underlying_type",
    "parent_id",
    "is_anonymous",
)

FIELD_COLUMNS = (
    "id",
    "tu_id",
    "type_id",
    "name",
    "type_spelling",
    "file",
    "line",
    "col",
    "end_line",
    "end_col",
    "offset_bits",
    "bit_width",
    "is_static",
    "is_bitfield",
    '"index"',
    "access",
)

CALL_COLUMNS = (
    "id",
    "tu_id",
    "caller_id",
    "file",
    "line",
    "col",
    "end_line",
    "end_col",
    "callee_id",
    "callee_usr",
    "callee_name",
    "callee_qualified_name",
    "kind",
    "is_resolved",
    "is_virtual",
    "is_macro_expansion",
    "arg_count",
)

CFG_BLOCK_COLUMNS = (
    "id",
    "function_id",
    '"index"',
    "kind",
    "terminator",
    "file",
    "line",
    "col",
    "end_line",
    "end_col",
    "statement_count",
)

CFG_EDGE_COLUMNS = ("id", "function_id", "src", "dst", "kind", "label")

BRANCH_COLUMNS = (
    "id",
    "function_id",
    "block_id",
    "file",
    "line",
    "col",
    "end_line",
    "end_col",
    "kind",
    "condition",
    "var_names",
    "true_block",
    "false_block",
    "is_negated",
    "case_value",
)

ASSIGNMENT_COLUMNS = (
    "id",
    "function_id",
    "block_id",
    "file",
    "line",
    "col",
    "end_line",
    "end_col",
    "lhs",
    "rhs",
    "var_names",
    "is_decl_init",
    '"operator"',
    "type_spelling",
)

RETURN_COLUMNS = (
    "id",
    "function_id",
    "block_id",
    "file",
    "line",
    "col",
    "end_line",
    "end_col",
    "expr",
    "var_names",
    "is_implicit",
)

RESOURCE_OP_COLUMNS = (
    "id",
    "function_id",
    "block_id",
    "file",
    "line",
    "col",
    "end_line",
    "end_col",
    "op",
    "resource_kind",
    "api_name",
    "target",
    "target_vars",
    "call_id",
    "matcher",
)

FINDING_COLUMNS = (
    "id",
    "function_id",
    "kind",
    "resource_kind",
    "message",
    "severity",
    "acquire_id",
    "release_ids",
    "path_blocks",
    "evidence",
)

CALL_TARGET_COLUMNS = (
    "id",
    "call_id",
    "caller_id",
    "callee_id",
    "callee_name",
    "kind",
    "confidence",
    "reason",
    "file",
    "line",
    "col",
    "end_line",
    "end_col",
)


# --------------------------------------------------------------------------- #
# Value encoders
# --------------------------------------------------------------------------- #


def _json_list(values: Sequence[Any]) -> str:
    return json.dumps(list(values), ensure_ascii=False, separators=(",", ":"))


def _sloc(sloc: Any) -> tuple[str, int, int, int, int]:
    return (sloc.file, sloc.line, sloc.col, sloc.end_line, sloc.end_col)


def _params_json(record: Any) -> str:
    return json.dumps(
        [
            {
                "name": param.name,
                "type_spelling": param.type_spelling,
                "index": param.index,
                "file": param.sloc.file,
                "line": param.sloc.line,
                "col": param.sloc.col,
                "end_line": param.sloc.end_line,
                "end_col": param.sloc.end_col,
                "default_value": param.default_value,
            }
            for param in record.params
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


# --------------------------------------------------------------------------- #
# Row builders (order matches the *_COLUMNS tuples above)
# --------------------------------------------------------------------------- #


def _tu_row(tu: Any) -> tuple[Any, ...]:
    return (
        tu.id,
        tu.file,
        tu.directory,
        tu.repo_root,
        _json_list(tu.arguments),
        tu.language,
        tu.source,
        tu.driver,
    )


def _function_row(record: Any) -> tuple[Any, ...]:
    return (
        record.id,
        record.tu_id,
        record.name,
        record.qualified_name,
        record.usr,
        record.kind,
        *_sloc(record.sloc),
        int(record.is_definition),
        record.return_type,
        record.signature,
        record.display_name,
        record.storage_class,
        record.linkage,
        int(record.is_static),
        int(record.is_inline),
        int(record.is_variadic),
        record.parent_id,
        _params_json(record),
    )


def _type_row(record: Any) -> tuple[Any, ...]:
    return (
        record.id,
        record.tu_id,
        record.name,
        record.qualified_name,
        record.usr,
        record.kind,
        *_sloc(record.sloc),
        int(record.is_definition),
        record.size_bytes,
        record.align_bytes,
        record.underlying_type,
        record.parent_id,
        int(record.is_anonymous),
    )


def _field_row(record: Any) -> tuple[Any, ...]:
    return (
        record.id,
        record.tu_id,
        record.type_id,
        record.name,
        record.type_spelling,
        *_sloc(record.sloc),
        record.offset_bits,
        record.bit_width,
        int(record.is_static),
        int(record.is_bitfield),
        record.index,
        record.access,
    )


def _call_row(record: Any) -> tuple[Any, ...]:
    return (
        record.id,
        record.tu_id,
        record.caller_id,
        *_sloc(record.sloc),
        record.callee_id,
        record.callee_usr,
        record.callee_name,
        record.callee_qualified_name,
        record.kind,
        int(record.is_resolved),
        int(record.is_virtual),
        int(record.is_macro_expansion),
        record.arg_count,
    )


def _cfg_block_row(record: Any) -> tuple[Any, ...]:
    return (
        record.id,
        record.function_id,
        record.index,
        record.kind,
        record.terminator,
        *_sloc(record.sloc),
        record.statement_count,
    )


def _cfg_edge_row(record: Any) -> tuple[Any, ...]:
    return (record.id, record.function_id, record.src, record.dst, record.kind, record.label)


def _branch_row(record: Any) -> tuple[Any, ...]:
    return (
        record.id,
        record.function_id,
        record.block_id,
        *_sloc(record.sloc),
        record.kind,
        record.condition,
        _json_list(record.var_names),
        record.true_block,
        record.false_block,
        int(record.is_negated),
        record.case_value,
    )


def _assignment_row(record: Any) -> tuple[Any, ...]:
    return (
        record.id,
        record.function_id,
        record.block_id,
        *_sloc(record.sloc),
        record.lhs,
        record.rhs,
        _json_list(record.var_names),
        int(record.is_decl_init),
        record.operator,
        record.type_spelling,
    )


def _return_row(record: Any) -> tuple[Any, ...]:
    return (
        record.id,
        record.function_id,
        record.block_id,
        *_sloc(record.sloc),
        record.expr,
        _json_list(record.var_names),
        int(record.is_implicit),
    )


def _resource_op_row(record: Any) -> tuple[Any, ...]:
    return (
        record.id,
        record.function_id,
        record.block_id,
        *_sloc(record.sloc),
        record.op,
        record.resource_kind,
        record.api_name,
        record.target,
        _json_list(record.target_vars),
        record.call_id,
        record.matcher,
    )


def _call_target_row(record: Any) -> tuple[Any, ...]:
    return (
        record.id,
        record.call_id,
        record.caller_id,
        record.callee_id,
        record.callee_name,
        record.kind,
        float(record.confidence),
        record.reason,
        *_sloc(record.sloc),
    )


def _finding_row(record: Any) -> tuple[Any, ...]:
    return (
        record.id,
        record.function_id,
        record.kind,
        record.resource_kind,
        record.message,
        record.severity,
        record.acquire_id,
        _json_list(record.release_ids),
        _json_list(record.path_blocks),
        _json_list(record.evidence),
    )


# --------------------------------------------------------------------------- #
# Prepared SQL
# --------------------------------------------------------------------------- #


def _insert_sql(table: str, columns: Sequence[str]) -> str:
    names = ", ".join(columns)
    marks = ", ".join("?" for _ in columns)
    return f"INSERT OR IGNORE INTO {table} ({names}) VALUES ({marks})"


TUS_INSERT = _insert_sql("tus", TUS_COLUMNS)
FUNCTION_INSERT = (
    _insert_sql("functions", FUNCTION_COLUMNS)
    + " ON CONFLICT(id) DO UPDATE SET "
    + ", ".join(f"{column} = excluded.{column}" for column in FUNCTION_COLUMNS[1:])
    + " WHERE excluded.is_definition > functions.is_definition"
)
TYPE_INSERT = _insert_sql("types", TYPE_COLUMNS)
FIELD_INSERT = _insert_sql("fields", FIELD_COLUMNS)
CALL_INSERT = _insert_sql("calls", CALL_COLUMNS)
CFG_BLOCK_INSERT = _insert_sql("cfg_blocks", CFG_BLOCK_COLUMNS)
CFG_EDGE_INSERT = _insert_sql("cfg_edges", CFG_EDGE_COLUMNS)
BRANCH_INSERT = _insert_sql("branches", BRANCH_COLUMNS)
ASSIGNMENT_INSERT = _insert_sql("assignments", ASSIGNMENT_COLUMNS)
RETURN_INSERT = _insert_sql("returns", RETURN_COLUMNS)
RESOURCE_OP_INSERT = _insert_sql("resource_ops", RESOURCE_OP_COLUMNS)
FINDING_INSERT = _insert_sql("findings", FINDING_COLUMNS)
CALL_TARGET_INSERT = _insert_sql("call_targets", CALL_TARGET_COLUMNS)


# --------------------------------------------------------------------------- #
# finalize(): placeholder callees, secondary indexes, statistics
# --------------------------------------------------------------------------- #

# Calls whose ``callee_id`` has no function row (typically external libc/kernel
# declarations or unresolved indirect targets) get a synthetic declaration row
# so callers/callees joins stay complete. The placeholder is never a definition
# and never replaces a real row (INSERT OR IGNORE on the primary key).
PLACEHOLDER_CALLEES_SQL = """
INSERT OR IGNORE INTO functions
    (id, name, qualified_name, usr, kind, is_definition, linkage)
SELECT
    callee_id,
    COALESCE(NULLIF(MAX(callee_name), ''), MAX(callee_qualified_name)),
    MAX(callee_qualified_name),
    MAX(callee_usr),
    'unknown',
    0,
    'unknown'
FROM calls
WHERE callee_id != ''
GROUP BY callee_id
"""

# FTS5 symbol search. The trigram tokenizer indexes every 3-character sequence,
# so it serves arbitrary substring queries (the LIKE '%symbol%' use case) through
# an index; unicode61 would only support token/prefix matches ("func_123*"), not
# substrings that cross a token boundary (measured in .tmp/agent-store/bench.py).
# The table is an external-content table over ``functions``: finalize() rebuilds
# it from the content table, which keeps ingest fast (no triggers) and makes the
# rebuild idempotent for pre-existing databases.
FTS_TABLE = "functions_fts"
FTS_READY_META_KEY = "fts_ready"
FTS_DDL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS functions_fts USING fts5("
    "name, qualified_name, content='functions', content_rowid='rowid', tokenize='trigram')"
)
FTS_REBUILD_SQL = "INSERT INTO functions_fts(functions_fts) VALUES('rebuild')"
FTS_OPTIMIZE_SQL = "INSERT INTO functions_fts(functions_fts) VALUES('optimize')"
# Patterns shorter than this fall back to LIKE: the trigram tokenizer cannot
# emit a trigram for 1-2 character queries (e.g. 2-character CJK words).
FTS_MIN_CHARS = 3

# Additive store-side schema changes are tracked separately from the core
# contract: meta["schema_version"] keeps ca.core.schema.SCHEMA_VERSION, while
# this key versions the physical layout (2 = phase-3 call_targets/workspace
# tables). Old databases are upgraded in place by open() (CREATE TABLE IF NOT
# EXISTS) plus finalize() (secondary indexes); no destructive migration runs.
STORE_SCHEMA_VERSION = "4"
STORE_SCHEMA_META_KEY = "store_schema_version"

# Legacy backfill: phase-3 databases predate deep_status. Any function that
# already carries deep facts (CFG blocks / call targets only come from the deep
# analyzer) is marked ready so query semantics do not change after upgrading.
# INSERT OR IGNORE: explicit pending/failed rows written by the new pipeline win.
DEEP_BACKFILL_SQL = """
INSERT OR IGNORE INTO deep_status
    (function_id, tu_id, status, analyzed_at, tool_version, source_hash, error, facts_json)
SELECT evidence.function_id, COALESCE(f.tu_id, ''), 'ready', '', 'legacy', '', '', '{}'
FROM (
    SELECT function_id FROM cfg_blocks
    UNION
    SELECT caller_id FROM call_targets
) AS evidence
LEFT JOIN functions AS f ON f.id = evidence.function_id
"""

# (name, DDL): created once after bulk load. Index count is a write-amplification
# vs. query-latency tradeoff measured in .tmp/agent-store/bench.py.
SECONDARY_INDEXES: tuple[tuple[str, str], ...] = (
    # Function lookups / symbol resolution: exact matches use these indexes.
    ("idx_functions_name", "CREATE INDEX IF NOT EXISTS idx_functions_name ON functions(name)"),
    # Incremental/deep updates are scoped by translation unit.
    ("idx_functions_tu_id", "CREATE INDEX IF NOT EXISTS idx_functions_tu_id ON functions(tu_id)"),
    (
        "idx_functions_qualified_name",
        "CREATE INDEX IF NOT EXISTS idx_functions_qualified_name ON functions(qualified_name)",
    ),
    ("idx_types_name", "CREATE INDEX IF NOT EXISTS idx_types_name ON types(name)"),
    (
        "idx_types_qualified_name",
        "CREATE INDEX IF NOT EXISTS idx_types_qualified_name ON types(qualified_name)",
    ),
    ("idx_fields_type_id", "CREATE INDEX IF NOT EXISTS idx_fields_type_id ON fields(type_id)"),
    ("idx_tus_file", "CREATE INDEX IF NOT EXISTS idx_tus_file ON tus(file)"),
    # Call graph: callers use callee_id, callees use caller_id. callee_name is
    # the fallback for calls the parser could not resolve to an id.
    ("idx_calls_caller_id", "CREATE INDEX IF NOT EXISTS idx_calls_caller_id ON calls(caller_id)"),
    ("idx_calls_callee_id", "CREATE INDEX IF NOT EXISTS idx_calls_callee_id ON calls(callee_id)"),
    ("idx_calls_callee_name", "CREATE INDEX IF NOT EXISTS idx_calls_callee_name ON calls(callee_name)"),
    # Per-function fact lookups (branches/assignments/... are keyed by owner).
    ("idx_cfg_blocks_function_id", "CREATE INDEX IF NOT EXISTS idx_cfg_blocks_function_id ON cfg_blocks(function_id)"),
    ("idx_cfg_edges_function_id", "CREATE INDEX IF NOT EXISTS idx_cfg_edges_function_id ON cfg_edges(function_id)"),
    ("idx_branches_function_id", "CREATE INDEX IF NOT EXISTS idx_branches_function_id ON branches(function_id)"),
    (
        "idx_assignments_function_id",
        "CREATE INDEX IF NOT EXISTS idx_assignments_function_id ON assignments(function_id)",
    ),
    ("idx_returns_function_id", "CREATE INDEX IF NOT EXISTS idx_returns_function_id ON returns(function_id)"),
    (
        "idx_resource_ops_function_id",
        "CREATE INDEX IF NOT EXISTS idx_resource_ops_function_id ON resource_ops(function_id)",
    ),
    ("idx_findings_kind", "CREATE INDEX IF NOT EXISTS idx_findings_kind ON findings(kind)"),
    ("idx_findings_function_id", "CREATE INDEX IF NOT EXISTS idx_findings_function_id ON findings(function_id)"),
    # Phase-3 candidate call edges (merged with direct calls by the graph API).
    ("idx_call_targets_call_id", "CREATE INDEX IF NOT EXISTS idx_call_targets_call_id ON call_targets(call_id)"),
    ("idx_call_targets_caller_id", "CREATE INDEX IF NOT EXISTS idx_call_targets_caller_id ON call_targets(caller_id)"),
    ("idx_call_targets_callee_id", "CREATE INDEX IF NOT EXISTS idx_call_targets_callee_id ON call_targets(callee_id)"),
    # Cross-TU callback registration facts: equality on path/module/TU/target,
    # prefix range scans on object_path, and reverse links to call_targets.
    # Leftmost column also serves object_path prefix range queries while the
    # full key makes the stable ORDER BY an index walk (no temp b-tree).
    (
        "idx_callback_order",
        "CREATE INDEX IF NOT EXISTS idx_callback_order ON callback_registrations("
        "object_path, file, line, target_function_name, id)",
    ),
    (
        "idx_callback_natural",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_callback_natural "
        "ON callback_registrations(tu_id, object_path, target_function_id, "
        "target_function_name, file, line, registration_kind)",
    ),
    (
        "idx_callback_field",
        "CREATE INDEX IF NOT EXISTS idx_callback_field ON callback_registrations(object_field)",
    ),
    (
        "idx_callback_module",
        "CREATE INDEX IF NOT EXISTS idx_callback_module ON callback_registrations(module)",
    ),
    (
        "idx_callback_tu_id",
        "CREATE INDEX IF NOT EXISTS idx_callback_tu_id ON callback_registrations(tu_id)",
    ),
    (
        "idx_callback_target_id",
        "CREATE INDEX IF NOT EXISTS idx_callback_target_id "
        "ON callback_registrations(target_function_id)",
    ),
    (
        "idx_callback_kind",
        "CREATE INDEX IF NOT EXISTS idx_callback_kind "
        "ON callback_registrations(registration_kind)",
    ),
    (
        "idx_callback_link_target",
        "CREATE INDEX IF NOT EXISTS idx_callback_link_target "
        "ON callback_target_links(call_target_id)",
    ),
    # Deep-analysis status: pending queue scans by status, per-TU invalidation.
    ("idx_deep_status_status", "CREATE INDEX IF NOT EXISTS idx_deep_status_status ON deep_status(status, function_id)"),
    ("idx_deep_status_tu_id", "CREATE INDEX IF NOT EXISTS idx_deep_status_tu_id ON deep_status(tu_id)"),
    # Multi-repo metadata lookups.
    ("idx_workspaces_path", "CREATE INDEX IF NOT EXISTS idx_workspaces_path ON workspaces(path)"),
    ("idx_projects_workspace_id", "CREATE INDEX IF NOT EXISTS idx_projects_workspace_id ON projects(workspace_id)"),
    ("idx_variants_workspace_id", "CREATE INDEX IF NOT EXISTS idx_variants_workspace_id ON variants(workspace_id)"),
)


def add_parse_result(conn: sqlite3.Connection, result: ParseResult) -> None:
    """Insert one parse result's rows (``executemany``, idempotent)."""

    conn.execute(TUS_INSERT, _tu_row(result.tu))
    conn.executemany(FUNCTION_INSERT, [_function_row(record) for record in result.functions])
    conn.executemany(TYPE_INSERT, [_type_row(record) for record in result.types])
    conn.executemany(FIELD_INSERT, [_field_row(record) for record in result.fields])
    conn.executemany(CALL_INSERT, [_call_row(record) for record in result.calls])
    # Diagnostics are not persisted: they are build/parse diagnostics, not
    # database records in the task's table contract.


def add_analysis_result(conn: sqlite3.Connection, result: AnalysisResult) -> None:
    """Insert one function analysis' rows (``executemany``, idempotent)."""

    conn.executemany(CFG_BLOCK_INSERT, [_cfg_block_row(record) for record in result.cfg_blocks])
    conn.executemany(CFG_EDGE_INSERT, [_cfg_edge_row(record) for record in result.cfg_edges])
    conn.executemany(BRANCH_INSERT, [_branch_row(record) for record in result.branches])
    conn.executemany(ASSIGNMENT_INSERT, [_assignment_row(record) for record in result.assignments])
    conn.executemany(RETURN_INSERT, [_return_row(record) for record in result.returns])
    conn.executemany(RESOURCE_OP_INSERT, [_resource_op_row(record) for record in result.resource_ops])
    conn.executemany(FINDING_INSERT, [_finding_row(record) for record in result.findings])
    conn.executemany(
        CALL_TARGET_INSERT, [_call_target_row(record) for record in result.call_targets]
    )


def add_call_targets(conn: sqlite3.Connection, records: Sequence[Any]) -> int:
    """Insert candidate ``CallTargetRecord`` rows outside an analysis batch.

    Used by post-processing passes (e.g. cross-TU callback resolution on an
    existing database). Accepts :class:`ca.core.schema.CallTargetRecord`
    objects or mappings with the same field names (``sloc`` or flattened
    ``file/line/col/end_line/end_col``; ``id`` required). Rows are
    ``INSERT OR IGNORE``, so re-running a pass is idempotent. Returns the number
    of rows actually inserted.
    """

    before = conn.total_changes
    rows: list[tuple[Any, ...]] = []
    for record in records:
        if isinstance(record, Mapping):
            sloc = record.get("sloc")
            if isinstance(sloc, Mapping):
                sloc_values = (
                    sloc.get("file", ""),
                    sloc.get("line", 0),
                    sloc.get("col", 0),
                    sloc.get("end_line", 0),
                    sloc.get("end_col", 0),
                )
            else:
                sloc_values = (
                    record.get("file", ""),
                    record.get("line", 0),
                    record.get("col", 0),
                    record.get("end_line", 0),
                    record.get("end_col", 0),
                )
            identifier = str(record.get("id", ""))
            if not identifier:
                raise ValueError("call target mapping is missing id")
            rows.append(
                (
                    identifier,
                    str(record.get("call_id", "")),
                    str(record.get("caller_id", "")),
                    str(record.get("callee_id", "")),
                    str(record.get("callee_name", "")),
                    str(record.get("kind", "indirect_candidate")),
                    float(record.get("confidence", 0.0)),
                    str(record.get("reason", "")),
                    *sloc_values,
                )
            )
        else:
            rows.append(_call_target_row(record))
    if rows:
        conn.executemany(CALL_TARGET_INSERT, rows)
    return conn.total_changes - before


def build_fts(conn: sqlite3.Connection) -> bool:
    """Create/populate the FTS5 trigram index; False when FTS5 is unavailable.

    Idempotent: creates the virtual table when missing (old databases) and
    rebuilds it from the external content table, so incremental loads just call
    finalize() again. A failure to build the index must never fail indexing:
    queries then fall back to the LIKE scan (``meta.fts_ready`` stays ``0``).
    """

    try:
        conn.execute("BEGIN")
        try:
            conn.execute(FTS_DDL)
            conn.execute(FTS_REBUILD_SQL)
            conn.execute(FTS_OPTIMIZE_SQL)
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, '1')",
                (FTS_READY_META_KEY,),
            )
            conn.execute("COMMIT")
            return True
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    except sqlite3.Error:
        # FTS5 not compiled in, or an incompatible pre-existing virtual table.
        try:
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, '0')",
                (FTS_READY_META_KEY,),
            )
        except sqlite3.Error:
            pass
        return False


def finalize(conn: sqlite3.Connection) -> None:
    """Repair unresolved callees, build indexes + FTS5, gather statistics.

    Called exactly once after all writers committed. DDL runs inside one
    transaction so a crash leaves either the pre- or post-finalize state.
    FTS5 construction is best effort: when it fails, queries fall back to LIKE.
    """

    conn.execute("BEGIN")
    try:
        conn.execute(PLACEHOLDER_CALLEES_SQL)
        for _name, ddl in SECONDARY_INDEXES:
            conn.execute(ddl)
        conn.execute(DEEP_BACKFILL_SQL)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    build_fts(conn)
    conn.execute("ANALYZE")
    conn.execute("PRAGMA optimize")
    # Fold the WAL back into the main file so the shipped DB is a single file
    # and stats().db_bytes reflects the settled size. Best effort: a reader may
    # block the checkpoint, which must not fail indexing.
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error:
        pass
