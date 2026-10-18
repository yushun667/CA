"""Read side of the SQLite store: the query API and streaming export.

Returned dict keys are the CLI/benchmark contract. They are the dataclass field
names from ``ca.core.schema`` with source ranges flattened to
``file, line, col, end_line, end_col`` and tuple fields decoded to lists. The
full key list (including the convenience keys ``caller_name``/``callee_name``/
``function_name``) is documented in :mod:`ca.store.sqlite_store`.

Implementation notes:

- every value is bound as a parameter; ``IN (...)`` lists are expanded to
  placeholder marks only, never interpolated values;
- symbol resolution is exact (``id``/``name``/``qualified_name``) first and
  only then falls back to a substring search; the substring search uses the
  FTS5 trigram index built by ``finalize()`` when available (``meta.fts_ready``)
  and the previous ``LIKE '%symbol%'`` table scan otherwise (wildcards are
  escaped; 1-2 character queries cannot use the trigram tokenizer);
- ordering is deterministic (``is_definition DESC, file, line, id``) so limits
  are stable across runs.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterator, Sequence

from ca.core.schema import SCHEMA_VERSION
from ca.store import virtual_dispatch
from ca.store.writer import (
    ASSIGNMENT_COLUMNS,
    CALL_TARGET_COLUMNS,
    FTS_MIN_CHARS,
    FTS_READY_META_KEY,
    BRANCH_COLUMNS,
    CALL_COLUMNS,
    CFG_BLOCK_COLUMNS,
    CFG_EDGE_COLUMNS,
    FIELD_COLUMNS,
    FINDING_COLUMNS,
    FUNCTION_COLUMNS,
    RESOURCE_OP_COLUMNS,
    RETURN_COLUMNS,
    TUS_COLUMNS,
    TYPE_COLUMNS,
)

# Resolution cap: enough for common names, bounded so a query for ``init``
# cannot pull tens of thousands of ids into an ``IN`` list.
SYMBOL_ID_LIMIT = 500

# Tables counted by ``stats()``.
TABLES: tuple[str, ...] = (
    "meta",
    "tus",
    "functions",
    "types",
    "fields",
    "calls",
    "cfg_blocks",
    "cfg_edges",
    "branches",
    "assignments",
    "returns",
    "resource_ops",
    "findings",
    "call_targets",
    "tu_includes",
    "virtual_overrides",
    "type_bases",
    "callback_registrations",
    "callback_target_links",
    "deep_status",
    "workspaces",
    "projects",
    "variants",
    "jobs",
    "job_events",
)

# kind -> (table, columns, json column -> output key, boolean columns)
RecordSpec = tuple[str, str, tuple[str, ...], dict[str, str], tuple[str, ...]]

RECORD_SPECS: tuple[RecordSpec, ...] = (
    ("compilation_unit", "tus", TUS_COLUMNS, {"arguments": "arguments"}, ()),
    (
        "function",
        "functions",
        FUNCTION_COLUMNS,
        {"params_json": "params"},
        ("is_definition", "is_static", "is_inline", "is_variadic"),
    ),
    (
        "type",
        "types",
        TYPE_COLUMNS,
        {},
        ("is_definition", "is_anonymous"),
    ),
    (
        "field",
        "fields",
        FIELD_COLUMNS,
        {},
        ("is_static", "is_bitfield"),
    ),
    (
        "call",
        "calls",
        CALL_COLUMNS,
        {},
        ("is_resolved", "is_virtual", "is_macro_expansion"),
    ),
    ("cfg_block", "cfg_blocks", CFG_BLOCK_COLUMNS, {}, ()),
    ("cfg_edge", "cfg_edges", CFG_EDGE_COLUMNS, {}, ()),
    ("branch", "branches", BRANCH_COLUMNS, {"var_names": "var_names"}, ("is_negated",)),
    (
        "assignment",
        "assignments",
        ASSIGNMENT_COLUMNS,
        {"var_names": "var_names"},
        ("is_decl_init",),
    ),
    ("return", "returns", RETURN_COLUMNS, {"var_names": "var_names"}, ("is_implicit",)),
    (
        "resource_op",
        "resource_ops",
        RESOURCE_OP_COLUMNS,
        {"target_vars": "target_vars"},
        (),
    ),
    (
        "finding",
        "findings",
        FINDING_COLUMNS,
        {"release_ids": "release_ids", "path_blocks": "path_blocks", "evidence": "evidence"},
        (),
    ),
    ("call_target", "call_targets", CALL_TARGET_COLUMNS, {}, ()),
)

_SPECS_BY_KIND: dict[str, RecordSpec] = {spec[0]: spec for spec in RECORD_SPECS}

# Fact tables queried per function, in query_function_detail() order.
FACT_KINDS: tuple[str, ...] = ("branch", "assignment", "return", "resource_op", "finding")

FUNCTION_SUMMARY_COLUMNS = (
    "id",
    "name",
    "qualified_name",
    "kind",
    "file",
    "line",
    "is_definition",
    "is_static",
    "linkage",
)

# A function detail / iter_records row uses the full record shape.
FUNCTION_EXPORT_SQL = "SELECT {} FROM functions WHERE id = ?".format(", ".join(FUNCTION_COLUMNS))


def _like_pattern(value: str) -> str:
    """``%value%`` with SQL LIKE wildcards in ``value`` escaped."""

    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _decode_json(value: Any) -> list[Any]:
    if not value:
        return []
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return []
    return decoded if isinstance(decoded, list) else []


def _convert(spec: RecordSpec, data: dict[str, Any]) -> dict[str, Any]:
    """Decode JSON/bool columns; query results keep the record's own fields."""

    _kind, _table, _columns, json_columns, bool_columns = spec
    for column, output_key in json_columns.items():
        data[output_key] = _decode_json(data.pop(column, None))
    for column in bool_columns:
        data[column] = bool(data.get(column, 0))
    return data


