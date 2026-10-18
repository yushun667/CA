"""Incremental per-TU database maintenance (``ca update`` / ``ca watch``).

Full re-indexing is too slow for small edits, so the store exposes two atomic
primitives that mutate one or more translation units in place:

- :func:`remove_units` cascades a TU (or a batch of TUs) out of every
  dependent table: ``tus``, ``functions``, ``types``, ``fields``, ``calls``,
  ``cfg_blocks``/``cfg_edges``, ``branches``/``assignments``/``returns``/
  ``resource_ops``/``findings``, ``call_targets``, callback registrations and
  their links, ``deep_status`` and ``tu_includes``.
- :func:`replace_units` removes the old rows and streams the new
  ``ParseResult``/``AnalysisResult``/callback/include records back in **one
  transaction**, without running ``finalize()`` and without rebuilding the
  whole database.

FTS5 consistency: ``functions_fts`` is an external-content index over
``functions``. For every touched function id the helper captures the old
``(rowid, name, qualified_name)`` tuple, issues the FTS5 ``delete`` command, and
after the writes re-inserts the current row, so substring search is correct
immediately after an incremental update when ``meta.fts_ready='1'``. Databases
without a usable FTS index simply skip this step (queries fall back to LIKE and
``finalize()`` can rebuild the index later).

Counts returned by the primitives are the contract for ``ca update`` logging:
:func:`remove_units` returns ``{table: deleted_rows}``; :func:`replace_units`
returns ``{tu_ids, removed, added, fts_deleted, fts_inserted, deep_ready}``.
Unknown TU ids and repeated calls are idempotent (all-zero counts). Both
functions join an already-open store transaction, otherwise they run their own
``BEGIN IMMEDIATE`` transaction and commit/rollback it.

Limitations: function rows are attributed to the first TU that inserted them
(header-inline definitions are deduplicated), so callers must re-index every TU
that contributed records for the affected files (``tu_includes`` /
``dependent_tus``); callbacks and includes are only re-added when passed to
:func:`replace_units`; no cross-shard transaction is involved.
"""

from __future__ import annotations

import dataclasses
import logging
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterable, Iterator, Mapping, Sequence

from ca.store import writer
from ca.store.callbacks import CallbackRegistry

logger = logging.getLogger(__name__)

# Tables whose rows are owned by a translation unit (in delete order).
UNIT_TABLES: tuple[str, ...] = (
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
    "callback_registrations",
    "callback_target_links",
    "deep_status",
    "tu_includes",
)

# Tables that may be absent on very old databases / minimal fixtures.
_OPTIONAL_TABLES = frozenset({"call_targets", "callback_registrations", "callback_target_links", "deep_status"})

_CHUNK = 500


# --------------------------------------------------------------------------- #
# Small SQL helpers (all values are bound; only internal identifiers are f-string)
# --------------------------------------------------------------------------- #
def _chunks(values: Sequence[Any], size: int = _CHUNK) -> Iterator[Sequence[Any]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _marks(count: int) -> str:
    return ", ".join("?" for _ in range(count))


def _has_table(connection: sqlite3.Connection, table: str) -> bool:
    try:
        return (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
                (table,),
            ).fetchone()
            is not None
        )
    except sqlite3.Error:  # pragma: no cover - defensive
        return False


def _select_ids(
    connection: sqlite3.Connection, table: str, select_column: str, where_column: str, values: Sequence[Any]
) -> list[str]:
    if not values or not _has_table(connection, table):
        return []
    found: list[str] = []
    for chunk in _chunks(list(values)):
        rows = connection.execute(
            f"SELECT DISTINCT {select_column} FROM {table} WHERE {where_column} IN ({_marks(len(chunk))})",
            list(chunk),
        ).fetchall()
        found.extend(str(row[0]) for row in rows)
    return found


def _delete_in(
    connection: sqlite3.Connection, table: str, column: str, values: Sequence[Any]
) -> int:
    if not values or not _has_table(connection, table):
        return 0
    deleted = 0
    for chunk in _chunks(list(values)):
        cursor = connection.execute(
            f"DELETE FROM {table} WHERE {column} IN ({_marks(len(chunk))})", list(chunk)
        )
        deleted += max(0, int(cursor.rowcount or 0))
    return deleted


