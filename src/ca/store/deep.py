"""Per-function deep-analysis status (layered indexing / on-demand deep pass).

The base index (L0: functions/types/calls/symbols) is written by the normal
pipeline; the deep pass adds CFG/data-flow facts for selected functions. This
table records which functions already have deep facts:

- ``ready``: deep facts were written (the analyzer marks it explicitly; a phase-3
  database is backfilled at ``finalize()`` from existing CFG/call_target rows);
- ``failed``: the deep pass ran but failed (``error`` explains why);
- ``pending``: explicitly queued, or *implicit* when no row exists at all — an
  L0-only database therefore reports every function as pending without writing
  one row per function.

Status rows are tiny and mutable; they live next to (not inside) the record
tables so L0 queries never depend on the deep pass. ``stats().tables`` includes
``deep_status``; IDs are the stable ``ca.core.ids`` function ids.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from ca.store.queries import QueryEngine

STATUS_PENDING = "pending"
STATUS_READY = "ready"
STATUS_FAILED = "failed"
STATUSES = (STATUS_PENDING, STATUS_READY, STATUS_FAILED)

_FACTS_EMPTY = "{}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _encode_facts(facts: Any) -> str:
    if not facts:
        return _FACTS_EMPTY
    try:
        return json.dumps(dict(facts), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return _FACTS_EMPTY


def _decode_facts(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _normalize_items(items: Any) -> list[dict[str, Any]]:
    """Accept ``"fn:..."`` / ``[id, ...]`` / ``[{function_id: ...}, ...]``."""

    if isinstance(items, str):
        items = [items]
    normalized: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, Mapping):
            function_id = str(item.get("function_id") or item.get("id") or "")
            if not function_id:
                raise ValueError("deep status item is missing function_id")
            merged = dict(item)
            merged["function_id"] = function_id
            normalized.append(merged)
        else:
            function_id = str(item or "")
            if function_id:
                normalized.append({"function_id": function_id})
    return normalized


def _status_row(row: sqlite3.Row, function: Mapping[str, Any] | None = None) -> dict[str, Any]:
    data = dict(row)
    data["facts"] = _decode_facts(data.pop("facts_json", None))
    data["deep_ready"] = data.get("status") == STATUS_READY
    if function is not None:
        data["name"] = function.get("name", "")
        data["file"] = function.get("file", "")
        data["line"] = function.get("line", 0)
        if not data.get("tu_id"):
            data["tu_id"] = function.get("tu_id", "")
    return data


def _list_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["function_id"] = data.pop("id")
    data["status"] = data.get("status") or STATUS_PENDING
    for column in ("analyzed_at", "tool_version", "source_hash", "error"):
        data[column] = data.get(column) or ""
    data["tu_id"] = data.get("tu_id") or ""
    data["facts"] = _decode_facts(data.pop("facts_json", None))
    data["deep_ready"] = data["status"] == STATUS_READY
    return data


class DeepStatusStore:
    """Deep-analysis status CRUD over an open store connection."""

    def __init__(self, connection: sqlite3.Connection, engine: QueryEngine | None = None) -> None:
        self._conn = connection
        self._engine = engine

    # ------------------------------------------------------------------ #
    # Symbol resolution (status queries accept ids or function names)
    # ------------------------------------------------------------------ #
    def _resolve_ids(self, symbols: Iterable[str], limit: int) -> list[str]:
        resolved: list[str] = []
        seen: set[str] = set()
        for symbol in symbols:
            token = str(symbol or "")
            if not token:
                continue
            candidates = [token]
            if not token.startswith("fn:") and self._engine is not None:
                candidates = self._engine._matching_function_ids(token)
            for function_id in candidates:
                if function_id not in seen:
                    seen.add(function_id)
                    resolved.append(function_id)
                    if len(resolved) >= limit:
                        return resolved
        return resolved

    @staticmethod
    def _write_sql(status: str, clear_error: bool, preserve_facts: bool = True) -> str:
        error_value = "''" if clear_error else "excluded.error"
        facts_value = (
            "COALESCE(NULLIF(excluded.facts_json, '{}'), deep_status.facts_json)"
            if preserve_facts
            else "excluded.facts_json"
        )
        return (
            "INSERT INTO deep_status "
            "(function_id, tu_id, status, analyzed_at, tool_version, source_hash, error, facts_json) "
            "VALUES (?, ?, '" + status + "', ?, ?, ?, ?, ?) "
            "ON CONFLICT(function_id) DO UPDATE SET "
            "tu_id = COALESCE(NULLIF(excluded.tu_id, ''), deep_status.tu_id), "
            "status = excluded.status, "
            "analyzed_at = excluded.analyzed_at, "
            "tool_version = COALESCE(NULLIF(excluded.tool_version, ''), deep_status.tool_version), "
            "source_hash = COALESCE(NULLIF(excluded.source_hash, ''), deep_status.source_hash), "
            "error = " + error_value + ", "
            "facts_json = " + facts_value + ""
        )

    # ------------------------------------------------------------------ #
    # Writes
    # ------------------------------------------------------------------ #
    def mark_deep_ready(self, items: Any) -> int:
        """Mark functions as deeply analyzed; returns rows written."""

        normalized = _normalize_items(items)
        if not normalized:
            return 0
        rows = [
            (
                item["function_id"],
                str(item.get("tu_id", "")),
                str(item.get("analyzed_at") or _now()),
                str(item.get("tool_version", "")),
                str(item.get("source_hash", "")),
                str(item.get("error", "")),
                _encode_facts(item.get("facts")),
            )
            for item in normalized
        ]
        self._conn.executemany(self._write_sql(STATUS_READY, clear_error=True), rows)
        return len(rows)

    def mark_deep_failed(self, items: Any, *, error: str = "") -> int:
        """Mark functions as failed; returns rows written."""

        normalized = _normalize_items(items)
        if not normalized:
            return 0
        rows = [
            (
                item["function_id"],
                str(item.get("tu_id", "")),
                str(item.get("analyzed_at") or _now()),
                str(item.get("tool_version", "")),
                str(item.get("source_hash", "")),
                str(item.get("error", "") or error),
                _encode_facts(item.get("facts")),
            )
            for item in normalized
        ]
        self._conn.executemany(self._write_sql(STATUS_FAILED, clear_error=False), rows)
        return len(rows)

    def mark_deep_pending(self, items: Any, *, tu_id: str = "") -> int:
        """Reset functions to pending (e.g. after a source change)."""

        normalized = _normalize_items(items)
        if not normalized:
            return 0
        rows = [
            (
                item["function_id"],
                str(item.get("tu_id", "") or tu_id),
                "",
                "",
                "",
                "",
                _FACTS_EMPTY,
            )
            for item in normalized
        ]
        self._conn.executemany(
            self._write_sql(STATUS_PENDING, clear_error=True, preserve_facts=False), rows
        )
        return len(rows)

    def invalidate_deep(
        self,
        function_ids: Iterable[str] | None = None,
        tu_ids: Iterable[str] | None = None,
    ) -> int:
        """Drop status rows for functions or whole translation units."""

        if function_ids is None and tu_ids is None:
            raise ValueError("invalidate_deep needs function_ids or tu_ids")
        deleted = 0
        ids = [str(item) for item in (function_ids or []) if item]
        if ids:
            for start in range(0, len(ids), 500):
                chunk = ids[start : start + 500]
                marks = ", ".join("?" for _ in chunk)
                cursor = self._conn.execute(
                    f"DELETE FROM deep_status WHERE function_id IN ({marks})", chunk
                )
                deleted += int(cursor.rowcount or 0)
        tus = [str(item) for item in (tu_ids or []) if item]
        if tus:
            for start in range(0, len(tus), 500):
                chunk = tus[start : start + 500]
                marks = ", ".join("?" for _ in chunk)
                cursor = self._conn.execute(
                    "DELETE FROM deep_status WHERE function_id IN "
                    f"(SELECT id FROM functions WHERE tu_id IN ({marks}))",
                    chunk,
                )
                deleted += int(cursor.rowcount or 0)
        return deleted

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #
    def get_deep_status(self, symbols: Iterable[str], limit: int = 200) -> dict[str, dict[str, Any]]:
        """``function_id -> status dict``; unmarked functions synthesize pending.

        Unknown symbols are omitted. ``symbols`` may mix ``fn:`` ids and function
        names (names are resolved by the usual exact-then-substring search).
        """

        if isinstance(symbols, str):
            symbols = [symbols]
        ids = self._resolve_ids(symbols, limit)
        if not ids:
            return {}
        functions = self._engine.function_summaries(ids) if self._engine else {}
        marks = ", ".join("?" for _ in ids)
        rows = self._conn.execute(
            f"SELECT * FROM deep_status WHERE function_id IN ({marks})", ids
        ).fetchall()
        statuses = {row["function_id"]: row for row in rows}
        result: dict[str, dict[str, Any]] = {}
        for function_id in ids:
            function = functions.get(function_id)
            if function_id not in statuses and function is None:
                continue
            if function_id in statuses:
                row = statuses[function_id]
                data = _status_row(row, None)
                data["function_id"] = function_id
                if function is not None:
                    data["name"] = function["name"]
                    data["file"] = function["file"]
                    data["line"] = function["line"]
                    if not data.get("tu_id"):
                        data["tu_id"] = function.get("tu_id", "")
            else:
                data = {
                    "function_id": function_id,
                    "name": function["name"] if function else "",
                    "file": function["file"] if function else "",
                    "line": function["line"] if function else 0,
                    "tu_id": function.get("tu_id", "") if function else "",
                    "status": STATUS_PENDING,
                    "analyzed_at": "",
                    "tool_version": "",
                    "source_hash": "",
                    "error": "",
                    "facts": {},
                    "deep_ready": False,
                }
            result[function_id] = data
        return result

    def _list_by_statuses(
        self,
        statuses: Sequence[str],
        limit: int | None = None,
        tu_ids: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        if limit is not None and limit <= 0:
            return []
        params: list[Any] = list(statuses)
        marks = ", ".join("?" for _ in statuses)
        where = [f"COALESCE(ds.status, 'pending') IN ({marks})"]
        tus = [str(item) for item in (tu_ids or []) if item]
        if tus:
            where.append(f"f.tu_id IN ({', '.join('?' for _ in tus)})")
            params.extend(tus)
        sql = (
            "SELECT f.id, f.name, f.file, f.line, f.tu_id, ds.status, ds.analyzed_at, "
            "ds.tool_version, ds.source_hash, ds.error, ds.facts_json "
            "FROM functions AS f LEFT JOIN deep_status AS ds ON ds.function_id = f.id "
            f"WHERE {' AND '.join(where)} ORDER BY f.id"
        )
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [_list_row(row) for row in self._conn.execute(sql, params)]

    def list_deep_pending(
        self, limit: int | None = None, tu_ids: Iterable[str] | None = None
    ) -> list[dict[str, Any]]:
        """Functions whose deep facts are missing or failed (implicit pending)."""

        pending = self._list_by_statuses(
            [STATUS_PENDING, STATUS_FAILED], None, tu_ids
        )
        if limit is None:
            return pending
        return pending[: max(0, int(limit))]

    def list_deep_ready(
        self, limit: int | None = None, tu_ids: Iterable[str] | None = None
    ) -> list[dict[str, Any]]:
        return self._list_by_statuses([STATUS_READY], limit, tu_ids)

    def count_deep_status(self) -> dict[str, int]:
        """Counts by status; ``pending`` includes functions without a row.

        Explicit ``pending`` rows are a subset of the implicit ones, so pending
        is derived as ``total_functions - ready - failed`` (the group-by reads
        only the small status table; only ``functions`` is counted).
        """

        rows = self._conn.execute(
            "SELECT status, COUNT(*) FROM deep_status GROUP BY status"
        ).fetchall()
        ready = failed = 0
        for row in rows:
            if row[0] == STATUS_READY:
                ready += int(row[1])
            elif row[0] == STATUS_FAILED:
                failed += int(row[1])
        total_functions = self._conn.execute("SELECT COUNT(*) FROM functions").fetchone()[0]
        total = int(total_functions)
        # Defensive: rows may reference functions removed by an incremental update.
        if ready + failed > total:
            total = ready + failed
        pending = max(0, total - ready - failed)
        return {
            STATUS_PENDING: pending,
            STATUS_READY: ready,
            STATUS_FAILED: failed,
            "total": total,
        }

    def deep_stats(self) -> dict[str, Any]:
        counts = self.count_deep_status()
        total = counts["total"]
        return {
            "total_functions": total,
            "deep_ready": counts["ready"],
            "pending": counts["pending"],
            "failed": counts["failed"],
            "coverage": round(counts["ready"] / total, 4) if total else 0.0,
        }
