"""Virtual dispatch persistence and candidate-edge SQL (task-37).

The parser records C++ dispatch metadata additively on the core records:

- ``FunctionRecord.is_virtual`` / ``is_pure_virtual`` / ``overrides``
  (ids of directly overridden declarations),
- ``TypeRecord.base_types`` (direct base type ids).

Store layout added on top of schema v6 (idempotent, additive only):

- ``functions.is_virtual``, ``functions.is_pure_virtual``,
  ``functions.overrides_json``;
- ``types.bases_json``;
- ``virtual_overrides(function_id, base_function_id)``: direct override edges,
  indexed in both directions;
- ``type_bases(type_id, base_type_id)``: direct inheritance edges.

Candidate edges are computed at query time (not materialized) so cross-TU data
from all indexed shards participates: a virtual call whose static target is
``Base::m`` gains a ``virtual_candidate`` edge to every known override of
``Base::m`` (transitive override chain, depth <= 3). Reverse queries follow the
same relation upwards: callers of ``Derived::m`` include virtual call sites
whose static target is an ancestor method. Confidence decreases with depth and
candidate edges never replace direct ones.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Sequence

VIRTUAL_OVERRIDES_TABLE = "virtual_overrides"
TYPE_BASES_TABLE = "type_bases"
_DEPTH_CONFIDENCE = {1: 0.6, 2: 0.5, 3: 0.45}
MAX_DEPTH = 3

_SCHEMA_STATEMENTS = (
    "CREATE TABLE IF NOT EXISTS virtual_overrides ("
    "function_id TEXT NOT NULL, base_function_id TEXT NOT NULL, "
    "PRIMARY KEY (function_id, base_function_id))",
    "CREATE INDEX IF NOT EXISTS idx_virtual_overrides_base "
    "ON virtual_overrides(base_function_id, function_id)",
    "CREATE TABLE IF NOT EXISTS type_bases ("
    "type_id TEXT NOT NULL, base_type_id TEXT NOT NULL, "
    "PRIMARY KEY (type_id, base_type_id))",
    "CREATE INDEX IF NOT EXISTS idx_type_bases_base ON type_bases(base_type_id, type_id)",
)

_FUNCTION_COLUMNS = {
    "is_virtual": "INTEGER NOT NULL DEFAULT 0",
    "is_pure_virtual": "INTEGER NOT NULL DEFAULT 0",
    "overrides_json": "TEXT NOT NULL DEFAULT '[]'",
}
_TYPE_COLUMNS = {"bases_json": "TEXT NOT NULL DEFAULT '[]'"}


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    try:
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:  # pragma: no cover - defensive
        return set()
    return {row[1] for row in rows}


def migrate(connection: sqlite3.Connection) -> None:
    """Idempotently add the virtual-dispatch layout to new and old databases."""

    function_columns = _table_columns(connection, "functions")
    if function_columns:
        for name, declaration in _FUNCTION_COLUMNS.items():
            if name not in function_columns:
                connection.execute(f"ALTER TABLE functions ADD COLUMN {name} {declaration}")
    type_columns = _table_columns(connection, "types")
    if type_columns:
        for name, declaration in _TYPE_COLUMNS.items():
            if name not in type_columns:
                connection.execute(f"ALTER TABLE types ADD COLUMN {name} {declaration}")
    for statement in _SCHEMA_STATEMENTS:
        connection.execute(statement)


def has_tables(connection: sqlite3.Connection) -> bool:
    try:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (VIRTUAL_OVERRIDES_TABLE,),
        ).fetchone()
    except sqlite3.Error:  # pragma: no cover - defensive
        return False
    return row is not None


def _confidence_case(depth_column: str) -> str:
    return (
        "CASE "
        + " ".join(
            f"WHEN {depth_column} = {depth} THEN {confidence}"
            for depth, confidence in _DEPTH_CONFIDENCE.items()
        )
        + f" ELSE {_DEPTH_CONFIDENCE[MAX_DEPTH]} END"
    )


_FORWARD_SQL = f"""
WITH RECURSIVE vt(function_id, base_function_id, depth) AS (
    SELECT function_id, base_function_id, 1 FROM virtual_overrides
    UNION
    -- Walk *down* the override chain: find methods that override the previous
    -- candidate while keeping the original static base target for the join.
    SELECT vo.function_id, vt.base_function_id, vt.depth + 1
    FROM virtual_overrides AS vo
    JOIN vt ON vo.base_function_id = vt.function_id
    WHERE vt.depth < {MAX_DEPTH}
),
edges(function_id, base_function_id, depth) AS (
    SELECT function_id, base_function_id, MIN(depth)
    FROM vt GROUP BY function_id, base_function_id
)
SELECT c.id AS call_id, c.caller_id AS caller_id, cf.name AS caller_name,
       cf.file AS caller_file, cf.line AS caller_line,
       edges.function_id AS callee_id,
       COALESCE(NULLIF(tf.name, ''), edges.function_id) AS callee_name,
       COALESCE(tf.file, '') AS callee_file, COALESCE(tf.line, 0) AS callee_line,
       c.file AS call_file, c.line AS call_line, 'virtual_candidate' AS kind,
       {_confidence_case("edges.depth")} AS confidence, 0 AS is_resolved,
       'virtual_candidate' AS source, 2 AS source_rank