def _delete_any(
    connection: sqlite3.Connection, table: str, columns: Sequence[str], values: Sequence[Any]
) -> int:
    """Delete rows matching ``ANY`` of ``columns IN values`` (deduped count)."""

    if not values or not _has_table(connection, table):
        return 0
    columns = [column for column in columns]
    deleted = 0
    for column in columns:
        deleted += _delete_in(connection, table, column, values)
    return deleted


# --------------------------------------------------------------------------- #
# FTS5 incremental maintenance
# --------------------------------------------------------------------------- #
def _fts_enabled(connection: sqlite3.Connection) -> bool:
    if not _has_table(connection, "functions_fts"):
        return False
    try:
        row = connection.execute(
            "SELECT value FROM meta WHERE key = 'fts_ready'"
        ).fetchone()
    except sqlite3.Error:  # pragma: no cover - defensive
        return False
    return bool(row and str(row[0]) == "1")


def _capture_fts_rows(
    connection: sqlite3.Connection, function_ids: Sequence[str]
) -> list[tuple[int, str, str]]:
    """Old ``(rowid, name, qualified_name)`` tuples for the FTS delete command."""

    rows: list[tuple[int, str, str]] = []
    if not function_ids:
        return rows
    for chunk in _chunks(list(dict.fromkeys(function_ids))):
        for row in connection.execute(
            "SELECT rowid, name, qualified_name FROM functions "
            f"WHERE id IN ({_marks(len(chunk))})",
            list(chunk),
        ):
            rows.append((int(row[0]), str(row[1]), str(row[2])))
    return rows


def _fts_delete(connection: sqlite3.Connection, rows: Sequence[tuple[int, str, str]]) -> int:
    if not rows:
        return 0
    try:
        connection.executemany(
            "INSERT INTO functions_fts(functions_fts, rowid, name, qualified_name) "
            "VALUES ('delete', ?, ?, ?)",
            rows,
        )
        return len(rows)
    except sqlite3.Error as exc:  # pragma: no cover - defensive; finalize can rebuild
        logger.warning("incremental: FTS delete failed (%s); run finalize to rebuild", exc)
        return 0


def _fts_insert(
    connection: sqlite3.Connection, function_ids: Sequence[str]
) -> int:
    inserted = 0
    if not function_ids:
        return inserted
    for chunk in _chunks(list(dict.fromkeys(function_ids))):
        try:
            cursor = connection.execute(
                "INSERT INTO functions_fts(rowid, name, qualified_name) "
                f"SELECT rowid, name, qualified_name FROM functions WHERE id IN ({_marks(len(chunk))})",
                list(chunk),
            )
            inserted += max(0, int(cursor.rowcount or 0))
        except sqlite3.Error as exc:  # pragma: no cover - defensive
            logger.warning("incremental: FTS insert failed (%s); run finalize to rebuild", exc)
    return inserted


# --------------------------------------------------------------------------- #
# Transaction handling
# --------------------------------------------------------------------------- #
@contextmanager
def _transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    if connection.in_transaction:
        yield connection
        return
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    else:
        connection.execute("COMMIT")


# --------------------------------------------------------------------------- #
# Reference collection + cascade delete
# --------------------------------------------------------------------------- #
def _normalize_ids(tu_ids: Iterable[Any]) -> list[str]:
    if isinstance(tu_ids, str):
        tu_ids = [tu_ids]
    return list(dict.fromkeys(str(item) for item in tu_ids or [] if str(item)))


def _collect_references(connection: sqlite3.Connection, tu_ids: Sequence[str]) -> dict[str, Any]:
    function_ids = _select_ids(connection, "functions", "id", "tu_id", tu_ids)
    call_ids = _select_ids(connection, "calls", "id", "tu_id", tu_ids)
    registration_ids = _select_ids(
        connection, "callback_registrations", "id", "tu_id", tu_ids
    )
    call_target_ids: list[str] = []
    if _has_table(connection, "call_targets"):
        for column, values in (
            ("caller_id", function_ids),
            ("callee_id", function_ids),
            ("call_id", call_ids),
        ):
            call_target_ids.extend(
                _select_ids(connection, "call_targets", "id", column, values)
            )
        call_target_ids = list(dict.fromkeys(call_target_ids))
    return {
        "tu_ids": list(tu_ids),
        "function_ids": function_ids,
        "call_ids": call_ids,
        "registration_ids": registration_ids,
        "call_target_ids": call_target_ids,
    }


