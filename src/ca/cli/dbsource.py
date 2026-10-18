"""Resolve CLI ``--db`` / ``--db-dir`` selectors into one query store.

Single database selectors keep the exact old behaviour (``SqliteStore``); a
directory (or several ``--db`` values) opens a read-only ``FederatedStore`` that
merges rows across shard databases.  Missing/corrupt shard databases are
reported once on stderr and skipped; when no readable shard remains the caller
raises :class:`DatabaseSourceError` so commands can exit with code 1.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Sequence


class DatabaseSourceError(RuntimeError):
    """No readable database matched the selector / federated support missing."""


@dataclass
class QuerySource:
    paths: list[str] = field(default_factory=list)
    federated: bool = False
    skipped: list[dict[str, Any]] = field(default_factory=list)

    @property
    def shard_names(self) -> list[str]:
        return [Path(path).stem for path in self.paths]


def _values(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [part for part in raw.split(",") if part]
    return [str(item) for item in raw if str(item)]


def resolve_paths(db_values: Any, db_dir: str | None) -> tuple[list[str], list[dict[str, Any]]]:
    """Expand ``--db`` values and ``--db-dir`` into a sorted path list."""

    errors: list[dict[str, Any]] = []
    paths: list[str] = []
    for value in _values(db_values):
        path = Path(value).expanduser()
        if not path.is_file():
            errors.append({"path": str(path), "error": "missing database file"})
            continue
        paths.append(str(path.resolve()))
    if db_dir:
        from ca.store.federated import discover_databases

        directory = Path(db_dir).expanduser()
        if not directory.is_dir():
            raise DatabaseSourceError(f"database directory not found: {directory}")
        paths.extend(str(path.resolve()) for path in discover_databases(directory))
    # Preserve caller order for explicit --db values, append directory entries
    # sorted; dedupe by resolved path.
    seen: set[str] = set()
    unique: list[str] = []
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        unique.append(path)
    return unique, errors


def _warn_skipped(skipped: Sequence[dict[str, Any]]) -> None:
    for item in skipped:
        print(
            f"[query] warning: skipping unreadable shard {item.get('path')}:"
            f" {item.get('error')}",
            file=sys.stderr,
        )


@contextmanager
def open_query_store(
    db_values: Any, db_dir: str | None = None
) -> Iterator[tuple[Any, QuerySource]]:
    """Yield ``(store, source)``; single DB keeps ``SqliteStore`` semantics."""

    paths, errors = resolve_paths(db_values, db_dir)
    if not paths:
        raise DatabaseSourceError("no database selected (pass --db or --db-dir)")
    if len(paths) == 1 and not db_dir:
        from ca.store import SqliteStore

        store = SqliteStore(paths[0])
        try:
            store.open()
        except Exception as exc:
            raise DatabaseSourceError(f"cannot open database {paths[0]}: {exc}") from exc
        try:
            yield store, QuerySource(paths=paths, federated=False)
        finally:
            store.close()
        return
    try:
        from ca.store.federated import FederatedStore
    except Exception as exc:  # pragma: no cover - fails only before the API lands
        raise DatabaseSourceError(
            "federated queries are not available in this build"
        ) from exc
    federated = FederatedStore(paths)
    federated.open()
    skipped = list(getattr(federated, "skipped", []) or [])
    skipped.extend(errors)
    shards = list(getattr(federated, "shards", []) or [])
    if not shards:
        federated.close()
        detail = "; ".join(
            f"{item.get('path')}: {item.get('error')}" for item in skipped[:5]
        )
        raise DatabaseSourceError(
            f"no readable shard databases ({detail or 'no *.db files found'})"
        )
    _warn_skipped(skipped)
    try:
        yield federated, QuerySource(
            paths=[str(item.get("path")) for item in shards],
            federated=True,
            skipped=skipped,
        )
    finally:
        federated.close()


def add_shard_columns(
    rows: Sequence[dict[str, Any]], columns: Sequence[str] | None
) -> tuple[list[str] | None, list[dict[str, Any]]]:
    """Add the ``shard`` column for federated rows (additive only)."""

    if not rows or "shard" not in rows[0]:
        return (list(columns) if columns else None), list(rows)
    if columns is None:
        return None, list(rows)
    merged = list(columns)
    if "shard" not in merged:
        merged.append("shard")
    return merged, list(rows)