def _export_convert(spec: RecordSpec, data: dict[str, Any]) -> dict[str, Any]:
    """Export dict: decoded fields plus ``kind``/``schema`` envelope keys.

    Like ``ca.core.schema.record_to_dict``, ``kind`` is the record type; a
    record whose own schema field is also named ``kind`` (call/branch/...)
    therefore exposes the record type there in export output.
    """

    data = _convert(spec, data)
    data["kind"] = spec[0]
    data["schema"] = SCHEMA_VERSION
    return data


def _placeholders(count: int) -> str:
    return ", ".join("?" for _ in range(count))


def _edge_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["confidence"] = float(data["confidence"])
    data["is_resolved"] = bool(data["is_resolved"])
    data["candidate"] = data["source"] == "call_target"
    data.pop("source_rank", None)
    return data


class QueryEngine:
    """All read methods of :class:`ca.store.sqlite_store.SqliteStore`."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection
        self._fts_available: bool | None = None
        self._table_cache: dict[str, bool] = {}

    # ------------------------------------------------------------------ #
    # Symbol resolution
    # ------------------------------------------------------------------ #
    def reset_caches(self) -> None:
        """Forget derived state; called by SqliteStore.finalize()."""

        self._fts_available = None
        self._table_cache.clear()

    def _has_table(self, name: str) -> bool:
        """Schema capability check (old databases lack newer tables)."""

        cached = self._table_cache.get(name)
        if cached is None:
            try:
                cached = (
                    self._conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') "
                        "AND name = ? LIMIT 1",
                        (name,),
                    ).fetchone()
                    is not None
                )
            except sqlite3.Error:
                cached = False
            self._table_cache[name] = cached
        return cached

    def _has_fts(self) -> bool:
        """True when finalize() successfully built the FTS5 trigram index."""

        if self._fts_available is None:
            try:
                row = self._conn.execute(
                    "SELECT value FROM meta WHERE key = ?", (FTS_READY_META_KEY,)
                ).fetchone()
            except sqlite3.Error:
                row = None
            self._fts_available = bool(row and row[0] == "1")
        return self._fts_available

    @staticmethod
    def _fts_phrase(pattern: str) -> str:
        """Literal FTS5 phrase query; embedded double quotes are doubled."""

        return '"' + pattern.replace('"', '""') + '"'

    def _fts_ready_for(self, pattern: str) -> bool:
        return len(pattern) >= FTS_MIN_CHARS and self._has_fts()

    def _fts_substring_ids(self, pattern: str, limit: int) -> list[str] | None:
        """Substring ids via FTS5; ``None`` means "fall back to LIKE"."""

        sql = (
            "SELECT f.id FROM functions_fts "
            "JOIN functions AS f ON functions_fts.rowid = f.rowid "
            "WHERE functions_fts MATCH ? "
            "ORDER BY f.is_definition DESC, f.file, f.line, f.id LIMIT ?"
        )
        try:
            rows = self._conn.execute(sql, (self._fts_phrase(pattern), limit)).fetchall()
        except sqlite3.Error:
            self._fts_available = False
            return None
        return [row[0] for row in rows]

    def _like_substring_ids(self, pattern: str, limit: int) -> list[str]:
        like = _like_pattern(pattern)
        rows = self._conn.execute(
            "SELECT id FROM functions "
            "WHERE name LIKE ? ESCAPE '\\' OR qualified_name LIKE ? ESCAPE '\\' "
            "ORDER BY is_definition DESC, file, line, id LIMIT ?",
            (like, like, limit),
        ).fetchall()
        return [row[0] for row in rows]

    def _matching_function_ids(self, symbol: str) -> list[str]:
        """Resolve a symbol to function ids: exact first, then substring."""

        if not symbol:
            return []
        rows = self._conn.execute(
            "SELECT id FROM functions "
            "WHERE id = ? OR name = ? OR qualified_name = ? "
            "ORDER BY is_definition DESC, file, line, id LIMIT ?",
            (symbol, symbol, symbol, SYMBOL_ID_LIMIT),
        ).fetchall()
        if rows:
            return [row[0] for row in rows]
        if self._fts_ready_for(symbol):
            ids = self._fts_substring_ids(symbol, SYMBOL_ID_LIMIT)
            if ids is not None:
                # Trigram covers every substring of length >= 3, so an empty
                # result is a real miss; no need to rescan with LIKE.
                return ids
        return self._like_substring_ids(symbol, SYMBOL_ID_LIMIT)

    def _like_substring_summary_rows(
        self, pattern: str, columns: Sequence[str], limit: int
    ) -> list[Any]:
        like = _like_pattern(pattern)
        sql = (
            f"SELECT {', '.join(columns)} FROM functions "
            "WHERE name LIKE ? ESCAPE '\\' OR qualified_name LIKE ? ESCAPE '\\' "
            "ORDER BY is_definition DESC, file, line, id LIMIT ?"
        )
        return self._conn.execute(sql, (like, like, limit)).fetchall()

    def _substring_summary_rows(
        self, pattern: str, columns: Sequence[str], limit: int
    ) -> list[Any]:
        """Substring query page: FTS5 trigram when available, else LIKE scan."""

        if self._fts_ready_for(pattern):
            sql = (
                f"SELECT {', '.join('f.' + column for column in columns)} FROM functions_fts "
                "JOIN functions AS f ON functions_fts.rowid = f.rowid "
                "WHERE functions_fts MATCH ? "
                "ORDER BY f.is_definition DESC, f.file, f.line, f.id LIMIT ?"
            )
            try:
                return self._conn.execute(sql, (self._fts_phrase(pattern), limit)).fetchall()
            except sqlite3.Error:
                self._fts_available = False
        return self._like_substring_summary_rows(pattern, columns, limit)

    def _function_row(self, function_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(FUNCTION_EXPORT_SQL, (function_id,)).fetchone()
        if row is None:
            return None
        return _convert(_SPECS_BY_KIND["function"], dict(row))

    def _fact_rows(self, kind: str, function_ids: Sequence[str]) -> list[dict[str, Any]]:
        if not function_ids:
            return []
        spec = _SPECS_BY_KIND[kind]
        _kind, table, columns, _json, _bool = spec
        sql = (
            f"SELECT {', '.join(columns)} FROM {table} "
            f"WHERE function_id IN ({_placeholders(len(function_ids))}) "
            "ORDER BY file, line, col, id"
        )
        return [_convert(spec, dict(row)) for row in self._conn.execute(sql, list(function_ids))]


    # ------------------------------------------------------------------ #
    # Merged call edges: direct CallRecord edges + candidate CallTargets.
    # Direct edges win when the same (caller, callee, call site) appears twice.
    # ------------------------------------------------------------------ #
    _EDGE_SELECT = (
        "SELECT {call_id} AS call_id, {caller_id} AS caller_id, "
        "cf.name AS caller_name, cf.file AS caller_file, cf.line AS caller_line, "
        "{callee_id} AS callee_id, "
        "COALESCE(NULLIF(tf.name, ''), {callee_name}) AS callee_name, "
        "COALESCE(tf.file, '') AS callee_file, COALESCE(tf.line, 0) AS callee_line, "
        "{call_file} AS call_file, {call_line} AS call_line, "
        "{kind} AS kind, {confidence} AS confidence, {is_resolved} AS is_resolved, "
        "{source} AS source, {source_rank} AS source_rank "
        "FROM {table} AS {alias} "
        "{caller_join} {callee_join} {call_join} "
        "WHERE {where} IN ({marks})"
    )

    def _load_merged_edges(
        self, ids: Sequence[str], direction: str, limit: int
    ) -> list[dict[str, Any]]:
        """Load forward (caller in ids) or reverse (callee in ids) edges."""

        if not ids or limit <= 0:
            return []
        marks = _placeholders(len(ids))
        if direction == "forward":
            direct = (
                f"SELECT c.id AS call_id, c.caller_id AS caller_id, cf.name AS caller_name, "
                "cf.file AS caller_file, cf.line AS caller_line, c.callee_id AS callee_id, "
                "COALESCE(NULLIF(tf.name, ''), c.callee_name) AS callee_name, "
                "COALESCE(tf.file, '') AS callee_file, COALESCE(tf.line, 0) AS callee_line, "
                "c.file AS call_file, c.line AS call_line, c.kind AS kind, 1.0 AS confidence, "
                "c.is_resolved AS is_resolved, 'call' AS source, 0 AS source_rank "
                "FROM calls AS c "
                "LEFT JOIN functions AS cf ON cf.id = c.caller_id "
                "LEFT JOIN functions AS tf ON tf.id = c.callee_id "
                f"WHERE c.caller_id IN ({marks})"
            )
            candidate = (
                "SELECT ct.call_id AS call_id, ct.caller_id AS caller_id, "
                "cf.name AS caller_name, cf.file AS caller_file, cf.line AS caller_line, "
                "ct.callee_id AS callee_id, "
                "COALESCE(NULLIF(tf.name, ''), ct.callee_name) AS callee_name, "
                "COALESCE(tf.file, '') AS callee_file, COALESCE(tf.line, 0) AS callee_line, "
                "COALESCE(c.file, ct.file) AS call_file, COALESCE(c.line, ct.line) AS call_line, "
                "ct.kind AS kind, ct.confidence AS confidence, 0 AS is_resolved, "
                "'call_target' AS source, 1 AS source_rank "
                "FROM call_targets AS ct "
                "LEFT JOIN calls AS c ON c.id = ct.call_id "
                "LEFT JOIN functions AS cf ON cf.id = ct.caller_id "
                "LEFT JOIN functions AS tf ON tf.id = ct.callee_id "
                f"WHERE ct.caller_id IN ({marks})"
            )
            params: list[Any] = [*ids, *ids, limit]
        else:
            direct = (
                f"SELECT c.id AS call_id, c.caller_id AS caller_id, cf.name AS caller_name, "
                "cf.file AS caller_file, cf.line AS caller_line, c.callee_id AS callee_id, "
                "COALESCE(NULLIF(tf.name, ''), c.callee_name) AS callee_name, "
                "COALESCE(tf.file, '') AS callee_file, COALESCE(tf.line, 0) AS callee_line, "
                "c.file AS call_file, c.line AS call_line, c.kind AS kind, 1.0 AS confidence, "
                "c.is_resolved AS is_resolved, 'call' AS source, 0 AS source_rank "
                "FROM calls AS c "
                "LEFT JOIN functions AS cf ON cf.id = c.caller_id "
                "LEFT JOIN functions AS tf ON tf.id = c.callee_id "
                f"WHERE c.callee_id IN ({marks})"
            )
            candidate = (
                "SELECT ct.call_id AS call_id, ct.caller_id AS caller_id, "
                "cf.name AS caller_name, cf.file AS caller_file, cf.line AS caller_line, "
                "ct.callee_id AS callee_id, "
                "COALESCE(NULLIF(tf.name, ''), ct.callee_name) AS callee_name, "
                "COALESCE(tf.file, '') AS callee_file, COALESCE(tf.line, 0) AS callee_line, "
                "COALESCE(c.file, ct.file) AS call_file, COALESCE(c.line, ct.line) AS call_line, "
                "ct.kind AS kind, ct.confidence AS confidence, 0 AS is_resolved, "
                "'call_target' AS source, 1 AS source_rank "
                "FROM call_targets AS ct "
                "LEFT JOIN calls AS c ON c.id = ct.call_id "
                "LEFT JOIN functions AS cf ON cf.id = ct.caller_id "
                "LEFT JOIN functions AS tf ON tf.id = ct.callee_id "
                f"WHERE ct.callee_id IN ({marks})"
            )
            params = [*ids, *ids, limit]
        if self._has_table("call_targets"):
            sql = (
                f"{direct} UNION ALL {candidate} "
                "ORDER BY call_file, call_line, caller_id, callee_id, source_rank LIMIT ?"
            )
        else:
            # Phase-3/older database: direct call edges only (no candidate rows).
            sql = (
                f"{direct} "
                "ORDER BY call_file, call_line, caller_id, callee_id, source_rank LIMIT ?"
            )
            params = [*ids, limit]
        try:
            rows = self._conn.execute(sql, params).fetchall()
        except sqlite3.Error:
            rows = []
        merged = [_edge_row(row) for row in rows]
        # Query-time virtual candidates (task-37): computed from persisted
        # override metadata so cross-TU overrides from any indexed shard
        # participate. Degrades to direct/indirect edges on old databases.
        if self._has_table("virtual_overrides"):
            if direction == "forward":
                merged.extend(virtual_dispatch.forward_edges(self._conn, ids, limit))
            else:
                merged.extend(virtual_dispatch.reverse_edges(self._conn, ids, limit))
        source_rank = {"call": 0, "call_target": 1, "virtual_candidate": 2}
        merged.sort(
            key=lambda row: (
                row["call_file"],
                row["call_line"],
                row["caller_id"],
                row["callee_id"],
                source_rank.get(row["source"], 3),
            )
        )
        return merged[:limit]

    def load_forward_edges(self, caller_ids: Sequence[str], limit: int = 500) -> list[dict[str, Any]]:
        """Direct + candidate edges whose caller is in ``caller_ids``."""

        return self._load_merged_edges(caller_ids, "forward", limit)

    def load_reverse_edges(self, callee_ids: Sequence[str], limit: int = 500) -> list[dict[str, Any]]:
        """Direct + candidate edges whose callee is in ``callee_ids``."""

        return self._load_merged_edges(callee_ids, "reverse", limit)

    def function_summaries(self, function_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        """``id -> {id, name, file, line}`` for graph node rendering."""

        result: dict[str, dict[str, Any]] = {}
        ids = [fid for fid in dict.fromkeys(function_ids) if fid]
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            marks = _placeholders(len(chunk))
            rows = self._conn.execute(
                f"SELECT id, name, file, line, tu_id FROM functions WHERE id IN ({marks})", chunk
            ).fetchall()
            for row in rows:
                result[row["id"]] = {
                    "id": row["id"],
                    "name": row["name"],
                    "file": row["file"],
                    "line": row["line"],
                    "tu_id": row["tu_id"],
                }
        return result

    def deep_ready_map(self, function_ids: Sequence[str]) -> dict[str, bool]:
        """``function_id -> deep_ready`` (all False on L0-only/old databases)."""

        ids = [fid for fid in dict.fromkeys(function_ids) if fid]
        result = {fid: False for fid in ids}
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            marks = _placeholders(len(chunk))
            try:
                rows = self._conn.execute(
                    f"SELECT function_id FROM deep_status WHERE status = 'ready' "
                    f"AND function_id IN ({marks})",
                    chunk,
                ).fetchall()
            except sqlite3.Error:
                return result
            for row in rows:
                result[row[0]] = True
        return result

    def query_call_targets(
        self, symbol: str, direction: str = "callee", limit: int = 200
    ) -> list[dict[str, Any]]:
        """Candidate call_targets for a symbol (``direction`` callee|caller)."""

        if direction not in ("callee", "caller"):
            raise ValueError("direction must be 'callee' or 'caller'")
        if limit <= 0:
            return []
        ids = self._matching_function_ids(symbol)
        if not ids:
            return []
        marks = _placeholders(len(ids))
        join_column = "ct.caller_id" if direction == "callee" else "ct.callee_id"
        sql = (
            "SELECT ct.call_id, ct.caller_id, cf.name AS caller_name, "
            "ct.callee_id, COALESCE(NULLIF(tf.name, ''), ct.callee_name) AS callee_name, "
            "COALESCE(tf.file, '') AS callee_file, COALESCE(tf.line, 0) AS callee_line, "
            "COALESCE(c.file, ct.file) AS call_file, COALESCE(c.line, ct.line) AS call_line, "
            "ct.kind, ct.confidence, ct.reason, 'call_target' AS source "
            "FROM call_targets AS ct "
            "LEFT JOIN calls AS c ON c.id = ct.call_id "
            "LEFT JOIN functions AS cf ON cf.id = ct.caller_id "
            "LEFT JOIN functions AS tf ON tf.id = ct.callee_id "
            f"WHERE {join_column} IN ({marks}) "
            "ORDER BY call_file, call_line, ct.caller_id, ct.callee_id, ct.id LIMIT ?"
        )
        rows = []
        for row in self._conn.execute(sql, [*ids, limit]):
            data = dict(row)
            data["candidate"] = True
            rows.append(data)
        return rows

    # ------------------------------------------------------------------ #
    # Public query API
    # ------------------------------------------------------------------ #
    def query_functions(self, pattern: str, limit: int = 50) -> list[dict[str, Any]]:
        """Exact name/id hits win; substring search runs only when nothing matches.

        Substring queries use the FTS5 trigram index built by ``finalize()``
        (arbitrary substrings stay index-backed) and degrade to the previous
        ``LIKE '%pattern%'`` scan when FTS5 is unavailable or the pattern is
        shorter than 3 characters. Returned keys and ordering are unchanged.
        """

        if not pattern or limit <= 0:
            return []
        columns = ", ".join(FUNCTION_SUMMARY_COLUMNS)
        rows = self._conn.execute(
            f"SELECT {columns} FROM functions "
            "WHERE id = ? OR name = ? OR qualified_name = ? "
            "ORDER BY is_definition DESC, file, line, id LIMIT ?",
            (pattern, pattern, pattern, limit),
        ).fetchall()
        if rows:
            return [self._summary(row) for row in rows]
        rows = self._substring_summary_rows(pattern, FUNCTION_SUMMARY_COLUMNS, limit)
        return [self._summary(row) for row in rows]

    @staticmethod
    def _summary(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["is_definition"] = bool(data["is_definition"])
        data["is_static"] = bool(data["is_static"])
        return data

    def query_callers(self, symbol: str, limit: int = 200) -> list[dict[str, Any]]:
        """Who calls ``symbol``: merged direct calls + candidate call_targets.

        Keys: ``caller_id, caller_name, caller_file, caller_line, callee_name,
        call_file, call_line, is_resolved, kind, confidence, source`` where
        ``source`` is ``direct`` (CallRecord, confidence 1.0) or ``candidate``
        (CallTargetRecord with its heuristic ``confidence``); duplicates are
        collapsed with the direct edge winning. Unresolved calls
        (``callee_id == ''``) are matched by their redundant ``callee_name``.
        """

        if not symbol or limit <= 0:
            return []
        rows: list[dict[str, Any]] = []
        function_ids = self._matching_function_ids(symbol)
        if function_ids:
            for edge in self.load_reverse_edges(function_ids, limit):
                rows.append(
                    {
                        "caller_id": edge["caller_id"],
                        "caller_name": edge["caller_name"],
                        "caller_file": edge["caller_file"],
                        "caller_line": edge["caller_line"],
                        "callee_name": edge["callee_name"],
                        "call_file": edge["call_file"],
                        "call_line": edge["call_line"],
                        "is_resolved": edge["is_resolved"],
                        "kind": edge["kind"],
                        "confidence": edge["confidence"],
                        "source": edge["source"],
                        "candidate": edge["candidate"],
                        "_callee_id": edge["callee_id"],
                        "_rank": 0 if edge["source"] == "call" else 1,
                    }
                )
        # Callee id may be empty (unresolved) but the parser still recorded the
        # target name; keep those callers attributed.
        rows.extend(
            self._unresolved_caller_rows(symbol, limit)
        )
        deduped: dict[tuple[Any, ...], dict[str, Any]] = {}
        for row in rows:
            key = (
                row["caller_id"],
                row["_callee_id"],
                row["callee_name"],
                row["call_file"],
                row["call_line"],
            )
            current = deduped.get(key)
            if current is None or row["_rank"] < current["_rank"]:
                deduped[key] = row
        ordered = sorted(
            deduped.values(),
            key=lambda row: (
                row["call_file"],
                row["call_line"],
                row["_rank"],
                row["caller_id"],
                row["callee_name"],
            ),
        )[:limit]
        for row in ordered:
            row.pop("_callee_id", None)
            row.pop("_rank", None)
        return ordered

    def _unresolved_caller_rows(self, symbol: str, limit: int) -> list[dict[str, Any]]:
        sql = (
            "SELECT c.caller_id, COALESCE(caller.name, '') AS caller_name, "
            "COALESCE(caller.file, '') AS caller_file, COALESCE(caller.line, 0) AS caller_line, "
            "c.callee_name, c.file AS call_file, c.line AS call_line, c.is_resolved, "
            "c.kind AS kind, 1.0 AS confidence, 'call' AS source, c.callee_id AS _callee_id, "
            "0 AS _rank "
            "FROM calls AS c LEFT JOIN functions AS caller ON caller.id = c.caller_id "
            "WHERE c.callee_id = '' AND c.callee_name = ? "
            "ORDER BY c.file, c.line, c.id LIMIT ?"
        )
        return [dict(row) for row in self._conn.execute(sql, (symbol, limit))]

    def query_callees(self, symbol: str, limit: int = 200) -> list[dict[str, Any]]:
        """What ``symbol`` calls: merged direct calls + candidate call_targets.

        Keys: ``callee_id, callee_name, callee_file, callee_line, call_file,
        call_line, is_resolved, kind, confidence, source``; ``source`` is
        ``direct`` or ``candidate`` and duplicates keep the direct edge.
        """

        if not symbol or limit <= 0:
            return []
        function_ids = self._matching_function_ids(symbol)
        if not function_ids:
            return []
        rows: list[dict[str, Any]] = []
        for edge in self.load_forward_edges(function_ids, limit):
            rows.append(
                {
                    "callee_id": edge["callee_id"],
                    "callee_name": edge["callee_name"],
                    "callee_file": edge["callee_file"],
                    "callee_line": edge["callee_line"],
                    "call_file": edge["call_file"],
                    "call_line": edge["call_line"],
                    "is_resolved": edge["is_resolved"],
                    "kind": edge["kind"],
                    "confidence": edge["confidence"],
                    "source": edge["source"],
                    "candidate": edge["candidate"],
                    "_rank": 0 if edge["source"] == "call" else 1,
                }
            )
        deduped: dict[tuple[Any, ...], dict[str, Any]] = {}
        for row in rows:
            key = (
                row["callee_id"],
                row["callee_name"],
                row["call_file"],
                row["call_line"],
            )
            current = deduped.get(key)
            if current is None or row["_rank"] < current["_rank"]:
                deduped[key] = row
        ordered = sorted(
            deduped.values(),
            key=lambda row: (
                row["call_file"],
                row["call_line"],
                row["_rank"],
                row["callee_id"],
                row["callee_name"],
            ),
        )[:limit]
        for row in ordered:
            row.pop("_rank", None)
        return ordered

    def query_function_detail(self, symbol: str) -> dict[str, Any]:
        function_ids = self._matching_function_ids(symbol)
        if not function_ids:
            return {
                "function": None,
                "deep_ready": False,
                "branches": [],
                "assignments": [],
                "returns": [],
                "resource_ops": [],
                "findings": [],
            }
        function_id = function_ids[0]
        return {
            "function": self._function_row(function_id),
            "deep_ready": self.deep_ready_map([function_id]).get(function_id, False),
            "branches": self._fact_rows("branch", [function_id]),
            "assignments": self._fact_rows("assignment", [function_id]),
            "returns": self._fact_rows("return", [function_id]),
            "resource_ops": self._fact_rows("resource_op", [function_id]),
            "findings": self._detail_findings(function_id),
        }

    def query_branches(self, symbol: str) -> list[dict[str, Any]]:
        return self._fact_rows("branch", self._matching_function_ids(symbol))

    def query_assignments(self, symbol: str) -> list[dict[str, Any]]:
        return self._fact_rows("assignment", self._matching_function_ids(symbol))

    def query_returns(self, symbol: str) -> list[dict[str, Any]]:
        return self._fact_rows("return", self._matching_function_ids(symbol))

    def query_resource_ops(self, symbol: str) -> list[dict[str, Any]]:
        return self._fact_rows("resource_op", self._matching_function_ids(symbol))

    def _detail_findings(self, function_id: str) -> list[dict[str, Any]]:
        sql = (
            "SELECT f.id, f.function_id, COALESCE(fn.name, '') AS function_name, f.kind, "
            "f.resource_kind, f.message, f.severity, f.acquire_id, f.release_ids, "
            "f.path_blocks, f.evidence, COALESCE(fn.file, '') AS file, COALESCE(fn.line, 0) AS line "
            "FROM findings AS f LEFT JOIN functions AS fn ON fn.id = f.function_id "
            "WHERE f.function_id = ? ORDER BY f.id"
        )
        return [self._finding_row(row) for row in self._conn.execute(sql, (function_id,))]

    @staticmethod
    def _finding_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        for column in ("release_ids", "path_blocks", "evidence"):
            data[column] = _decode_json(data.get(column))
        return data

    def query_findings(self, kind: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        sql = (
            "SELECT f.id, f.function_id, COALESCE(fn.name, '') AS function_name, f.kind, "
            "f.resource_kind, f.message, f.severity, COALESCE(fn.file, '') AS file, "
            "COALESCE(fn.line, 0) AS line "
            "FROM findings AS f LEFT JOIN functions AS fn ON fn.id = f.function_id "
        )
        if kind is None:
            sql += "ORDER BY f.id LIMIT ?"
            params: tuple[Any, ...] = (limit,)
        else:
            sql += "WHERE f.kind = ? ORDER BY f.id LIMIT ?"
            params = (kind, limit)
        return [dict(row) for row in self._conn.execute(sql, params)]

    def query_cfg(
        self, symbol: str, max_blocks: int = 500, max_edges: int = 1000
    ) -> dict[str, Any]:
        """Control-flow graph plus branch annotations for one function symbol.

        Keys: ``function_ids, blocks, edges, branches, truncated``. Blocks carry
        ``condition``/``branch_kind``/``true_block``/``false_block`` when a
        branch record targets the block (matched by ``block_id``) and the
        ``is_entry``/``is_exit`` display flags. Edges carry a display ``label``:
        ``true``/``false`` for edges leaving a branch block and matching its
        ``true_block``/``false_block``, otherwise the stored label/kind.
        Rows are bounded by ``max_blocks``/``max_edges``; ``truncated`` reports
        that a bound was hit.
        """

        function_ids = self._matching_function_ids(symbol)
        if not function_ids:
            return {
                "function_ids": [],
                "blocks": [],
                "edges": [],
                "branches": [],
                "truncated": False,
            }
        marks = _placeholders(len(function_ids))
        block_limit = max(1, int(max_blocks))
        edge_limit = max(1, int(max_edges))
        blocks = [
            dict(row)
            for row in self._conn.execute(
                "SELECT b.id, b.function_id, b.\"index\" AS \"index\", b.kind, "
                'b.terminator, b.file, b.line, b.col, b.end_line, b.end_col, '
                "b.statement_count FROM cfg_blocks AS b "
                f'WHERE b.function_id IN ({marks}) ORDER BY b."index", b.id LIMIT ?',
                [*function_ids, block_limit],
            )
        ]
        edges = [
            dict(row)
            for row in self._conn.execute(
                "SELECT e.id, e.function_id, e.src, e.dst, e.kind, e.label "
                "FROM cfg_edges AS e "
                f"WHERE e.function_id IN ({marks}) ORDER BY e.id LIMIT ?",
                [*function_ids, edge_limit],
            )
        ]
        branches = [
            dict(row)
            for row in self._conn.execute(
                "SELECT id, function_id, block_id, file, line, col, end_line, "
                "end_col, kind, condition, var_names, true_block, false_block, "
                "is_negated, case_value FROM branches "
                f"WHERE function_id IN ({marks}) ORDER BY file, line, id",
                list(function_ids),
            )
        ]
        for branch in branches:
            branch["var_names"] = _decode_json(branch.get("var_names"))
            branch["is_negated"] = bool(branch.get("is_negated"))
        branch_by_block = {
            branch["block_id"]: branch for branch in branches if branch["block_id"]
        }
        for block in blocks:
            branch = branch_by_block.get(block["id"])
            block["condition"] = branch["condition"] if branch else ""
            block["branch_kind"] = branch["kind"] if branch else ""
            block["true_block"] = branch["true_block"] if branch else ""
            block["false_block"] = branch["false_block"] if branch else ""
            block["is_entry"] = block.get("kind") == "entry"
            block["is_exit"] = (
                block.get("kind") == "exit" or block.get("terminator") == "return"
            )
        for edge in edges:
            branch = branch_by_block.get(edge["src"])
            label = str(edge.get("label") or "")
            if branch:
                if branch["true_block"] and edge["dst"] == branch["true_block"]:
                    label = "true"
                elif branch["false_block"] and edge["dst"] == branch["false_block"]:
                    label = "false"
            edge["label"] = label or str(edge.get("kind") or "")
        return {
            "function_ids": function_ids,
            "blocks": blocks,
            "edges": edges,
            "branches": branches,
            "truncated": len(blocks) >= block_limit or len(edges) >= edge_limit,
        }

    def stats(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for table in TABLES:
            try:
                counts[table] = self._conn.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
            except sqlite3.OperationalError:
                # Jobs/events live in an optional state DB; a regular index
                # database legitimately lacks those tables.
                continue
        page_count = self._conn.execute("PRAGMA page_count").fetchone()[0]
        page_size = self._conn.execute("PRAGMA page_size").fetchone()[0]
        row = self._conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        return {
            "tables": counts,
            "db_bytes": page_count * page_size,
            "schema_version": row[0] if row else SCHEMA_VERSION,
            "sqlite_version": sqlite3.sqlite_version,
        }

    # ------------------------------------------------------------------ #
    # Streaming export
    # ------------------------------------------------------------------ #
    def iter_records(
        self,
        kinds: tuple[str, ...] | None = None,
        limit: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        if kinds is None:
            specs: tuple[RecordSpec, ...] = RECORD_SPECS
        else:
            unknown = [kind for kind in kinds if kind not in _SPECS_BY_KIND]
            if unknown:
                raise ValueError(f"unknown record kinds: {', '.join(unknown)}")
            wanted = set(kinds)
            specs = tuple(spec for spec in RECORD_SPECS if spec[0] in wanted)
        if limit is not None and limit <= 0:
            return
        emitted = 0
        for spec in specs:
            _kind, table, columns, _json, _bool = spec
            cursor = self._conn.execute(f"SELECT {', '.join(columns)} FROM {table} ORDER BY id")
            while True:
                chunk = cursor.fetchmany(1000)
                if not chunk:
                    break
                for row in chunk:
                    yield _export_convert(spec, dict(row))
                    emitted += 1
                    if limit is not None and emitted >= limit:
                        return