def _delete_references(
    connection: sqlite3.Connection, references: Mapping[str, Any]
) -> dict[str, int]:
    tu_ids = references["tu_ids"]
    function_ids = references["function_ids"]
    call_ids = references["call_ids"]
    registration_ids = references["registration_ids"]
    call_target_ids = references["call_target_ids"]
    counts = {table: 0 for table in UNIT_TABLES}

    if _has_table(connection, "callback_target_links"):
        counts["callback_target_links"] = _delete_any(
            connection,
            "callback_target_links",
            ("registration_id", "call_target_id"),
            [*registration_ids, *call_target_ids],
        )
    counts["call_targets"] = _delete_any(
        connection, "call_targets", ("caller_id", "callee_id", "call_id"),
        [*function_ids, *call_ids],
    )
    for table in (
        "findings",
        "resource_ops",
        "returns",
        "assignments",
        "branches",
        "cfg_edges",
        "cfg_blocks",
    ):
        counts[table] = _delete_in(connection, table, "function_id", function_ids)
    counts["calls"] = _delete_in(connection, "calls", "tu_id", tu_ids)
    counts["fields"] = _delete_in(connection, "fields", "tu_id", tu_ids)
    counts["types"] = _delete_in(connection, "types", "tu_id", tu_ids)
    counts["functions"] = _delete_in(connection, "functions", "id", function_ids)
    counts["callback_registrations"] = _delete_in(
        connection, "callback_registrations", "tu_id", tu_ids
    )
    if _has_table(connection, "deep_status"):
        counts["deep_status"] = _delete_any(
            connection, "deep_status", ("function_id", "tu_id"), [*function_ids, *tu_ids]
        )
    counts["tu_includes"] = _delete_in(connection, "tu_includes", "tu_id", tu_ids)
    counts["tus"] = _delete_in(connection, "tus", "id", tu_ids)
    return counts


def _remove_units_tx(connection: sqlite3.Connection, tu_ids: Sequence[str]) -> dict[str, int]:
    counts = {table: 0 for table in UNIT_TABLES}
    if not tu_ids:
        return counts
    references = _collect_references(connection, tu_ids)
    fts_rows = (
        _capture_fts_rows(connection, references["function_ids"])
        if _fts_enabled(connection)
        else []
    )
    counts = _delete_references(connection, references)
    if fts_rows:
        _fts_delete(connection, fts_rows)
        counts["functions_fts"] = len(fts_rows)  # type: ignore[assignment]
    return counts


# --------------------------------------------------------------------------- #
# Public primitives
# --------------------------------------------------------------------------- #
def remove_units(store: Any, tu_ids: Iterable[Any]) -> dict[str, int]:
    """Cascade-delete every row owned by ``tu_ids`` (atomic, idempotent).

    Returns ``{table: deleted_rows}`` for :data:`UNIT_TABLES` (plus
    ``functions_fts`` when the FTS index was maintained). Unknown ids produce
    all-zero counts; an empty input is a no-op.
    """

    ids = _normalize_ids(tu_ids)
    connection = store._require_connection()
    with _transaction(connection):
        return _remove_units_tx(connection, ids)