FROM calls AS c
JOIN edges ON c.callee_id = edges.base_function_id
LEFT JOIN functions AS cf ON cf.id = c.caller_id
LEFT JOIN functions AS tf ON tf.id = edges.function_id
WHERE c.kind = 'virtual' AND c.caller_id IN ({{marks}})
ORDER BY call_file, call_line, caller_id, callee_id
LIMIT ?
"""

def _placeholders(count: int) -> str:
    return ", ".join("?" for _ in range(count))


def forward_edges(
    connection: sqlite3.Connection, caller_ids: Sequence[str], limit: int = 500
) -> list[dict[str, Any]]:
    """Virtual-candidate edges whose caller is in ``caller_ids``."""

    ids = [value for value in dict.fromkeys(caller_ids) if value]
    if not ids or limit <= 0:
        return []
    sql = _FORWARD_SQL.format(marks=_placeholders(len(ids)))
    try:
        rows = connection.execute(sql, [*ids, limit]).fetchall()
    except sqlite3.OperationalError:  # missing virtual tables on old databases
        return []
    result = []
    for row in rows:
        data = dict(row)
        data["candidate"] = True
        data["confidence"] = float(data["confidence"])
        data["is_resolved"] = bool(data["is_resolved"])
        result.append(data)
    return result


def _reverse_sql(seed_count: int) -> str:
    seed_rows = " UNION ALL ".join("SELECT ? AS id" for _ in range(seed_count))
    return f"""
WITH RECURSIVE anc(function_id, ancestor_id, depth) AS (
    SELECT id, id, 0 FROM (
        {seed_rows}
    )
    UNION
    SELECT anc.function_id, vo.base_function_id, anc.depth + 1
    FROM virtual_overrides AS vo
    JOIN anc ON vo.function_id = anc.ancestor_id
    WHERE anc.depth < {MAX_DEPTH}
),
edges(function_id, ancestor_id, depth) AS (
    SELECT function_id, ancestor_id, MIN(depth)
    FROM anc WHERE depth > 0 GROUP BY function_id, ancestor_id
)
SELECT c.id AS call_id, c.caller_id AS caller_id, cf.name AS caller_name,
       cf.file AS caller_file, cf.line AS caller_line,
       edges.function_id AS callee_id,
       COALESCE(NULLIF(tf.name, ''), edges.function_id) AS callee_name,
       COALESCE(tf.file, '') AS callee_file, COALESCE(tf.line, 0) AS callee_line,
       c.file AS call_file, c.line AS call_line, 'virtual_candidate' AS kind,
       {_confidence_case("edges.depth")} AS confidence, 0 AS is_resolved,
       'virtual_candidate' AS source, 2 AS source_rank
FROM calls AS c
JOIN edges ON c.callee_id = edges.ancestor_id
LEFT JOIN functions AS cf ON cf.id = c.caller_id
LEFT JOIN functions AS tf ON tf.id = edges.function_id
WHERE c.kind = 'virtual' AND edges.function_id IN ({_placeholders(seed_count)})
ORDER BY call_file, call_line, caller_id, callee_id
LIMIT ?
"""


def reverse_edges(
    connection: sqlite3.Connection, callee_ids: Sequence[str], limit: int = 500
) -> list[dict[str, Any]]:
    """Virtual-candidate edges whose (dynamic) callee is in ``callee_ids``."""

    ids = [value for value in dict.fromkeys(callee_ids) if value]
    if not ids or limit <= 0:
        return []
    sql = _reverse_sql(len(ids))
    try:
        rows = connection.execute(sql, [*ids, *ids, limit]).fetchall()
    except sqlite3.OperationalError:  # missing virtual tables on old databases
        return []
    result = []
    for row in rows:
        data = dict(row)
        data["candidate"] = True
        data["confidence"] = float(data["confidence"])
        data["is_resolved"] = bool(data["is_resolved"])
        result.append(data)
    return result
