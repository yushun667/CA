"""Local read-only web UI for CA: JSON API + single-page frontend.

``ca ui --db DB`` (or ``--db-dir DIR``) serves a browser UI on loopback. The
HTTP API is a thin adapter over the same store query module the CLI and MCP
interface use (:mod:`ca.store.queries`, :mod:`ca.store.federated`); the
frontend under ``ca/ui/static`` is plain HTML/CSS/JS with no CDN, Node build
step or third-party dependency.

Read-only guarantee
-------------------

Every request opens a fresh read-only SQLite connection: one database through
:class:`_ReadOnlySqliteStore` (``PRAGMA query_only=ON`` makes any accidental
write fail), several shards through :class:`ca.store.federated.FederatedStore`
(per-shard ``mode=ro`` attachments). The UI deliberately does **not** use
``ca.cli.dbsource.open_query_store`` for single databases because that helper
opens a read-write :class:`ca.store.SqliteStore`, whose ``open()`` applies DDL
and meta upserts; the UI must never mutate an index. Database discovery
(``--db``/``--db-dir`` expansion) is still reused from
:func:`ca.cli.dbsource.resolve_paths`.

There are no write endpoints: every non-GET method returns ``405`` with the
standard error envelope.

Safety
------

The default bind address is ``127.0.0.1``. The UI has no authentication and is
meant for one analyst on a workstation (or a local SSH tunnel), never a public
network. Static files come from a fixed whitelist, so request paths never touch
the filesystem directly.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator, Sequence
from urllib.parse import parse_qs, unquote, urlsplit

from ca.cli.dbsource import DatabaseSourceError, resolve_paths
from ca.cli.graph import GraphError, build_call_graph
from ca.core.schema import TOOL_VERSION

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8766
DEFAULT_LIMIT = 50
MAX_LIMIT = 5000
PROXY_TIMEOUT = 30.0
PROXY_SSE_IDLE_TIMEOUT = 60.0
PROXY_MAX_BODY = 1024 * 1024
PROXY_POST_PATHS = ("/api/jobs",)
DEFAULT_SOURCE_CONTEXT = 3
MAX_SOURCE_CONTEXT = 10
MAX_SOURCE_LINES = 400
STATIC_DIR = Path(__file__).with_name("static")
STATIC_FILES = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "i18n.js": "text/javascript; charset=utf-8",
    "highlight.js": "text/javascript; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
    "favicon.svg": "image/svg+xml",
}
FACT_ENDPOINTS = {
    "/api/branches": "query_branches",
    "/api/assignments": "query_assignments",
    "/api/returns": "query_returns",
    "/api/resources": "query_resource_ops",
}
_CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'"
)


class ApiError(Exception):
    """An API request that should become a JSON error envelope."""

    def __init__(
        self,
        code: str,
        message: str,
        status: HTTPStatus = HTTPStatus.BAD_REQUEST,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


# --------------------------------------------------------------------------- #
# Read-only store access
# --------------------------------------------------------------------------- #


def _connect_read_only(path: str) -> sqlite3.Connection:
    """Open ``path`` with a connection that rejects every write.

    ``PRAGMA query_only=ON`` is enforced by SQLite itself, so a programming
    mistake in a query path cannot mutate the index even though the file was
    opened with the default (read-write capable) flags.
    """

    connection = sqlite3.connect(str(Path(path).expanduser()), timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


class _ReadOnlySqliteStore:
    """Minimal read-only facade over the query engines of one database."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._connection: sqlite3.Connection | None = None
        self._queries: Any = None
        self._graph: Any = None
        self._callbacks: Any = None

    def open(self) -> None:
        if self._connection is not None:
            return
        from ca.store.callbacks import CallbackRegistry
        from ca.store.graph import CallGraph
        from ca.store.queries import QueryEngine

        connection = _connect_read_only(self.path)
        self._connection = connection
        self._queries = QueryEngine(connection)
        self._graph = CallGraph(self._queries)
        self._callbacks = CallbackRegistry(connection)

    def close(self) -> None:
        if self._connection is None:
            return
        try:
            self._connection.close()
        finally:
            self._connection = None
            self._queries = None
            self._graph = None
            self._callbacks = None

    # Query API used by the HTTP layer (mirrors the SqliteStore method names).

    def query_functions(self, pattern: str, limit: int = 50) -> list[dict[str, Any]]:
        return self._queries.query_functions(pattern, limit)

    def query_callers(self, symbol: str, limit: int = 200) -> list[dict[str, Any]]:
        return self._queries.query_callers(symbol, limit)

    def query_callees(self, symbol: str, limit: int = 200) -> list[dict[str, Any]]:
        return self._queries.query_callees(symbol, limit)

    def query_function_detail(self, symbol: str) -> dict[str, Any]:
        return self._queries.query_function_detail(symbol)

    def query_branches(self, symbol: str) -> list[dict[str, Any]]:
        return self._queries.query_branches(symbol)

    def query_assignments(self, symbol: str) -> list[dict[str, Any]]:
        return self._queries.query_assignments(symbol)

    def query_returns(self, symbol: str) -> list[dict[str, Any]]:
        return self._queries.query_returns(symbol)

    def query_resource_ops(self, symbol: str) -> list[dict[str, Any]]:
        return self._queries.query_resource_ops(symbol)

    def query_findings(
        self, kind: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        return self._queries.query_findings(kind=kind, limit=limit)

    def query_cfg(
        self, symbol: str, max_blocks: int = 500, max_edges: int = 1000
    ) -> dict[str, Any]:
        return self._queries.query_cfg(symbol, max_blocks, max_edges)

    def query_call_graph(
        self,
        symbol: str,
        direction: str = "both",
        depth: int = 1,
        limit: int = 500,
    ) -> dict[str, Any]:
        return self._graph.query_call_graph(symbol, direction, depth, limit)

    def query_callbacks(self, **filters: Any) -> list[dict[str, Any]]:
        return self._callbacks.query_callbacks(**filters)

    def callback_stats(self) -> dict[str, Any]:
        return self._callbacks.callback_stats()

    def stats(self) -> dict[str, Any]:
        return self._queries.stats()


class QueryService:
    """Resolve a selector into read-only stores for request handlers."""

    def __init__(
        self,
        paths: Sequence[str],
        *,
        federated: bool = False,
        skipped: Sequence[dict[str, Any]] = (),
    ) -> None:
        if not paths:
            raise DatabaseSourceError("no database selected (pass --db or --db-dir)")
        self.paths = [str(path) for path in paths]
        self.federated = bool(federated or len(self.paths) > 1)
        self.skipped = [dict(item) for item in skipped]
        self.last_skipped = list(self.skipped)

    @contextmanager
    def open(self) -> Iterator[Any]:
        """Yield a read-only store (single database or federated shards)."""

        if self.federated:
            from ca.store.federated import FederatedStore

            store = FederatedStore(list(self.paths))
            store.open()
            try:
                shards = list(getattr(store, "shards", []) or [])
                if not shards:
                    skipped = list(getattr(store, "skipped", []) or []) + self.skipped
                    detail = "; ".join(
                        f"{item.get('path')}: {item.get('error')}"
                        for item in skipped[:3]
                    )
                    raise DatabaseSourceError(
                        "no readable shard databases"
                        + (f" ({detail})" if detail else "")
                    )
                self.last_skipped = (
                    list(getattr(store, "skipped", []) or []) + self.skipped
                )
                yield store
            finally:
                store.close()
        else:
            store = _ReadOnlySqliteStore(self.paths[0])
            store.open()
            try:
                yield store
            finally:
                store.close()

    def db_path_for_shard(self, shard: str) -> str | None:
        """Resolve a federated shard name (database filename stem) to its path."""

        if not shard:
            return None
        for path in self.paths:
            if Path(path).stem == shard:
                return path
        return None

    def describe(self) -> dict[str, Any]:
        """Selector description for ``/api/health`` and ``/api/stats``."""

        return {
            "paths": list(self.paths),
            "federated": self.federated,
            "shards": [Path(path).stem for path in self.paths],
            "skipped": list(self.last_skipped),
        }


def resolve_service(
    db_values: Any, db_dir: str | None = None
) -> QueryService:
    """Expand ``--db``/``--db-dir`` into a :class:`QueryService` (read-only)."""

    paths, errors = resolve_paths(db_values, db_dir)
    if not paths:
        detail = "; ".join(
            f"{item.get('path')}: {item.get('error')}" for item in errors[:3]
        )
        if detail:
            raise DatabaseSourceError(f"no readable database ({detail})")
        if db_dir:
            raise DatabaseSourceError(f"no shard databases found in {db_dir}")
        raise DatabaseSourceError("no database selected (pass --db or --db-dir)")
    return QueryService(
        paths,
        federated=bool(db_dir) or len(paths) > 1,
        skipped=errors,
    )


# --------------------------------------------------------------------------- #
# Query parameters + API implementations
# --------------------------------------------------------------------------- #


def _params(query: str) -> dict[str, str]:
    return {
        key: values[0] for key, values in parse_qs(query, keep_blank_values=True).items()
    }


def _int_param(
    params: dict[str, str],
    name: str,
    default: int,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    raw = params.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ApiError(
            "INVALID_ARGUMENT", f"parameter {name!r} must be an integer"
        ) from None
    value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _symbol(params: dict[str, str]) -> str:
    symbol = (
        params.get("symbol")
        or params.get("name")
        or params.get("id")
        or params.get("q")
        or ""
    ).strip()
    if not symbol:
        raise ApiError("INVALID_ARGUMENT", "parameter 'symbol' is required")
    return symbol


def api_search(service: QueryService, params: dict[str, str]) -> dict[str, Any]:
    pattern = (params.get("q") or params.get("pattern") or "").strip()
    if not pattern:
        raise ApiError("INVALID_ARGUMENT", "query parameter 'q' is required")
    limit = _int_param(params, "limit", DEFAULT_LIMIT, minimum=1, maximum=500)
    offset = _int_param(params, "offset", 0, minimum=0)
    with service.open() as store:
        rows = list(store.query_functions(pattern, limit=limit + offset))
    items = rows[offset : offset + limit]
    return {
        "query": pattern,
        "items": items,
        "count": len(items),
        "limit": limit,
        "offset": offset,
        "federated": service.federated,
    }


def _edge_rows(
    service: QueryService, params: dict[str, str], method: str
) -> dict[str, Any]:
    symbol = _symbol(params)
    limit = _int_param(params, "limit", 200, minimum=1, maximum=2000)
    offset = _int_param(params, "offset", 0, minimum=0)
    with service.open() as store:
        rows = list(getattr(store, method)(symbol, limit=limit + offset))
    items = rows[offset : offset + limit]
    return {
        "symbol": symbol,
        "items": items,
        "count": len(items),
        "limit": limit,
        "offset": offset,
        "federated": service.federated,
    }


def api_callers(service: QueryService, params: dict[str, str]) -> dict[str, Any]:
    return _edge_rows(service, params, "query_callers")


def api_callees(service: QueryService, params: dict[str, str]) -> dict[str, Any]:
    return _edge_rows(service, params, "query_callees")


def api_detail(service: QueryService, symbol: str) -> dict[str, Any]:
    symbol = (symbol or "").strip()
    if not symbol:
        raise ApiError("INVALID_ARGUMENT", "a function name or id is required")
    with service.open() as store:
        detail = dict(store.query_function_detail(symbol))
    function = detail.get("function")
    if not function:
        raise ApiError(
            "NOT_FOUND",
            f"no function matching {symbol!r}",
            HTTPStatus.NOT_FOUND,
        )
    if service.federated:
        detail["federated"] = True
        detail["shard"] = detail.get("shard") or function.get("shard") or ""
    return detail


def api_graph(service: QueryService, params: dict[str, str]) -> dict[str, Any]:
    symbol = _symbol(params)
    direction = (params.get("direction") or "both").strip().lower()
    direction = {
        "callee": "forward",
        "callees": "forward",
        "out": "forward",
        "caller": "reverse",
        "callers": "reverse",
        "in": "reverse",
    }.get(direction, direction)
    depth = _int_param(params, "depth", 1, minimum=0, maximum=12)
    limit = _int_param(params, "limit", 500, minimum=1, maximum=MAX_LIMIT)
    with service.open() as store:
        graph = build_call_graph(
            store, symbol, direction=direction, depth=depth, limit=limit
        )
    graph["federated"] = service.federated
    graph["found"] = bool(graph.get("nodes"))
    return graph


def api_cfg(service: QueryService, params: dict[str, str]) -> dict[str, Any]:
    """Read-only control-flow graph (blocks/edges) with branch annotations."""

    symbol = _symbol(params)
    with service.open() as store:
        detail = dict(store.query_function_detail(symbol))
        function = detail.get("function")
        if not function:
            raise ApiError(
                "NOT_FOUND",
                f"no function matching {symbol!r}",
                HTTPStatus.NOT_FOUND,
            )
        cfg = dict(store.query_cfg(symbol))
        deep_ready = bool(detail.get("deep_ready"))
        shard = str(detail.get("shard") or function.get("shard") or "")
    blocks = list(cfg.get("blocks") or [])
    edges = list(cfg.get("edges") or [])
    branches = list(cfg.get("branches") or [])
    return {
        "symbol": symbol,
        "function": {
            "id": str(function.get("id") or ""),
            "name": str(function.get("name") or ""),
            "qualified_name": str(function.get("qualified_name") or ""),
            "kind": str(function.get("kind") or ""),
            "file": str(function.get("file") or ""),
            "line": int(function.get("line") or 0),
            "end_line": int(function.get("end_line") or 0),
        },
        "deep_ready": deep_ready,
        "shard": shard or None,
        "federated": service.federated,
        "blocks": blocks,
        "edges": edges,
        "branches": branches,
        "truncated": bool(cfg.get("truncated")),
        "stats": {
            "blocks": len(blocks),
            "edges": len(edges),
            "branches": len(branches),
        },
    }


def api_facts(
    service: QueryService, params: dict[str, str], method: str
) -> dict[str, Any]:
    symbol = _symbol(params)
    limit = _int_param(params, "limit", 500, minimum=1, maximum=MAX_LIMIT)
    with service.open() as store:
        rows = list(getattr(store, method)(symbol))
    items = rows[:limit]
    return {
        "symbol": symbol,
        "items": items,
        "count": len(items),
        "limit": limit,
        "offset": 0,
        "federated": service.federated,
    }


def api_stats(service: QueryService) -> dict[str, Any]:
    with service.open() as store:
        data = dict(store.stats())
        callback_stats = getattr(store, "callback_stats", None)
        if callable(callback_stats):
            try:
                data["callbacks"] = dict(callback_stats())
            except Exception:  # additive: a missing callback table must not break stats
                pass
    data["federated"] = service.federated
    data["database"] = service.describe()
    return data


def api_callbacks(service: QueryService, params: dict[str, str]) -> dict[str, Any]:
    limit = _int_param(params, "limit", 200, minimum=1, maximum=1000)
    offset = _int_param(params, "offset", 0, minimum=0)
    filters: dict[str, Any] = {}
    prefix = (params.get("path") or params.get("prefix") or "").strip()
    if prefix:
        filters["path_prefix"] = prefix
    object_path = (params.get("object_path") or "").strip()
    if object_path:
        filters["object_path"] = object_path
    for name, column in (
        ("field", "field"),
        ("module", "module"),
        ("kind", "registration_kind"),
        ("directory", "directory"),
    ):
        value = (params.get(name) or "").strip()
        if value:
            filters[column] = value
    with service.open() as store:
        rows = list(store.query_callbacks(limit=limit + offset, offset=0, **filters))
    items = rows[offset : offset + limit]
    return {
        "items": items,
        "count": len(items),
        "limit": limit,
        "offset": offset,
        "filters": filters,
        "federated": service.federated,
    }


def _read_tu_row(db_path: str, tu_id: str) -> dict[str, Any] | None:
    """Read the ``tus`` provenance row that locates a function's source file."""

    if not tu_id:
        return None
    try:
        connection = _connect_read_only(db_path)
    except (sqlite3.Error, OSError):
        return None
    try:
        row = connection.execute(
            "SELECT id, file, directory, repo_root, language FROM tus WHERE id = ?",
            (tu_id,),
        ).fetchone()
        return dict(row) if row is not None else None
    except sqlite3.Error:
        return None
    finally:
        connection.close()


def _source_payload(
    service: QueryService, symbol: str, context: int
) -> dict[str, Any]:
    """Return a read-only source window around a function's recorded location.

    The symbol resolves through the normal detail query; the repository root
    comes from the ``tus`` row of the owning database (federated: the shard that
    holds the function). The resolved real path must stay inside that root, so a
    crafted ``..``/absolute path in the index cannot escape the repository.
    """

    with service.open() as store:
        detail = dict(store.query_function_detail(symbol))
        function = detail.get("function")
        if not function:
            raise ApiError(
                "NOT_FOUND",
                f"no function matching {symbol!r}",
                HTTPStatus.NOT_FOUND,
            )
        shard = str(detail.get("shard") or function.get("shard") or "")

    db_candidates: list[str] = []
    shard_path = service.db_path_for_shard(shard) if service.federated else None
    if shard_path:
        db_candidates.append(shard_path)
    if service.federated:
        db_candidates.extend(path for path in service.paths if path not in db_candidates)
    else:
        db_candidates.append(service.paths[0])

    tu_id = str(function.get("tu_id") or "")
    tu_row: dict[str, Any] | None = None
    for db_path in db_candidates:
        tu_row = _read_tu_row(db_path, tu_id)
        if tu_row is not None:
            break

    file_value = str(function.get("file") or "")
    repo_root = str(
        (tu_row or {}).get("repo_root") or (tu_row or {}).get("directory") or ""
    )
    language = str((tu_row or {}).get("language") or "")
    function_start = int(function.get("line") or 0)
    function_end = int(function.get("end_line") or 0) or function_start
    if not file_value or not repo_root or function_start <= 0:
        raise ApiError(
            "NOT_FOUND",
            "no source location recorded for this function",
            HTTPStatus.NOT_FOUND,
        )

    file_path = Path(file_value)
    if file_path.is_absolute() or ".." in file_path.parts or file_value.startswith("\\"):
        raise ApiError(
            "NOT_FOUND",
            "source path escapes the repository root",
            HTTPStatus.NOT_FOUND,
        )
    try:
        root = Path(repo_root).expanduser().resolve()
        candidate = (root / file_value).resolve()
        candidate.relative_to(root)  # realpath containment check
    except (OSError, ValueError):
        raise ApiError(
            "NOT_FOUND",
            "source path escapes the repository root",
            HTTPStatus.NOT_FOUND,
        ) from None
    if not candidate.is_file():
        raise ApiError(
            "NOT_FOUND",
            f"source file not found: {file_value}",
            HTTPStatus.NOT_FOUND,
        )
    try:
        source_text = candidate.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ApiError(
            "NOT_FOUND",
            f"cannot read source file: {exc}",
            HTTPStatus.NOT_FOUND,
        ) from None

    source_lines = source_text.splitlines()
    total_lines = len(source_lines)
    if total_lines == 0 or function_start > total_lines:
        raise ApiError(
            "NOT_FOUND",
            "recorded source line is outside the file",
            HTTPStatus.NOT_FOUND,
        )
    if function_end < function_start:
        function_end = function_start
    start = max(1, function_start - context)
    end = min(total_lines, function_end + context)
    truncated = False
    if end - start + 1 > MAX_SOURCE_LINES:
        end = start + MAX_SOURCE_LINES - 1
        truncated = True
    lines = [
        {"line": number, "text": source_lines[number - 1]}
        for number in range(start, end + 1)
    ]
    return {
        "symbol": symbol,
        "function": {
            "id": str(function.get("id") or ""),
            "name": str(function.get("name") or ""),
            "qualified_name": str(function.get("qualified_name") or ""),
            "kind": str(function.get("kind") or ""),
            "file": file_value,
            "line": function_start,
            "end_line": function_end,
            "signature": str(function.get("signature") or ""),
        },
        "file": file_value,
        "language": language,
        "repo_root": str(root),
        "path": str(candidate),
        "start": start,
        "end": end,
        "total_lines": total_lines,
        "function_start": function_start,
        "function_end": function_end,
        "context": context,
        "truncated": truncated,
        "lines": lines,
        "shard": shard or None,
        "federated": service.federated,
    }


def api_source(service: QueryService, params: dict[str, str]) -> dict[str, Any]:
    symbol = _symbol(params)
    context = _int_param(
        params,
        "context",
        DEFAULT_SOURCE_CONTEXT,
        minimum=0,
        maximum=MAX_SOURCE_CONTEXT,
    )
    return _source_payload(service, symbol, context)


# --------------------------------------------------------------------------- #
def api_health(service: QueryService, started_at: float) -> dict[str, Any]:
    database = service.describe()
    data: dict[str, Any] = {
        "status": "ok",
        "server": "ca-ui",
        "version": TOOL_VERSION,
        "started_at": time.strftime(
            "%Y-%m-%dT%H:%M:%S%z", time.localtime(started_at)
        ),
        "uptime_seconds": round(max(0.0, time.time() - started_at), 3),
        "database": database,
        "db_ok": True,
    }
    try:
        with service.open() as store:
            stats = dict(store.stats())
            shards = getattr(store, "shards", None)
            if shards is not None:
                database["shards"] = [
                    item.get("shard", "") for item in shards
                ]
                database["skipped"] = list(getattr(store, "skipped", []) or [])
    except (DatabaseSourceError, sqlite3.Error, OSError) as exc:
        data["status"] = "degraded"
        data["db_ok"] = False
        data["error"] = {"code": "DB_UNAVAILABLE", "message": str(exc)}
        return data
    tables = stats.get("tables") or {}
    database.update(
        {
            "db_bytes": stats.get("db_bytes", 0),
            "schema_version": stats.get("schema_version", ""),
            "sqlite_version": stats.get("sqlite_version", ""),
            "counts": {
                table: int(tables.get(table, 0))
                for table in (
                    "functions",
                    "calls",
                    "branches",
                    "returns",
                    "resource_ops",
                    "callback_registrations",
                )
            },
        }
    )
    return data


# --------------------------------------------------------------------------- #
# HTTP server
# --------------------------------------------------------------------------- #


def _make_handler(service: QueryService) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = f"ca-ui/{TOOL_VERSION}"
        sys_version = ""

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            print(
                f"[ui] {self.address_string()} {format % args}",
                file=sys.stderr,
                flush=True,
            )

        # -- response helpers ------------------------------------------------

        def _headers(self) -> None:
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", _CSP)

        def _send_json(self, status: HTTPStatus, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(int(status))
            self._headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _ok(self, data: Any) -> None:
            self._send_json(HTTPStatus.OK, {"ok": True, "data": data})

        def _error(
            self, status: HTTPStatus, code: str, message: str
        ) -> None:
            self._send_json(
                status,
                {"ok": False, "error": {"code": code, "message": message}},
            )

        def _send_static(self, name: str) -> None:
            content_type = STATIC_FILES.get(name)
            if content_type is None:
                self._error(HTTPStatus.NOT_FOUND, "NOT_FOUND", f"not found: {name}")
                return
            try:
                body = (STATIC_DIR / name).read_bytes()
            except OSError as exc:
                self._error(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "STATIC_UNAVAILABLE",
                    f"cannot read {name}: {exc}",
                )
                return
            self.send_response(int(HTTPStatus.OK))
            self._headers()
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        # -- routing ---------------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            parsed = urlsplit(self.path)
            path = parsed.path or "/"
            params = _params(parsed.query)
            try:
                if path in ("/", "/index.html"):
                    self._send_static("index.html")
                    return
                if path.startswith("/") and path.count("/") == 1:
                    name = path[1:]
                    if name in STATIC_FILES:
                        self._send_static(name)
                        return
                if path in ("/api/health", "/health"):
                    self._ok(api_health(service, self.server.started_at))
                    return
                if path == "/api/ui":
                    self._ok(
                        {
                            "mode": "embedded",
                            "serve_url": "",
                            "version": TOOL_VERSION,
                            "features": {"jobs": False, "sse": False, "mcp": False},
                        }
                    )
                    return
                if path == "/api/search":
                    self._ok(api_search(service, params))
                    return
                if path in ("/api/function", "/api/detail"):
                    symbol = params.get("symbol") or params.get("name") or ""
                    self._ok(api_detail(service, symbol))
                    return
                if path.startswith("/api/function/"):
                    self._ok(
                        api_detail(
                            service, unquote(path[len("/api/function/") :])
                        )
                    )
                    return
                if path == "/api/callers":
                    self._ok(api_callers(service, params))
                    return
                if path == "/api/callees":
                    self._ok(api_callees(service, params))
                    return
                if path == "/api/graph":
                    self._ok(api_graph(service, params))
                    return
                if path == "/api/stats":
                    self._ok(api_stats(service))
                    return
                if path == "/api/callbacks":
                    self._ok(api_callbacks(service, params))
                    return
                if path == "/api/source":
                    self._ok(api_source(service, params))
                    return
                if path == "/api/cfg":
                    self._ok(api_cfg(service, params))
                    return
                if path in FACT_ENDPOINTS:
                    self._ok(api_facts(service, params, FACT_ENDPOINTS[path]))
                    return
                if path.startswith("/api/"):
                    self._error(
                        HTTPStatus.NOT_FOUND,
                        "NOT_FOUND",
                        f"unknown endpoint: {path}",
                    )
                    return
                self._error(
                    HTTPStatus.NOT_FOUND, "NOT_FOUND", f"not found: {path}"
                )
            except ApiError as exc:
                self._error(exc.status, exc.code, exc.message)
            except DatabaseSourceError as exc:
                self._error(
                    HTTPStatus.SERVICE_UNAVAILABLE, "DB_UNAVAILABLE", str(exc)
                )
            except (ValueError, GraphError) as exc:
                self._error(HTTPStatus.BAD_REQUEST, "INVALID_ARGUMENT", str(exc))
            except sqlite3.Error as exc:
                print(
                    f"[ui] database error: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                self._error(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "DB_ERROR",
                    f"{type(exc).__name__}: {exc}",
                )
            except Exception as exc:  # pragma: no cover - defensive last resort
                print(traceback.format_exc(), file=sys.stderr, flush=True)
                self._error(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "INTERNAL",
                    f"{type(exc).__name__}: {exc}",
                )

        def _read_only(self) -> None:
            self._error(
                HTTPStatus.METHOD_NOT_ALLOWED,
                "READ_ONLY",
                "the CA UI is read-only; only GET requests are supported",
            )

        do_POST = _read_only  # noqa: N815 - http.server API
        do_PUT = _read_only  # noqa: N815
        do_PATCH = _read_only  # noqa: N815
        do_DELETE = _read_only  # noqa: N815
        do_OPTIONS = _read_only  # noqa: N815

    return Handler


class UiHTTPServer(ThreadingHTTPServer):
    """Threaded HTTP server carrying the read-only query service."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self, address: tuple[str, int], service: QueryService
    ) -> None:
        self.service = service
        self.started_at = time.time()
        super().__init__(address, _make_handler(service))

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        if ":" in str(host):  # IPv6 literal
            return f"http://[{host}]:{port}/"
        return f"http://{host}:{port}/"


class UiProxyHTTPServer(ThreadingHTTPServer):
    """Thin UI server: static assets + same-origin reverse proxy to ``ca serve``."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        serve_url: str,
        *,
        timeout: float = PROXY_TIMEOUT,
    ) -> None:
        self.serve_url = serve_url.rstrip("/")
        self.upstream_timeout = float(timeout)
        self.started_at = time.time()
        super().__init__(address, _make_proxy_handler(self))

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        if ":" in str(host):  # IPv6 literal
            return f"http://[{host}]:{port}/"
        return f"http://{host}:{port}/"


def _make_proxy_handler(server: "UiProxyHTTPServer") -> type[BaseHTTPRequestHandler]:
    class ProxyHandler(BaseHTTPRequestHandler):
        server_version = f"ca-ui/{TOOL_VERSION}"
        sys_version = ""

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            print(
                f"[ui] {self.address_string()} {format % args}",
                file=sys.stderr,
                flush=True,
            )

        # -- response helpers ------------------------------------------------

        def _headers(self) -> None:
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", _CSP)

        def _send_raw(
            self, status: int, content_type: str, body: bytes
        ) -> None:
            self.send_response(int(status))
            self._headers()
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode(
                "utf-8"
            )
            self._send_raw(
                status, "application/json; charset=utf-8", body
            )

        def _error(self, status: int, code: str, message: str) -> None:
            self._send_json(
                status,
                {"ok": False, "error": {"code": code, "message": message}},
            )

        def _send_static(self, name: str) -> None:
            content_type = STATIC_FILES.get(name)
            if content_type is None:
                self._error(
                    int(HTTPStatus.NOT_FOUND), "NOT_FOUND", f"not found: {name}"
                )
                return
            try:
                body = (STATIC_DIR / name).read_bytes()
            except OSError as exc:
                self._error(
                    int(HTTPStatus.INTERNAL_SERVER_ERROR),
                    "STATIC_UNAVAILABLE",
                    f"cannot read {name}: {exc}",
                )
                return
            self._send_raw(int(HTTPStatus.OK), content_type, body)

        # -- upstream proxy --------------------------------------------------

        def _upstream_url(self) -> str:
            return f"{self.server.serve_url}{self.path}"

        def _proxy_json(self, method: str, body: bytes | None) -> None:
            headers = {"Accept": "application/json"}
            if body is not None:
                headers["Content-Type"] = (
                    self.headers.get("Content-Type")
                    or "application/json; charset=utf-8"
                )
            request = urllib.request.Request(
                self._upstream_url(),
                data=body,
                headers=headers,
                method=method,
            )
            try:
                with urllib.request.urlopen(
                    request, timeout=self.server.upstream_timeout
                ) as response:
                    payload = response.read()
                    status = int(response.status)
                    content_type = response.headers.get(
                        "Content-Type", "application/json; charset=utf-8"
                    )
            except urllib.error.HTTPError as exc:  # pass upstream errors through
                payload = exc.read()
                status = int(exc.code)
                content_type = exc.headers.get(
                    "Content-Type", "application/json; charset=utf-8"
                )
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                self._error(
                    int(HTTPStatus.BAD_GATEWAY),
                    "SERVE_UNAVAILABLE",
                    f"cannot reach CA serve backend at {self.server.serve_url}:"
                    f" {type(exc).__name__}: {exc}",
                )
                return
            self._send_raw(status, content_type, payload)

        def _proxy_events(self) -> None:
            headers = {"Accept": "text/event-stream"}
            last_event_id = self.headers.get("Last-Event-ID")
            if last_event_id:
                headers["Last-Event-ID"] = last_event_id
            request = urllib.request.Request(
                self._upstream_url(), headers=headers, method="GET"
            )
            try:
                response = urllib.request.urlopen(
                    request, timeout=PROXY_SSE_IDLE_TIMEOUT
                )
            except urllib.error.HTTPError as exc:
                payload = exc.read()
                self._send_raw(
                    int(exc.code),
                    exc.headers.get(
                        "Content-Type", "application/json; charset=utf-8"
                    ),
                    payload,
                )
                return
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                self._error(
                    int(HTTPStatus.BAD_GATEWAY),
                    "SERVE_UNAVAILABLE",
                    f"cannot reach CA serve event stream at"
                    f" {self.server.serve_url}: {type(exc).__name__}: {exc}",
                )
                return
            try:
                status = int(getattr(response, "status", 200))
                self.send_response(status)
                self._headers()
                self.send_header(
                    "Content-Type",
                    response.headers.get(
                        "Content-Type", "text/event-stream; charset=utf-8"
                    ),
                )
                self.send_header("Cache-Control", "no-cache")
                self.send_header("X-Accel-Buffering", "no")
                # Close-delimited HTTP/1.0 stream: force the connection to close
                # when the upstream ends so EventSource can reconnect cleanly.
                self.close_connection = True
                self.end_headers()
                # ``read(8192)`` blocks until 8192 bytes for close-delimited /
                # chunked bodies; ``read1`` forwards each arrived chunk so SSE
                # events reach the browser immediately.
                reader = getattr(response, "read1", None)
                while True:
                    chunk = reader(8192) if reader is not None else response.readline()
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (OSError, TimeoutError, ValueError):
                pass  # client disconnected or upstream stalled; EventSource reconnects
            finally:
                response.close()

        def _read_body(self) -> bytes | None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0 or length > PROXY_MAX_BODY:
                self._error(
                    int(HTTPStatus.BAD_REQUEST),
                    "INVALID_BODY",
                    "invalid or too large request body",
                )
                return None
            return self.rfile.read(length)

        # -- routing ---------------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            parsed = urlsplit(self.path)
            path = parsed.path or "/"
            try:
                if path in ("/", "/index.html"):
                    self._send_static("index.html")
                    return
                if path.startswith("/") and path.count("/") == 1:
                    name = path[1:]
                    if name in STATIC_FILES:
                        self._send_static(name)
                        return
                if path == "/api/ui":
                    self._send_json(
                        int(HTTPStatus.OK),
                        {
                            "ok": True,
                            "data": {
                                "mode": "remote",
                                "serve_url": self.server.serve_url,
                                "version": TOOL_VERSION,
                                "features": {
                                    "jobs": True,
                                    "sse": True,
                                    "mcp": True,
                                },
                            },
                        },
                    )
                    return
                if path == "/events":
                    self._proxy_events()
                    return
                if path.startswith("/api/"):
                    self._proxy_json("GET", None)
                    return
                self._error(
                    int(HTTPStatus.NOT_FOUND), "NOT_FOUND", f"not found: {path}"
                )
            except Exception as exc:  # pragma: no cover - defensive
                print(traceback.format_exc(), file=sys.stderr, flush=True)
                self._error(
                    int(HTTPStatus.INTERNAL_SERVER_ERROR),
                    "INTERNAL",
                    f"{type(exc).__name__}: {exc}",
                )

        def do_POST(self) -> None:  # noqa: N802 - http.server API
            path = urlsplit(self.path).path or "/"
            allowed = path in PROXY_POST_PATHS or path.startswith("/api/jobs/")
            if not allowed:
                self._error(
                    int(HTTPStatus.METHOD_NOT_ALLOWED),
                    "READ_ONLY",
                    "only /api/jobs accepts POST from the UI",
                )
                return
            body = self._read_body()
            if body is None:
                return
            try:
                self._proxy_json("POST", body)
            except Exception as exc:  # pragma: no cover - defensive
                print(traceback.format_exc(), file=sys.stderr, flush=True)
                self._error(
                    int(HTTPStatus.INTERNAL_SERVER_ERROR),
                    "INTERNAL",
                    f"{type(exc).__name__}: {exc}",
                )

        def _read_only(self) -> None:
            self._error(
                int(HTTPStatus.METHOD_NOT_ALLOWED),
                "READ_ONLY",
                "the CA UI is read-only; only GET and /api/jobs POST are supported",
            )

        do_PUT = _read_only  # noqa: N815 - http.server API
        do_PATCH = _read_only  # noqa: N815
        do_DELETE = _read_only  # noqa: N815
        do_OPTIONS = _read_only  # noqa: N815

    return ProxyHandler


def make_proxy_server(
    serve_url: str,
    *,
    host: str = DEFAULT_HOST,
    port: int = 0,
    timeout: float = PROXY_TIMEOUT,
) -> UiProxyHTTPServer:
    """Build (but do not start) a thin UI proxy server for ``ca serve``.

    The browser keeps talking to the local UI origin; ``/api/*`` and ``/events``
    are forwarded verbatim to the unified backend, so no CORS/CSP changes are
    needed. ``port=0`` lets the OS pick a free port (used by tests).
    """

    url = str(serve_url or "").strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise DatabaseSourceError(
            f"--serve-url must be an http(s) URL, got {serve_url!r}"
        )
    try:
        return UiProxyHTTPServer((host, int(port)), url, timeout=timeout)
    except OSError as exc:
        raise DatabaseSourceError(f"cannot bind {host}:{port}: {exc}") from exc


def serve_proxy(
    serve_url: str,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    timeout: float = PROXY_TIMEOUT,
) -> int:
    """Serve the thin UI until SIGINT/SIGTERM; returns a CLI exit code."""

    try:
        server = make_proxy_server(
            serve_url, host=host, port=port, timeout=timeout
        )
    except DatabaseSourceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(
        f"[ui] CA web UI (thin client) listening on {server.url}"
        f" (serve={server.serve_url})",
        file=sys.stderr,
        flush=True,
    )
    print("[ui] press Ctrl-C to stop", file=sys.stderr, flush=True)

    stop = threading.Event()
    previous: dict[int, Any] = {}

    def _on_signal(signum: int, _frame: Any) -> None:
        stop.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[signum] = signal.signal(signum, _on_signal)
        except (ValueError, OSError):  # not in the main thread
            pass
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.2},
        name="ca-ui-proxy-http",
        daemon=True,
    )
    started = False
    try:
        thread.start()
        started = True
        while not stop.wait(0.2):
            pass
    except KeyboardInterrupt:  # pragma: no cover - interactive path
        pass
    finally:
        for signum, handler in previous.items():
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):  # pragma: no cover - non-main thread
                pass
        if started:
            server.shutdown()
            thread.join(timeout=5.0)
        server.server_close()
    print("[ui] server stopped", file=sys.stderr, flush=True)
    return 0