def _normalize_units(units: Iterable[Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for unit in units:
        callbacks: Sequence[Any] = ()
        include_paths: Sequence[str] | None = None
        if isinstance(unit, Mapping):
            parse_result = unit.get("parse") or unit.get("parse_result")
            analyses = unit.get("analyses") or ()
            callbacks = unit.get("callbacks") or ()
            include_paths = unit.get("include_paths")
        else:
            try:
                values = tuple(unit)
            except TypeError:
                raise ValueError("replace_units items must be mappings or tuples") from None
            if len(values) < 2:
                raise ValueError("replace_units tuples need (parse_result, analyses)")
            parse_result, analyses = values[0], values[1]
            if len(values) >= 3:
                callbacks = values[2]
            if len(values) >= 4:
                include_paths = values[3]
        if parse_result is None or getattr(parse_result, "tu", None) is None:
            raise ValueError("replace_units item has no ParseResult")
        tu_id = str(parse_result.tu.id)
        if tu_id in seen:
            raise ValueError(f"duplicate TU in replace_units: {tu_id}")
        seen.add(tu_id)
        normalized.append(
            {
                "parse": parse_result,
                "analyses": tuple(analyses or ()),
                "callbacks": tuple(callbacks or ()),
                "include_paths": include_paths,
                "tu_id": tu_id,
            }
        )
    return normalized


def replace_units(
    store: Any,
    units: Iterable[Any],
    *,
    deep_ready: bool = True,
) -> dict[str, Any]:
    """Replace one or more TUs in a single transaction (incremental write-back).

    ``units`` items are mappings ``{parse, analyses, callbacks, include_paths}``
    or tuples ``(parse_result, analyses[, callbacks[, include_paths]])``. The old
    rows for each TU are removed first (same cascade as :func:`remove_units`),
    then new records are inserted; ``AnalysisResult`` functions are marked
    deep-ready when ``deep_ready=True``. FTS5 docs are updated incrementally via
    the delete/insert commands (no full rebuild).

    Returns ``{tu_ids, removed, added, fts_deleted, fts_inserted, deep_ready}``.
    """

    normalized = _normalize_units(units)
    result: dict[str, Any] = {
        "tu_ids": [item["tu_id"] for item in normalized],
        "removed": {table: 0 for table in UNIT_TABLES},
        "added": {},
        "fts_deleted": 0,
        "fts_inserted": 0,
        "deep_ready": 0,
    }
    if not normalized:
        return result
    connection = store._require_connection()
    tu_ids = result["tu_ids"]
    with _transaction(connection):
        references = _collect_references(connection, tu_ids)
        touched_function_ids = set(references["function_ids"])
        for item in normalized:
            touched_function_ids.update(
                str(function.id) for function in item["parse"].functions
            )
        fts_enabled = _fts_enabled(connection)
        fts_rows = (
            _capture_fts_rows(connection, sorted(touched_function_ids))
            if fts_enabled
            else []
        )
        result["removed"] = _delete_references(connection, references)

        added = {table: 0 for table in UNIT_TABLES}
        deep_items: list[dict[str, str]] = []
        for item in normalized:
            parse_result = item["parse"]
            writer.add_parse_result(connection, parse_result)
            added["tus"] += 1
            added["functions"] += len(parse_result.functions)
            added["types"] += len(parse_result.types)
            added["fields"] += len(parse_result.fields)
            added["calls"] += len(parse_result.calls)
            if item["include_paths"] is not None:
                writer.add_tu_includes(
                    connection, item["tu_id"], list(item["include_paths"])
                )
            call_targets_supported = _has_table(connection, "call_targets")
            callbacks_supported = _has_table(connection, "callback_registrations")
            for analysis in item["analyses"]:
                analysis_to_write = analysis
                if analysis.call_targets and not call_targets_supported:
                    # Old database without the call_targets table: write the
                    # rest of the analysis and skip the candidate edges.
                    analysis_to_write = dataclasses.replace(
                        analysis, call_targets=[]
                    )
                writer.add_analysis_result(connection, analysis_to_write)
                added["cfg_blocks"] += len(analysis.cfg_blocks)
                added["cfg_edges"] += len(analysis.cfg_edges)
                added["branches"] += len(analysis.branches)
                added["assignments"] += len(analysis.assignments)
                added["returns"] += len(analysis.returns)
                added["resource_ops"] += len(analysis.resource_ops)
                added["findings"] += len(analysis.findings)
                if call_targets_supported:
                    added["call_targets"] += len(analysis.call_targets)
                deep_items.append(
                    {"function_id": str(analysis.function_id), "tu_id": item["tu_id"]}
                )
            if item["callbacks"] and callbacks_supported:
                added["callback_registrations"] += len(item["callbacks"])
                CallbackRegistry(connection).add_callback_registrations(
                    item["callbacks"]
                )
        result["added"] = added

        if fts_enabled:
            result["fts_deleted"] = _fts_delete(connection, fts_rows)
            result["fts_inserted"] = _fts_insert(
                connection, sorted(touched_function_ids)
            )
        if deep_ready and deep_items and _has_table(connection, "deep_status"):
            result["deep_ready"] = store.mark_deep_ready(deep_items)
    return result


def replace_unit(
    store: Any,
    parse_result: Any,
    analyses: Iterable[Any] = (),
    *,
    callbacks: Iterable[Any] = (),
    include_paths: Iterable[str] | None = None,
    deep_ready: bool = True,
) -> dict[str, Any]:
    """Single-TU convenience wrapper around :func:`replace_units`."""

    return replace_units(
        store,
        [
            (
                parse_result,
                tuple(analyses or ()),
                tuple(callbacks or ()),
                include_paths,
            )
        ],
        deep_ready=deep_ready,
    )


__all__ = [
    "UNIT_TABLES",
    "remove_units",
    "replace_unit",
    "replace_units",
]
