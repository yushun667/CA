"""Read-only federated queries across multiple CA shard databases.

A *federated store* answers the same query API as :class:`ca.store.SqliteStore`
against a set of shard databases (e.g. the OH ``oh_S1..S8`` indexes). It is
strictly read-only and never writes to a shard:

- a single in-memory **control connection** attaches every shard through a
  read-only URI (``file:...?mode=ro``) for discovery, validation and schema/meta
  inspection;
- each valid shard then opens its own read-only URI connection and reuses the
  normal :class:`ca.store.queries.QueryEngine`. FTS5 ``MATCH`` cannot be proxied
  through a view/``ATTACH`` alias, so reusing per-shard engines keeps
  arbitrary-substring search index-backed for every shard;
- missing, unreadable or non-CA databases are skipped and recorded in
  :attr:`FederatedStore.skipped` instead of failing the query.

Merge rules (deterministic, contract for the CLI/MCP layer):

- shards are ordered by resolved path; every returned row carries a ``shard``
  key (the database filename stem, uniquified with ``_2`` suffixes);
- duplicates are collapsed by stable ids (functions ``id``, fact ``id``, graph
  node ``id``, graph edge ``(caller_id, callee_id, call_file, call_line,
  source)``, caller edge ``(caller_id, callee_name, call_file, call_line)``,
  call_target ``(call_id, callee_id, kind, callee_name)``, callback ``id``);
  direct call edges win over candidate edges; the first shard in path order wins
  otherwise;
- results are globally sorted with the single-database ordering and ``offset``/
  ``limit`` are applied *after* dedupe and sort, so pagination is exact;
- per-shard queries push down ``offset + limit`` rows (doubling up to three
  times when dedupe underfills) to avoid full scans for selective queries.

Limitations: no cross-shard transactions or writes (the store exposes no write
methods); ``query_function_detail``/``query_call_graph`` merge ``deep_ready`` as
"ready in any shard" because deep-analysis status is shard-local; only
``query_call_graph`` is provided for the merged graph (no path/cycle search
beyond the bounded graph view); a shard containing non-CA tables only (or no
``functions`` table) is skipped.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from ca.core.schema import SCHEMA_VERSION
from ca.store import graph as graph_module
from ca.store import queries
from ca.store.callbacks import CallbackRegistry
from ca.store.queries import QueryEngine

READ_ONLY_ERROR = "FederatedStore is read-only"

# Bounded merge: per-shard fetch width and attempts.
_MAX_FETCH_ROUNDS = 3
_GRAPH_LIMIT = graph_module.MAX_GRAPH_LIMIT


def discover_databases(
    directory: str | os.PathLike[str],
    *,
    pattern: str = "*.db",
    recursive: bool = False,
) -> list[Path]:
    """List candidate shard databases under ``directory`` (sorted by name).

    Sidecar files (``-wal``/``-shm``/``-journal``) and hidden database files are
    excluded. ``FileNotFoundError`` when the directory does not exist; invalid
    databases are filtered later by :class:`FederatedStore`.
    """

    root = Path(directory).expanduser()
    if not root.is_dir():
        raise FileNotFoundError(f"database directory not found: {root}")
    candidates = root.rglob(pattern) if recursive else root.glob(pattern)
    found: list[Path] = []
    for path in candidates:
        if not path.is_file():
            continue
        name = path.name
        if name.startswith(".") or name.endswith(("-wal", "-shm", "-journal")):
            continue
        found.append(path)
    return sorted(found, key=lambda path: (path.name, str(path)))


def _readonly_uri(path: Path, *, immutable: bool = False) -> str:
    suffix = "?mode=ro&immutable=1" if immutable else "?mode=ro"
    return path.resolve().as_uri() + suffix


def _connect_readonly(path: Path) -> sqlite3.Connection:
    """Open a shard strictly read-only (``mode=ro``, immutable fallback)."""

    last_error: Exception | None = None
    for immutable in (False, True):
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                _readonly_uri(path, immutable=immutable), uri=True, timeout=5.0
            )
            connection.row_factory = sqlite3.Row
            connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
            return connection
        except sqlite3.Error as exc:  # pragma: no cover - depends on sqlite build
            last_error = exc
            if connection is not None:
                connection.close()
    raise sqlite3.DatabaseError(str(last_error) if last_error else "cannot open database")


def _inspect(connection: sqlite3.Connection) -> tuple[frozenset[str], str, str]:
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        )
    }
    if "functions" not in tables:
        raise sqlite3.DatabaseError("not a CA index database (functions table missing)")
    schema_version = ""
    store_schema_version = ""
    if "meta" in tables:
        try:
            for key, value in connection.execute("SELECT key, value FROM meta"):
                if key == "schema_version":
                    schema_version = str(value)
                elif key == "store_schema_version":
                    store_schema_version = str(value)
        except sqlite3.Error:
            pass
    return frozenset(tables), schema_version or SCHEMA_VERSION, store_schema_version


def _function_sort_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        0 if row.get("is_definition") else 1,
        row.get("file", ""),
        row.get("line", 0),
        row.get("id", ""),
        row.get("shard", ""),
    )


def _caller_sort_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        row.get("call_file", ""),
        row.get("call_line", 0),
        0 if row.get("source") == "call" else 1,
        row.get("caller_id", ""),
        row.get("callee_name", ""),
        row.get("shard", ""),
    )


def _callee_sort_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        row.get("call_file", ""),
        row.get("call_line", 0),
        0 if row.get("source") == "call" else 1,
        row.get("callee_id", ""),
        row.get("callee_name", ""),
        row.get("shard", ""),
    )


def _fact_sort_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        row.get("file", ""),
        row.get("line", 0),
        row.get("col", 0),
        row.get("id", ""),
        row.get("shard", ""),
    )


def _finding_sort_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (row.get("id", ""), row.get("shard", ""))


def _call_target_sort_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        row.get("call_file", ""),
        row.get("call_line", 0),
        row.get("caller_id", ""),
        row.get("callee_id", ""),
        row.get("kind", ""),
        row.get("shard", ""),
    )


def _callback_sort_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        row.get("object_path", ""),
        row.get("file", ""),
        row.get("line", 0),
        row.get("target_function_name", ""),
        row.get("id", ""),
        row.get("shard", ""),
    )


@dataclass
class _Shard:
    name: str
    path: str
    connection: sqlite3.Connection
    engine: QueryEngine
    tables: frozenset[str]
    schema_version: str
    store_schema_version: str
    db_bytes: int

    def supports(self, *tables: str) -> bool:
        return all(table in self.tables for table in tables)

    def info(self) -> dict[str, Any]:
        return {
            "shard": self.name,
            "path": self.path,
            "db_bytes": self.db_bytes,
            "schema_version": self.schema_version,
            "store_schema_version": self.store_schema_version,
        }

    def close(self) -> None:
        try:
            self.connection.close()
        except sqlite3.Error:
            pass


class FederatedStore:
    """Read-only merge of multiple CA shard databases.

    Construction is cheap; :meth:`open` attaches/validates shards. The store can
    be used as a context manager. Invalid shards are recorded in
    :attr:`skipped`; when no shard is valid every query returns an empty result
    (the CLI maps that to a non-zero exit code).
    """

    def __init__(
        self,
        paths: Sequence[str | os.PathLike[str]] | str | os.PathLike[str] = (),
        *,
        read_only: bool = True,
        shard_names: Sequence[str] | None = None,
    ) -> None:
        if not read_only:
            raise ValueError(READ_ONLY_ERROR)
        self._read_only = True
        self._pending: list[Path] = _coerce_paths(paths)
        self._name_overrides = list(shard_names or [])
        self._control: sqlite3.Connection | None = None
        self._shards: list[_Shard] = []
        self._skipped: list[dict[str, str]] = []
        self._known: set[str] = set()
        self._requested: list[Path] = []
        self._attach_counter = 0
        self._opened = False

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    @classmethod
    def from_directory(
        cls,
        directory: str | os.PathLike[str],
        *,
        pattern: str = "*.db",
        recursive: bool = False,
        read_only: bool = True,
    ) -> "FederatedStore":
        return cls(
            discover_databases(directory, pattern=pattern, recursive=recursive),
            read_only=read_only,
        )

    def _ensure_open(self) -> None:
        if not self._opened:
            self.open()

    def open(self) -> None:
        if self._opened:
            return
        self._control = sqlite3.connect(":memory:", uri=True)
        self._opened = True
        pending, self._pending = self._pending, []
        if not pending and self._requested:
            pending = list(self._requested)
        self.attach_databases(pending)

    def attach_databases(
        self, paths: Sequence[str | os.PathLike[str]] | str | os.PathLike[str]
    ) -> list[dict[str, Any]]:
        """Attach additional shard databases; returns the newly added shard infos."""

        requested = _coerce_paths(paths)
        if not self._opened:
            self._pending.extend(requested)
            return []
        added: list[dict[str, Any]] = []
        for raw in requested:
            self._requested.append(Path(raw))
            path = Path(raw).expanduser()
            try:
                resolved = path.resolve()
            except OSError as exc:
                self._record_skip(str(path), f"{type(exc).__name__}: {exc}")
                continue
            key = str(resolved)
            if key in self._known:
                continue
            self._known.add(key)
            shard = self._attach_one(resolved)
            if shard is not None:
                self._shards.append(shard)
                added.append(shard.info())
        self._shards.sort(key=lambda item: item.path)
        self._skipped.sort(key=lambda item: item["path"])
        return added

    def _attach_one(self, path: Path) -> _Shard | None:
        if not path.is_file():
            self._record_skip(str(path), "missing database file")
            return None
        control = self._control
        assert control is not None
        alias = f"shard_{self._attach_counter}"
        self._attach_counter += 1
        attached = False
        last_error: Exception | None = None
        for immutable in (False, True):
            try:
                # A copied WAL database without -shm can only be attached
                # read-only in immutable mode; try the regular RO URI first.
                control.execute(
                    f"ATTACH DATABASE ? AS {alias}",
                    (_readonly_uri(path, immutable=immutable),),
                )
                control.execute(f"SELECT 1 FROM {alias}.sqlite_master LIMIT 1").fetchone()
                attached = True
                break
            except sqlite3.Error as exc:
                last_error = exc
                _detach_quietly(control, alias)
        if not attached:
            self._record_skip(
                str(path),
                f"{type(last_error).__name__}: {last_error}" if last_error else "cannot attach",
            )
            return None
        try:
            connection = _connect_readonly(path)
            tables, schema_version, store_schema_version = _inspect(connection)
        except (sqlite3.Error, OSError) as exc:
            self._record_skip(str(path), f"{type(exc).__name__}: {exc}")
            _detach_quietly(control, alias)
            return None
        return _Shard(
            name=self._unique_name(path.stem),
            path=str(path),
            connection=connection,
            engine=QueryEngine(connection),
            tables=tables,
            schema_version=schema_version,
            store_schema_version=store_schema_version,
            db_bytes=int(path.stat().st_size) if path.is_file() else 0,
        )

    def _record_skip(self, path: str, error: str) -> None:
        self._skipped.append({"path": path, "error": error})

    def _unique_name(self, stem: str) -> str:
        base = stem or "shard"
        if self._name_overrides:
            index = len(self._shards)
            if index < len(self._name_overrides):
                base = str(self._name_overrides[index]) or base
        name = base
        suffix = 2
        used = {shard.name for shard in self._shards}
        while name in used:
            name = f"{base}_{suffix}"
            suffix += 1
        return name

    def close(self) -> None:
        for shard in self._shards:
            shard.close()
        self._shards = []
        self._skipped = []
        self._known.clear()
        self._attach_counter = 0
        if self._control is not None:
            try:
                self._control.close()
            except sqlite3.Error:
                pass
        self._control = None
        self._opened = False

    def __enter__(self) -> "FederatedStore":
        self.open()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    @property
    def shards(self) -> list[dict[str, Any]]:
        self._ensure_open()
        return [shard.info() for shard in self._shards]

    @property
    def skipped(self) -> list[dict[str, str]]:
        self._ensure_open()
        return [dict(item) for item in self._skipped]

    @property
    def closed(self) -> bool:
        return not self._opened

    # ------------------------------------------------------------------ #
    # Merge helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _merge_rows(
        rows: Iterable[dict[str, Any]],
        *,
        key: Callable[[Mapping[str, Any]], Any],
        sort_key: Callable[[Mapping[str, Any]], Any],
        limit: int,
        offset: int,
        prefer: Callable[[Mapping[str, Any]], Any] | None = None,
    ) -> list[dict[str, Any]]:
        best: dict[Any, dict[str, Any]] = {}
        for row in rows:
            identity = key(row)
            current = best.get(identity)
            if current is None:
                best[identity] = row
            elif prefer is not None and prefer(row) < prefer(current):
                best[identity] = row
        merged = sorted(best.values(), key=sort_key)
        return merged[offset : offset + limit]

    def _shards_for(self, *tables: str) -> list[_Shard]:
        self._ensure_open()
        return [shard for shard in self._shards if shard.supports(*tables)]

    def _collect_paged(
        self,
        fetch: Callable[[_Shard, int], list[dict[str, Any]]],
        *,
        key: Callable[[Mapping[str, Any]], Any],
        sort_key: Callable[[Mapping[str, Any]], Any],
        limit: int,
        offset: int,
        requires: Sequence[str],
        prefer: Callable[[Mapping[str, Any]], Any] | None = None,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        shards = self._shards_for(*requires)
        if not shards:
            return []
        request = max(1, offset + limit)
        merged: list[dict[str, Any]] = []
        for _round in range(_MAX_FETCH_ROUNDS):
            rows: list[dict[str, Any]] = []
            exhausted = True
            for shard in shards:
                try:
                    batch = fetch(shard, request)
                except sqlite3.Error:
                    continue
                if len(batch) >= request:
                    exhausted = False
                for item in batch:
                    row = dict(item)
                    row["shard"] = shard.name
                    rows.append(row)
            merged = self._merge_rows(
                rows, key=key, sort_key=sort_key, limit=limit, offset=offset, prefer=prefer
            )
            if len(merged) >= offset + limit or exhausted:
                return merged
            request *= 2
        return merged

    def _collect_facts(
        self,
        fetch: Callable[[_Shard], list[dict[str, Any]]],
        *,
        key: Callable[[Mapping[str, Any]], Any],
        sort_key: Callable[[Mapping[str, Any]], Any],
        limit: int,
        offset: int,
        requires: Sequence[str],
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        rows: list[dict[str, Any]] = []
        for shard in self._shards_for(*requires):
            try:
                batch = fetch(shard)
            except sqlite3.Error:
                continue
            for item in batch:
                row = dict(item)
                row["shard"] = shard.name
                rows.append(row)
        return self._merge_rows(rows, key=key, sort_key=sort_key, limit=limit, offset=offset)

    # ------------------------------------------------------------------ #
    # Functions / call graph queries
    # ------------------------------------------------------------------ #
    def query_functions(self, pattern: str, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        if not pattern or limit <= 0:
            return []
        shards = self._shards_for("functions")
        if not shards:
            return []
        request = max(1, offset + limit)
        merged: list[dict[str, Any]] = []
        for _round in range(_MAX_FETCH_ROUNDS):
            rows: list[dict[str, Any]] = []
            exhausted = True
            for shard in shards:
                try:
                    batch = shard.engine.query_functions(pattern, request)
                except sqlite3.Error:
                    continue
                if len(batch) >= request:
                    exhausted = False
                for item in batch:
                    row = dict(item)
                    row["shard"] = shard.name
                    rows.append(row)
            exact = [
                row
                for row in rows
                if row.get("id") == pattern
                or row.get("name") == pattern
                or row.get("qualified_name") == pattern
            ]
            if exact:
                # Match single-database semantics: exact hits globally win.
                rows = exact
                exhausted = True
            merged = self._merge_rows(
                rows,
                key=lambda row: row["id"],
                sort_key=_function_sort_key,
                limit=limit,
                offset=offset,
            )
            if len(merged) >= offset + limit or exhausted:
                return merged
            request *= 2
        return merged

    def query_callers(self, symbol: str, limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
        return self._collect_paged(
            lambda shard, request: shard.engine.query_callers(symbol, request),
            key=lambda row: (
                row.get("caller_id"),
                row.get("callee_name"),
                row.get("call_file"),
                row.get("call_line"),
            ),
            sort_key=_caller_sort_key,
            limit=limit,
            offset=offset,
            requires=("calls", "functions"),
            prefer=lambda row: (0 if row.get("source") == "call" else 1, row.get("shard", "")),
        )

    def query_callees(self, symbol: str, limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
        return self._collect_paged(
            lambda shard, request: shard.engine.query_callees(symbol, request),
            key=lambda row: (
                row.get("callee_id"),
                row.get("callee_name"),
                row.get("call_file"),
                row.get("call_line"),
            ),
            sort_key=_callee_sort_key,
            limit=limit,
            offset=offset,
            requires=("calls", "functions"),
            prefer=lambda row: (0 if row.get("source") == "call" else 1, row.get("shard", "")),
        )

    def query_call_targets(
        self, symbol: str, direction: str = "callee", limit: int = 200, offset: int = 0
    ) -> list[dict[str, Any]]:
        return self._collect_paged(
            lambda shard, request: shard.engine.query_call_targets(symbol, direction, request),
            key=lambda row: (
                row.get("call_id"),
                row.get("callee_id"),
                row.get("kind"),
                row.get("callee_name"),
            ),
            sort_key=_call_target_sort_key,
            limit=limit,
            offset=offset,
            requires=("call_targets",),
        )

    def query_findings(
        self, kind: str | None = None, limit: int = 200, offset: int = 0
    ) -> list[dict[str, Any]]:
        return self._collect_paged(
            lambda shard, request: shard.engine.query_findings(kind, limit=request),
            key=lambda row: row.get("id"),
            sort_key=_finding_sort_key,
            limit=limit,
            offset=offset,
            requires=("findings",),
        )

    # ------------------------------------------------------------------ #
    # Per-function fact queries
    # ------------------------------------------------------------------ #
    def query_branches(self, symbol: str, limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
        return self._collect_facts(
            lambda shard: shard.engine.query_branches(symbol),
            key=lambda row: row.get("id"),
            sort_key=_fact_sort_key,
            limit=limit,
            offset=offset,
            requires=("branches", "functions"),
        )

    def query_assignments(self, symbol: str, limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
        return self._collect_facts(
            lambda shard: shard.engine.query_assignments(symbol),
            key=lambda row: row.get("id"),
            sort_key=_fact_sort_key,
            limit=limit,
            offset=offset,
            requires=("assignments", "functions"),
        )

    def query_returns(self, symbol: str, limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
        return self._collect_facts(
            lambda shard: shard.engine.query_returns(symbol),
            key=lambda row: row.get("id"),
            sort_key=_fact_sort_key,
            limit=limit,
            offset=offset,
            requires=("returns", "functions"),
        )

    def query_resource_ops(self, symbol: str, limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
        return self._collect_facts(
            lambda shard: shard.engine.query_resource_ops(symbol),
            key=lambda row: row.get("id"),
            sort_key=_fact_sort_key,
            limit=limit,
            offset=offset,
            requires=("resource_ops", "functions"),
        )

    # ------------------------------------------------------------------ #
    # Callback registrations
    # ------------------------------------------------------------------ #
    def query_callbacks(self, limit: int = 200, offset: int = 0, **filters: Any) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        shards = self._shards_for("callback_registrations")
        if not shards:
            return []
        request = max(1, offset + limit)
        merged: list[dict[str, Any]] = []
        for _round in range(_MAX_FETCH_ROUNDS):
            rows: list[dict[str, Any]] = []
            exhausted = True
            for shard in shards:
                try:
                    batch = CallbackRegistry(shard.connection).query_callbacks(
                        limit=request, offset=0, **filters
                    )
                except (sqlite3.Error, ValueError):
                    continue
                if len(batch) >= request:
                    exhausted = False
                for item in batch:
                    row = dict(item)
                    row["shard"] = shard.name
                    rows.append(row)
            merged = self._merge_rows(
                rows,
                key=lambda row: row["id"],
                sort_key=_callback_sort_key,
                limit=limit,
                offset=offset,
            )
            if len(merged) >= offset + limit or exhausted:
                return merged
            request *= 2
        return merged

    def callback_stats(self) -> dict[str, Any]:
        totals = {
            "registrations": 0,
            "object_paths": 0,
            "object_fields": 0,
            "targets": 0,
            "modules": 0,
            "directories": 0,
            "tu_ids": 0,
            "by_kind": {},
            "linked_call_targets": 0,
        }
        shard_stats: list[dict[str, Any]] = []
        for shard in self._shards_for("callback_registrations"):
            try:
                stats = CallbackRegistry(shard.connection).callback_stats()
            except sqlite3.Error:
                continue
            for key in (
                "registrations",
                "object_paths",
                "object_fields",
                "targets",
                "modules",
                "directories",
                "tu_ids",
                "linked_call_targets",
            ):
                totals[key] += int(stats.get(key, 0))
            for kind, count in (stats.get("by_kind") or {}).items():
                totals["by_kind"][kind] = totals["by_kind"].get(kind, 0) + int(count)
            shard_stats.append({"shard": shard.name, **stats})
        return {**totals, "shard_count": len(shard_stats), "shards": shard_stats}

    # ------------------------------------------------------------------ #
    # Detail
    # ------------------------------------------------------------------ #
    def query_function_detail(self, symbol: str) -> dict[str, Any]:
        candidates: list[tuple[_Shard, dict[str, Any]]] = []
        for shard in self._shards_for("functions"):
            try:
                detail = shard.engine.query_function_detail(symbol)
            except sqlite3.Error:
                continue
            if detail.get("function"):
                candidates.append((shard, detail))
        if not candidates:
            return {
                "function": None,
                "deep_ready": False,
                "shard": None,
                "branches": [],
                "assignments": [],
                "returns": [],
                "resource_ops": [],
                "findings": [],
            }
        chosen = min(
            candidates,
            key=lambda item: (
                0 if item[1]["function"].get("is_definition") else 1,
                item[0].name,
                item[1]["function"].get("file", ""),
                item[1]["function"].get("line", 0),
                item[1]["function"].get("id", ""),
            ),
        )
        function = dict(chosen[1]["function"])
        function["shard"] = chosen[0].name
        sections: dict[str, list[dict[str, Any]]] = {}
        for section in ("branches", "assignments", "returns", "resource_ops", "findings"):
            rows: list[dict[str, Any]] = []
            for shard, detail in candidates:
                for item in detail.get(section) or []:
                    row = dict(item)
                    row["shard"] = shard.name
                    rows.append(row)
            sort_key = _finding_sort_key if section == "findings" else _fact_sort_key
            sections[section] = self._merge_rows(
                rows,
                key=lambda row: row.get("id"),
                sort_key=sort_key,
                limit=len(rows) or 1,
                offset=0,
            )
        return {
            "function": function,
            "deep_ready": any(detail.get("deep_ready") for _, detail in candidates),
            "shard": chosen[0].name,
            **sections,
        }

    # ------------------------------------------------------------------ #
    # Cross-shard call graph
    # ------------------------------------------------------------------ #
    def query_cfg(
        self, symbol: str, max_blocks: int = 500, max_edges: int = 1000
    ) -> dict[str, Any]:
        """Merged read-only CFG for ``symbol`` across shards (shard-tagged)."""

        self._ensure_open()
        blocks: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        branches: list[dict[str, Any]] = []
        function_ids: list[str] = []
        truncated = False
        for shard in self._shards_for("cfg_blocks", "cfg_edges", "branches"):
            try:
                cfg = shard.engine.query_cfg(
                    symbol, max_blocks=max_blocks, max_edges=max_edges
                )
            except sqlite3.Error:
                continue
            truncated = truncated or bool(cfg.get("truncated"))
            for item in cfg.get("function_ids") or []:
                if item not in function_ids:
                    function_ids.append(item)
            for key, bucket in (
                ("blocks", blocks),
                ("edges", edges),
                ("branches", branches),
            ):
                for row in cfg.get(key) or []:
                    item = dict(row)
                    item["shard"] = shard.name
                    bucket.append(item)
        def _dedupe(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            seen: set[Any] = set()
            unique: list[dict[str, Any]] = []
            for row in rows:
                key = row.get("id")
                if key in seen:
                    continue
                seen.add(key)
                unique.append(row)
            return unique

        blocks = _dedupe(blocks)
        edges = _dedupe(edges)
        branches = _dedupe(branches)
        if len(blocks) > max_blocks:
            blocks = blocks[:max_blocks]
            truncated = True
        if len(edges) > max_edges:
            edges = edges[:max_edges]
            truncated = True
        return {
            "function_ids": function_ids,
            "blocks": blocks,
            "edges": edges,
            "branches": branches,
            "truncated": truncated,
        }

    def query_call_graph(
        self,
        symbol: str,
        direction: str = "both",
        depth: int = 1,
        limit: int = 500,
    ) -> dict[str, Any]:
        self._ensure_open()
        if direction not in ("forward", "reverse", "both"):
            raise ValueError("direction must be 'forward', 'reverse' or 'both'")
        if isinstance(depth, bool) or not isinstance(depth, int) or depth < 0:
            raise ValueError("depth must be a non-negative integer")
        if limit < 0:
            raise ValueError("limit must be non-negative")
        root_rows = self.query_functions(symbol, limit=1)
        empty = {
            "root": None,
            "deep_ready": False,
            "direction": direction,
            "depth": depth,
            "nodes": [],
            "edges": [],
            "cycles": [],
            "truncated": False,
        }
        if not root_rows or limit == 0:
            return empty
        root_row = root_rows[0]
        root_id = str(root_row["id"])
        cap = min(limit, _GRAPH_LIMIT)
        shards = self._shards_for("calls", "functions")
        node_meta: dict[str, dict[str, Any]] = {
            root_id: {"depth": 0, "direction": "root", "shard": root_row["shard"]}
        }
        node_ids: set[str] = {root_id}
        edges: list[dict[str, Any]] = []
        seen_edges: set[tuple[Any, ...]] = set()
        truncated = False
        walks = ("forward", "reverse") if direction == "both" else (direction,)
        for walk in walks:
            visited = {root_id}
            frontier = [root_id]
            for level in range(1, depth + 1):
                budget = cap - len(edges)
                if budget <= 0:
                    truncated = True
                    break
                next_frontier: list[str] = []
                for shard in shards:
                    try:
                        if walk == "forward":
                            batch = shard.engine.load_forward_edges(frontier, budget)
                        else:
                            batch = shard.engine.load_reverse_edges(frontier, budget)
                    except sqlite3.Error:
                        continue
                    for edge in batch:
                        if len(edges) >= cap:
                            truncated = True
                            break
                        key = (
                            edge["caller_id"],
                            edge["callee_id"],
                            edge["call_file"],
                            edge["call_line"],
                            edge["source"],
                        )
                        if key in seen_edges:
                            continue
                        seen_edges.add(key)
                        neighbor = (
                            edge["callee_id"] if walk == "forward" else edge["caller_id"]
                        )
                        if not neighbor:
                            continue
                        item = dict(edge)
                        item["shard"] = shard.name
                        item["depth"] = level
                        item["direction"] = walk
                        edges.append(item)
                        node_ids.add(neighbor)
                        if neighbor not in node_meta:
                            node_meta[neighbor] = {"depth": level, "direction": walk}
                        if neighbor not in visited:
                            visited.add(neighbor)
                            next_frontier.append(neighbor)
                if len(edges) >= cap:
                    truncated = True
                if not next_frontier:
                    break
                frontier = next_frontier
        # Node info + deep_ready across shards (first shard in path order wins).
        info: dict[str, dict[str, Any]] = {}
        ready: dict[str, bool] = {node_id: False for node_id in node_ids}
        for shard in shards:
            try:
                summaries = shard.engine.function_summaries(sorted(node_ids))
            except sqlite3.Error:
                continue
            for node_id, summary in summaries.items():
                if node_id not in info:
                    summary = dict(summary)
                    summary["shard"] = shard.name
                    info[node_id] = summary
            try:
                for node_id, is_ready in shard.engine.deep_ready_map(
                    sorted(node_ids)
                ).items():
                    if is_ready:
                        ready[node_id] = True
            except sqlite3.Error:
                pass
        nodes: list[dict[str, Any]] = []
        for node_id, meta in node_meta.items():
            summary = info.get(node_id) or {}
            edge_name = next(
                (
                    edge["callee_name"]
                    if edge["callee_id"] == node_id
                    else edge["caller_name"]
                    for edge in edges
                    if node_id in (edge["callee_id"], edge["caller_id"])
                ),
                "",
            )
            nodes.append(
                {
                    "id": node_id,
                    "name": summary.get("name", "") or edge_name,
                    "file": summary.get("file", ""),
                    "line": summary.get("line", 0),
                    "depth": meta["depth"],
                    "direction": meta["direction"],
                    "deep_ready": ready.get(node_id, False),
                    "shard": summary.get("shard") or meta.get("shard") or root_row["shard"],
                }
            )
        nodes.sort(key=lambda node: (node["depth"], node["id"]))
        edges.sort(
            key=lambda edge: (
                edge["depth"],
                edge["call_file"],
                edge["call_line"],
                edge["caller_id"],
                edge["callee_id"],
                edge["source"],
                edge["shard"],
            )
        )
        root_payload = {
            "id": root_id,
            "name": root_row.get("name", ""),
            "file": root_row.get("file", ""),
            "line": root_row.get("line", 0),
            "deep_ready": ready.get(root_id, False),
            "shard": root_row["shard"],
        }
        return {
            "root": root_payload,
            "deep_ready": ready.get(root_id, False),
            "direction": direction,
            "depth": depth,
            "nodes": nodes,
            "edges": edges,
            "cycles": graph_module.CallGraph._cycles_from_edges(edges, limit=20),
            "truncated": truncated,
        }

    # ------------------------------------------------------------------ #
    # Aggregated stats
    # ------------------------------------------------------------------ #
    def stats(self) -> dict[str, Any]:
        self._ensure_open()
        totals = {table: 0 for table in queries.TABLES}
        shard_entries: list[dict[str, Any]] = []
        for shard in self._shards:
            counts: dict[str, int] = {}
            for table in queries.TABLES:
                if table not in shard.tables:
                    continue
                try:
                    counts[table] = int(
                        shard.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    )
                except sqlite3.Error:
                    continue
                totals[table] += counts[table]
            shard_entries.append(
                {
                    "shard": shard.name,
                    "path": shard.path,
                    "ok": True,
                    "db_bytes": shard.db_bytes,
                    "schema_version": shard.schema_version,
                    "store_schema_version": shard.store_schema_version,
                    "tables": counts,
                }
            )
        return {
            "tables": totals,
            "db_bytes": sum(shard.db_bytes for shard in self._shards),
            "schema_version": SCHEMA_VERSION,
            "sqlite_version": sqlite3.sqlite_version,
            "shard_count": len(self._shards),
            "shards": shard_entries,
            "skipped": self.skipped,
        }


def _detach_quietly(connection: sqlite3.Connection, alias: str) -> None:
    try:
        connection.execute(f"DETACH DATABASE {alias}")
    except sqlite3.Error:
        pass


def _coerce_paths(paths: Sequence[str | os.PathLike[str]] | str | os.PathLike[str]) -> list[Path]:
    if isinstance(paths, (str, os.PathLike)):
        return [Path(paths)]
    return [Path(item) for item in paths]
