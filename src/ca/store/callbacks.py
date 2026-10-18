"""Cross-TU callback / function-pointer registration facts.

A *registration fact* records that a function reference was stored into (or
registered at) an object path: a global variable, a struct field, an array
element, a designated initializer, a registration call such as
``AddCallback(&g_ops, fn)``, an argument pass, a returned pointer or a C++
virtual override. Unlike :class:`ca.core.schema.CallTargetRecord` these facts
are independent of any call site; a later cross-TU pass joins them with
indirect call sites to emit candidate edges.

Records are accepted as mappings or objects exposing the same attributes, so a
future ``ca.core.schema`` dataclass can be passed without changing the store.
Only known fields are persisted; unknown fields are ignored. IDs are stable and
store-local (``cbreg:`` + :func:`ca.core.ids.digest`) unless the producer
supplies one. Re-ingesting the same fact is a no-op (natural-key unique index).

Canonical fields::

    id (optional), object_path (required), object_field (derived),
    target_function_id, target_function_name (one required), target_usr,
    file, line, col, end_line, end_col, tu_id, registration_kind,
    confidence (0..1), evidence (str | list[str]), module, directory
    (derived from file), registrar_function_id

Query results add the decoded ``evidence`` list. ``callback_target_links``
connects registration facts to the candidate ``call_targets`` rows a resolver
produced, so graph/export output can carry the registration evidence.
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any, Iterable, Mapping, Sequence

from ca.core.ids import digest

DEFAULT_CONFIDENCE = 0.5
DEFAULT_KIND = "unknown"
ID_PREFIX = "cbreg:"

COLUMNS = (
    "id",
    "object_path",
    "object_field",
    "target_function_id",
    "target_function_name",
    "target_usr",
    "file",
    "line",
    "col",
    "end_line",
    "end_col",
    "tu_id",
    "registration_kind",
    "confidence",
    "evidence_json",
    "module",
    "directory",
    "registrar_function_id",
)

_INSERT_SQL = (
    f"INSERT OR IGNORE INTO callback_registrations ({', '.join(COLUMNS)}) "
    f"VALUES ({', '.join('?' for _ in COLUMNS)})"
)
_LINK_INSERT_SQL = (
    "INSERT OR IGNORE INTO callback_target_links (registration_id, call_target_id) "
    "VALUES (?, ?)"
)
_SELECT_SQL = f"SELECT {', '.join(COLUMNS)} FROM callback_registrations"


def _get(record: Any, key: str, default: Any = None) -> Any:
    if isinstance(record, Mapping):
        return record.get(key, default)
    return getattr(record, key, default)


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _field_of(object_path: str) -> str:
    """Last component of ``a.b->c[1]``-style paths."""

    path = object_path.rstrip()
    while path.endswith("]"):
        index = path.rfind("[")
        if index <= 0:
            break
        path = path[:index]
    for separator in ("->", "."):
        if separator in path:
            return path.rsplit(separator, 1)[1].strip()
    return path.strip()


def _directory_of(file: str) -> str:
    return os.path.dirname(file)


def _prefix_upper(prefix: str) -> str:
    """Smallest string greater than every string starting with ``prefix``."""

    return prefix[:-1] + chr(ord(prefix[-1]) + 1)


def _encode_evidence(value: Any) -> str:
    if value is None:
        return "[]"
    if isinstance(value, str):
        items = [value] if value else []
    elif isinstance(value, Mapping):
        items = [json.dumps(dict(value), ensure_ascii=False, sort_keys=True)]
    else:
        try:
            items = [str(item) for item in value]
        except TypeError:
            items = [str(value)]
    return json.dumps(items, ensure_ascii=False, separators=(",", ":"))


def _decode_evidence(value: Any) -> list[str]:
    if not value:
        return []
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return [str(value)]
    return [str(item) for item in decoded] if isinstance(decoded, list) else [str(decoded)]


def registration_id(
    *,
    tu_id: str,
    object_path: str,
    target_function_id: str,
    target_function_name: str,
    file: str,
    line: int,
    registration_kind: str,
) -> str:
    return ID_PREFIX + digest(
        tu_id,
        object_path,
        target_function_id,
        target_function_name,
        file,
        str(line),
        registration_kind,
    )


def normalize_record(record: Any) -> dict[str, Any]:
    """Validate and normalize one producer record (mapping or object)."""

    object_path = _text(_get(record, "object_path")).strip()
    if not object_path:
        raise ValueError("callback registration needs a non-empty object_path")
    target_function_id = _text(_get(record, "target_function_id")).strip()
    target_function_name = _text(_get(record, "target_function_name")).strip()
    if not target_function_id and not target_function_name:
        raise ValueError("callback registration needs target_function_id or target_function_name")
    file = _text(_get(record, "file"))
    line = _int(_get(record, "line"))
    kind = _text(_get(record, "registration_kind", DEFAULT_KIND)).strip() or DEFAULT_KIND
    tu_id = _text(_get(record, "tu_id"))
    identifier = _text(_get(record, "id")).strip() or registration_id(
        tu_id=tu_id,
        object_path=object_path,
        target_function_id=target_function_id,
        target_function_name=target_function_name,
        file=file,
        line=line,
        registration_kind=kind,
    )
    try:
        confidence = float(_get(record, "confidence", DEFAULT_CONFIDENCE))
    except (TypeError, ValueError):
        confidence = DEFAULT_CONFIDENCE
    return {
        "id": identifier,
        "object_path": object_path,
        "object_field": _text(_get(record, "object_field")).strip() or _field_of(object_path),
        "target_function_id": target_function_id,
        "target_function_name": target_function_name,
        "target_usr": _text(_get(record, "target_usr")),
        "file": file,
        "line": line,
        "col": _int(_get(record, "col")),
        "end_line": _int(_get(record, "end_line")),
        "end_col": _int(_get(record, "end_col")),
        "tu_id": tu_id,
        "registration_kind": kind,
        "confidence": min(1.0, max(0.0, confidence)),
        "evidence_json": _encode_evidence(_get(record, "evidence")),
        "module": _text(_get(record, "module")),
        "directory": _text(_get(record, "directory")) or _directory_of(file),
        "registrar_function_id": _text(_get(record, "registrar_function_id")),
    }


def _row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["confidence"] = float(data["confidence"])
    data["evidence"] = _decode_evidence(data.pop("evidence_json", None))
    return data


class CallbackRegistry:
    """Batch writes and indexed queries for callback registration facts."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection

    # ------------------------------------------------------------------ #
    # Writes
    # ------------------------------------------------------------------ #
    def add_callback_registrations(self, records: Iterable[Any]) -> int:
        """Insert facts (idempotent by id + natural key); returns rows added."""

        normalized = [normalize_record(record) for record in records]
        if not normalized:
            return 0
        rows = [tuple(item[column] for column in COLUMNS) for item in normalized]
        before = self._conn.total_changes
        self._conn.executemany(_INSERT_SQL, rows)
        return self._conn.total_changes - before

    def link_callback_targets(self, pairs: Iterable[Any]) -> int:
        """Associate registration facts with candidate ``call_targets`` rows."""

        rows: list[tuple[str, str]] = []
        for pair in pairs:
            if isinstance(pair, Mapping):
                registration = _text(pair.get("registration_id"))
                call_target = _text(pair.get("call_target_id"))
            else:
                try:
                    registration, call_target = pair
                except (TypeError, ValueError):
                    raise ValueError("link pairs must be (registration_id, call_target_id)") from None
                registration, call_target = _text(registration), _text(call_target)
            if registration and call_target:
                rows.append((registration, call_target))
        if not rows:
            return 0
        before = self._conn.total_changes
        self._conn.executemany(_LINK_INSERT_SQL, rows)
        return self._conn.total_changes - before

    # ------------------------------------------------------------------ #
    # Queries
    # ------------------------------------------------------------------ #
    def query_callbacks(
        self,
        *,
        object_path: str | None = None,
        path_prefix: str | None = None,
        field: str | None = None,
        module: str | None = None,
        directory: str | None = None,
        tu_id: str | None = None,
        target_function_id: str | None = None,
        target_function_name: str | None = None,
        registration_kind: str | None = None,
        min_confidence: float | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Filtered registration query; all values are bound parameters.

        ``path_prefix`` matches object paths by prefix via an index range scan;
        it cannot be combined with ``object_path``. Ordering is stable:
        ``(object_path, file, line, target_function_name, id)``.
        """

        if limit <= 0:
            return []
        if object_path is not None and path_prefix is not None:
            raise ValueError("pass object_path or path_prefix, not both")
        where: list[str] = []
        params: list[Any] = []
        if object_path is not None:
            where.append("object_path = ?")
            params.append(_text(object_path))
        if path_prefix is not None:
            prefix = _text(path_prefix)
            if prefix:
                where.append("object_path >= ? AND object_path < ?")
                params.extend([prefix, _prefix_upper(prefix)])
        for column, value in (
            ("object_field", field),
            ("module", module),
            ("directory", directory),
            ("tu_id", tu_id),
            ("target_function_id", target_function_id),
            ("target_function_name", target_function_name),
            ("registration_kind", registration_kind),
        ):
            if value is not None:
                where.append(f"{column} = ?")
                params.append(_text(value))
        if min_confidence is not None:
            where.append("confidence >= ?")
            params.append(float(min_confidence))
        sql = _SELECT_SQL
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY object_path, file, line, target_function_name, id LIMIT ? OFFSET ?"
        params.extend([int(limit), max(0, int(offset))])
        return [_row(row) for row in self._conn.execute(sql, params)]

    def query_callbacks_by_path(self, prefix: str, limit: int = 200) -> list[dict[str, Any]]:
        return self.query_callbacks(path_prefix=prefix, limit=limit)

    def query_callbacks_by_field(self, field: str, limit: int = 200) -> list[dict[str, Any]]:
        return self.query_callbacks(field=field, limit=limit)

    def query_callbacks_by_module(self, module: str, limit: int = 200) -> list[dict[str, Any]]:
        return self.query_callbacks(module=module, limit=limit)

    def query_callback_registrations(self, **filters: Any) -> list[dict[str, Any]]:
        """Alias of :meth:`query_callbacks` (name matches the table)."""

        return self.query_callbacks(**filters)

    def get_callback(self, registration_id_value: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            _SELECT_SQL + " WHERE id = ?", (_text(registration_id_value),)
        ).fetchone()
        return _row(row) if row is not None else None

    def count_callbacks(self) -> int:
        return int(
            self._conn.execute("SELECT COUNT(*) FROM callback_registrations").fetchone()[0]
        )

    def callback_stats(self) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT object_path), COUNT(DISTINCT object_field), "
            "COUNT(DISTINCT CASE WHEN target_function_id != '' THEN target_function_id "
            "ELSE 'name:' || target_function_name END), COUNT(DISTINCT module), "
            "COUNT(DISTINCT directory), COUNT(DISTINCT tu_id) FROM callback_registrations"
        ).fetchone()
        by_kind = {
            str(kind): int(count)
            for kind, count in self._conn.execute(
                "SELECT registration_kind, COUNT(*) FROM callback_registrations "
                "GROUP BY registration_kind ORDER BY registration_kind"
            )
        }
        linked = int(
            self._conn.execute("SELECT COUNT(*) FROM callback_target_links").fetchone()[0]
        )
        return {
            "registrations": int(row[0]),
            "object_paths": int(row[1]),
            "object_fields": int(row[2]),
            "targets": int(row[3]),
            "modules": int(row[4]),
            "directories": int(row[5]),
            "tu_ids": int(row[6]),
            "by_kind": by_kind,
            "linked_call_targets": linked,
        }

    # ------------------------------------------------------------------ #
    # Registration <-> call_targets association
    # ------------------------------------------------------------------ #
    def query_callback_targets(
        self, registration_id_value: str, limit: int = 200
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        rows = self._conn.execute(
            "SELECT l.registration_id, l.call_target_id, ct.call_id, ct.caller_id, "
            "ct.callee_id, ct.callee_name, ct.kind, ct.confidence, ct.reason "
            "FROM callback_target_links AS l "
            "JOIN call_targets AS ct ON ct.id = l.call_target_id "
            "WHERE l.registration_id = ? "
            "ORDER BY ct.callee_name, ct.id LIMIT ?",
            (_text(registration_id_value), int(limit)),
        )
        result = []
        for row in rows:
            data = dict(row)
            data["confidence"] = float(data["confidence"])
            result.append(data)
        return result

    def query_registrations_for_target(
        self, call_target_id_value: str, limit: int = 200
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        rows = self._conn.execute(
            "SELECT r.* FROM callback_target_links AS l "
            "JOIN callback_registrations AS r ON r.id = l.registration_id "
            "WHERE l.call_target_id = ? "
            "ORDER BY r.object_path, r.file, r.line, r.id LIMIT ?",
            (_text(call_target_id_value), int(limit)),
        )
        return [_row(row) for row in rows]