def make_server(
    db_values: Any,
    db_dir: str | None = None,
    *,
    host: str = DEFAULT_HOST,
    port: int = 0,
) -> UiHTTPServer:
    """Build (but do not start) a read-only UI server.

    ``port=0`` lets the OS pick a free port (used by tests). Database selection
    errors raise :class:`ca.cli.dbsource.DatabaseSourceError`.
    """

    service = resolve_service(db_values, db_dir)
    try:
        return UiHTTPServer((host, int(port)), service)
    except OSError as exc:
        raise DatabaseSourceError(
            f"cannot bind {host}:{port}: {exc}"
        ) from exc


def serve(
    db_values: Any,
    db_dir: str | None = None,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> int:
    """Serve the UI until SIGINT/SIGTERM; returns a CLI exit code."""

    try:
        server = make_server(db_values, db_dir, host=host, port=port)
    except DatabaseSourceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(
        f"[ui] CA read-only web UI listening on {server.url}"
        f" (db={', '.join(server.service.paths)}; federated="
        f"{'yes' if server.service.federated else 'no'})",
        file=sys.stderr,
        flush=True,
    )
    print("[ui] press Ctrl-C to stop", file=sys.stderr, flush=True)

    stop = threading.Event()
    previous: dict[int, Any] = {}

    def _on_signal(signum: int, _frame: Any) -> None:
        stop.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[signum] = signal.signal(signum, _on_signal)
        except (ValueError, OSError):  # not in the main thread
            pass
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.2},
        name="ca-ui-http",
        daemon=True,
    )
    started = False
    try:
        thread.start()
        started = True
        while not stop.wait(0.2):
            pass
    except KeyboardInterrupt:  # pragma: no cover - interactive path
        pass
    finally:
        for signum, handler in previous.items():
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):  # pragma: no cover - non-main thread
                pass
        if started:
            server.shutdown()
            thread.join(timeout=5.0)
        server.server_close()
    print("[ui] server stopped", file=sys.stderr, flush=True)
    return 0


def cmd_ui(args: Any) -> int:
    """``ca ui`` subcommand implementation.

    ``--serve-url`` selects the thin-client mode (static UI + same-origin proxy
    to the unified ``ca serve`` backend); otherwise the embedded read-only mode
    opens the database directly, preserving the previous behaviour.
    """

    serve_url = getattr(args, "serve_url", None)
    db_values = getattr(args, "db", None)
    db_dir = getattr(args, "db_dir", None)
    if serve_url:
        if db_values or db_dir:
            print(
                "error: --serve-url cannot be combined with --db/--db-dir",
                file=sys.stderr,
            )
            return 1
        return serve_proxy(
            serve_url,
            host=getattr(args, "host", DEFAULT_HOST),
            port=getattr(args, "port", DEFAULT_PORT),
        )
    return serve(
        db_values,
        db_dir,
        host=getattr(args, "host", DEFAULT_HOST),
        port=getattr(args, "port", DEFAULT_PORT),
    )
