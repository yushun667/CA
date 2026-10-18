"""Minimal MCP (Model Context Protocol) server for CA over JSON-RPC 2.0.

Transports
----------

* ``stdio`` (default): newline-delimited JSON-RPC messages on stdin/stdout,
  diagnostics on stderr. This is what an agent process or an SSH pipe uses:

      ssh server '/path/to/venv/bin/python -m ca mcp --db /path/index.db'

* ``http``: JSON-RPC over ``POST /mcp`` (Streamable HTTP style, one JSON
  response per request) plus ``GET /health``. Default bind ``127.0.0.1:8765``;
  expose it to other machines only through a LAN firewall rule or, preferably,
  an SSH tunnel (see docs/mcp.md).

Every ``tools/call`` result is a JSON object inside MCP's text content block::

    {"ok": true, "tool": "find_symbol", "data": {...}}
    {"ok": false, "tool": "...", "error": {"code": "...", "message": "..."}}

so clients can parse one stable envelope regardless of the tool. List tools
return ``{"items": [...], "count": N, "limit": L, "offset": O}``; ``graph``
returns the normalized graph schema documented in :mod:`ca.cli.graph`.
"""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

from ca.core.schema import TOOL_VERSION

JSONRPC_VERSION = "2.0"
SERVER_NAME = "ca-analyzer"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_MAX_LIMIT = 1000
DEFAULT_HTTP_PORT = 8765


class JsonRpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class ToolError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- #
# Tool schemas (stable; docs/mcp.md mirrors this table)
# --------------------------------------------------------------------------- #

_STRING = {"type": "string"}
_INT = {"type": "integer", "minimum": 0}
_LIMIT = {"type": "integer", "minimum": 1, "maximum": DEFAULT_MAX_LIMIT}


def _tool(
    name: str, description: str, properties: dict[str, Any], required: list[str]
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


TOOLS: list[dict[str, Any]] = [
    _tool(
        "find_symbol",
        "Find functions by exact id/name/qualified_name or substring pattern.",
        {"pattern": _STRING, "limit": _LIMIT, "offset": _INT},
        ["pattern"],
    ),
    _tool(
        "functions",
        "Alias of find_symbol: search indexed function definitions/declarations.",
        {"pattern": _STRING, "limit": _LIMIT, "offset": _INT},
        ["pattern"],
    ),
    _tool(
        "callers",
        "Functions that call the given symbol (direct calls plus candidate edges).",
        {"symbol": _STRING, "limit": _LIMIT, "offset": _INT},
        ["symbol"],
    ),
    _tool(
        "callees",
        "Functions called by the given symbol (direct calls plus candidate edges).",
        {"symbol": _STRING, "limit": _LIMIT, "offset": _INT},
        ["symbol"],
    ),
    _tool(
        "graph",
        "Call graph rooted at a symbol; direct and candidate edges with depth.",
        {
            "symbol": _STRING,
            "direction": {"type": "string", "enum": ["forward", "reverse", "both"]},
            "depth": {"type": "integer", "minimum": 0, "maximum": 12},
            "limit": _LIMIT,
        },
        ["symbol"],
    ),
    _tool(
        "branches",
        "Branch/condition facts inside a function.",
        {"symbol": _STRING, "limit": _LIMIT},
        ["symbol"],
    ),
    _tool(
        "assignments",
        "Assignment facts inside a function.",
        {"symbol": _STRING, "limit": _LIMIT},
        ["symbol"],
    ),
    _tool(
        "returns",
        "Return-path facts inside a function.",
        {"symbol": _STRING, "limit": _LIMIT},
        ["symbol"],
    ),
    _tool(
        "resources",
        "Resource acquire/release operations inside a function.",
        {"symbol": _STRING, "limit": _LIMIT},
        ["symbol"],
    ),
    _tool(
        "leaks",
        "Data-flow findings (default kind=leak); optional resource_kind filter.",
        {
            "kind": {"type": "string"},
            "resource_kind": _STRING,
            "limit": _LIMIT,
        },
        [],
    ),
    _tool(
        "detail",
        "Function record plus its branches, assignments, returns, resources, findings.",
        {"symbol": _STRING},
        ["symbol"],
    ),
    _tool("stats", "Database statistics: table row counts, size, schema version.", {}, []),
    _tool(
        "batch",
        "Query one kind (callers/callees/branches/detail/...) for many symbols in one call.",
        {
            "kind": {"type": "string"},
            "symbols": {"type": "array", "items": {"type": "string"}},
            "limit": {"type": "integer", "minimum": 1},
            "offset": {"type": "integer", "minimum": 0},
            "fields": {},
            "compact": {"type": "boolean"},
        },
        ["kind", "symbols"],
    ),
    _tool(
        "brief",
        "Summary-first overview for one symbol (identity/counts/conditions/samples).",
        {"symbol": {"type": "string"}},
        ["symbol"],
    ),
    _tool(
        "callbacks",
        "Cross-TU callback registration facts: query by object path prefix, field or module.",
        {
            "path": {"type": "string"},
            "field": {"type": "string"},
            "module": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1},
        },
        [],
    ),
    _tool(
        "ensure_deep",
        "Lazily compute CFG/data-flow facts for function symbols (L1 lazy deep).",
        {
            "symbols": {"type": "array", "items": {"type": "string"}},
            "jobs": {"type": "integer", "minimum": 1},
            "wait": {"type": "boolean"},
        },
        ["symbols"],
    ),
    _tool(
        "deep_status",
        "Report deep_ready coverage, optionally per symbol.",
        {"symbols": {"type": "array", "items": {"type": "string"}}},
        [],
    ),
    _tool(
        "prewarm",
        "Pre-analyze the hottest pending queried symbols (L2 queue).",
        {
            "limit": {"type": "integer", "minimum": 1},
            "jobs": {"type": "integer", "minimum": 1},
            "background": {"type": "boolean"},
        },
        [],
    ),
]

TOOL_NAMES = {tool["name"] for tool in TOOLS}


# --------------------------------------------------------------------------- #
# Store access + tool implementations
# --------------------------------------------------------------------------- #


def _clamp_limit(value: Any, default: int = 50) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = default
    return max(1, min(limit, DEFAULT_MAX_LIMIT))


def _clamp_offset(value: Any) -> int:
    try:
        offset = int(value)
    except (TypeError, ValueError):
        offset = 0
    return max(0, offset)


@contextmanager
def _open_store(db_path: Any) -> Iterator[Any]:
    """Open one database or a read-only federated set of shard databases."""

    paths = [str(db_path)] if isinstance(db_path, (str, Path)) else [str(item) for item in db_path]
    paths = [path for path in paths if path]
    if not paths:
        raise ToolError("DB_NOT_FOUND", "no database selected")
    if len(paths) == 1:
        if not Path(paths[0]).is_file():
            raise ToolError("DB_NOT_FOUND", f"database not found: {paths[0]}")
        from ca.store import SqliteStore

        store: Any = SqliteStore(paths[0])
    else:
        from ca.store.federated import FederatedStore

        store = FederatedStore(paths)
    try:
        store.open()
        shards = getattr(store, "shards", None)
        if shards is not None and not shards:
            skipped = getattr(store, "skipped", []) or []
            detail = "; ".join(f"{item.get('path')}: {item.get('error')}" for item in skipped[:3])
            raise ToolError(
                "DB_NOT_FOUND", f"no readable shard databases: {detail or 'none found'}"
            )
        yield store
    except ToolError:
        raise
    except Exception as exc:  # store/database errors become tool errors
        raise ToolError("DB_ERROR", f"{type(exc).__name__}: {exc}") from exc
    finally:
        try:
            store.close()
        except Exception:
            pass


def _items(rows: list[dict[str, Any]], limit: int, offset: int) -> dict[str, Any]:
    window = rows[offset : offset + limit]
    return {"items": window, "count": len(window), "limit": limit, "offset": offset}


def _require(arguments: dict[str, Any], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ToolError("INVALID_ARGUMENT", f"argument {name!r} must be a non-empty string")
    return value


def _find_symbol(store: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    pattern = _require(arguments, "pattern")
    limit = _clamp_limit(arguments.get("limit"), 50)
    offset = _clamp_offset(arguments.get("offset"))
    rows = list(store.query_functions(pattern, limit=limit + offset))
    page = list(rows[offset : offset + limit])
    data = _items(rows, limit, offset)
    # Additive grouping for header-declaration-only symbols (definition missing).
    data["definitions"] = [row for row in page if row.get("is_definition")]
    data["declarations"] = [row for row in page if not row.get("is_definition")]
    return data


def _callers(store: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    symbol = _require(arguments, "symbol")
    limit = _clamp_limit(arguments.get("limit"), 200)
    offset = _clamp_offset(arguments.get("offset"))
    rows = list(store.query_callers(symbol, limit=limit + offset))
    return _items(rows, limit, offset)


def _callees(store: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    symbol = _require(arguments, "symbol")
    limit = _clamp_limit(arguments.get("limit"), 200)
    offset = _clamp_offset(arguments.get("offset"))
    rows = list(store.query_callees(symbol, limit=limit + offset))
    return _items(rows, limit, offset)


def _graph(store: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    from ca.cli.graph import DEFAULT_DEPTH, DEFAULT_LIMIT, GraphError, build_call_graph

    symbol = _require(arguments, "symbol")
    try:
        return build_call_graph(
            store,
            symbol,
            direction=str(arguments.get("direction") or "forward"),
            depth=arguments.get("depth", DEFAULT_DEPTH),
            limit=arguments.get("limit", DEFAULT_LIMIT),
        )
    except GraphError as exc:
        raise ToolError("INVALID_ARGUMENT", str(exc)) from exc


def _facts_tool(fetcher_name: str) -> Any:
    def implementation(store: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        symbol = _require(arguments, "symbol")
        limit = _clamp_limit(arguments.get("limit"), 200)
        rows = list(getattr(store, fetcher_name)(symbol)[:limit])
        return _items(rows, limit, 0)

    return implementation


def _leaks(store: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    limit = _clamp_limit(arguments.get("limit"), 200)
    kind = arguments.get("kind") or "leak"
    rows = list(store.query_findings(kind=str(kind), limit=limit))
    resource_kind = arguments.get("resource_kind")
    if resource_kind:
        needle = str(resource_kind).lower()
        rows = [
            row
            for row in rows
            if needle in str(row.get("resource_kind", "")).lower()
        ]
    return _items(rows, limit, 0)


def _detail(store: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    from ca.cli import query_ux

    symbol = _require(arguments, "symbol")
    detail = dict(store.query_function_detail(symbol))
    function = detail.get("function")
    if not function:
        raise ToolError("NOT_FOUND", f"no function matching {symbol!r}")
    compact = bool(arguments.get("compact")) or arguments.get("fields") == "compact"
    fields = arguments.get("fields")
    try:
        if compact:
            return query_ux.compact_detail(detail)
        if fields:
            return query_ux.project_detail(detail, query_ux.parse_fields(fields))
    except query_ux.QueryUxError as exc:
        raise ToolError("INVALID_ARGUMENT", str(exc)) from None
    return detail


def _stats(store: Any, _arguments: dict[str, Any]) -> dict[str, Any]:
    data = dict(store.stats())
    if hasattr(store, "callback_stats"):
        try:
            data["callbacks"] = dict(store.callback_stats())
        except Exception:
            pass
    return data


def _callbacks(store: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    """Query cross-TU callback registration facts (additive MCP tool)."""

    if not hasattr(store, "query_callbacks"):
        raise ToolError("DB_ERROR", "callback registration API unavailable in this store")
    limit = int(arguments.get("limit") or 200)
    prefix = arguments.get("path")
    field = arguments.get("field")
    module = arguments.get("module")
    offset = int(arguments.get("offset") or 0)
    rows = store.query_callbacks(
        path_prefix=str(prefix) if prefix else None,
        field=str(field) if field else None,
        module=str(module) if module else None,
        limit=limit,
        offset=offset,
    )
    return {"items": rows, "count": len(rows), "limit": limit, "offset": offset}


def _deep_ready(store: Any, db_path: Any, symbol: str) -> bool:
    from ca.cli import deep as deep_module

    try:
        detail = store.query_function_detail(symbol) or {}
        if "deep_ready" in detail:
            return bool(detail.get("deep_ready"))
    except Exception:
        pass
    if not isinstance(db_path, (str, Path)):
        return False
    try:
        return bool(
            (deep_module.deep_status_for(str(db_path), store, [symbol]).get(symbol) or {}).get(
                "deep_ready"
            )
        )
    except Exception:
        return False


def _detail_with_deep(store: Any, db_path: str, arguments: dict[str, Any]) -> dict[str, Any]:
    detail = _detail(store, arguments)
    symbol = str(arguments.get("symbol") or "")
    ready = bool(detail.get("deep_ready")) if "deep_ready" in detail else False
    if "deep_ready" not in detail:
        ready = _deep_ready(store, db_path, symbol)
    detail["deep_ready"] = ready
    return detail


def _graph_with_deep(store: Any, db_path: str, arguments: dict[str, Any]) -> dict[str, Any]:
    graph = _graph(store, arguments)
    symbol = str(arguments.get("symbol") or "")
    ready = bool(graph.get("deep_ready")) if "deep_ready" in graph else False
    if "deep_ready" not in graph:
        ready = _deep_ready(store, db_path, symbol)
    graph["deep_ready"] = ready
    return graph


def _facts_with_deep(fetcher_name: str, store: Any, db_path: str, arguments: dict[str, Any]) -> dict[str, Any]:
    symbol = str(arguments.get("symbol") or "")
    data = _facts_tool(fetcher_name)(store, arguments)
    ready = _deep_ready(store, db_path, symbol)
    data["deep_ready"] = ready
    data["pending"] = not ready
    return data


def _leaks_with_deep(store: Any, _db_path: str, arguments: dict[str, Any]) -> dict[str, Any]:
    data = _leaks(store, arguments)
    data["deep_ready"] = None  # findings are cross-symbol; caller checks stats/detail
    return data


def _ensure_deep_tool(store: Any, db_path: str, arguments: dict[str, Any], session: "McpSession") -> dict[str, Any]:
    from ca.cli import deep as deep_module

    symbols = [str(item) for item in (arguments.get("symbols") or []) if str(item)]
    if not symbols:
        raise ToolError("INVALID_ARGUMENT", "symbols must be a non-empty list")
    if not session.repo or not session.compdb:
        raise ToolError(
            "DEEP_UNAVAILABLE",
            "server started without --repo/--compdb; lazy deep analysis unavailable",
        )
    wait = bool(arguments.get("wait", True))
    jobs = max(1, int(arguments.get("jobs") or 1))
    if not wait:
        queue = deep_module.DeepQueue(db_path)
        for symbol in symbols:
            queue.record(symbol)
        status = deep_module.deep_status_for(db_path, store, symbols)
        return {
            "requested": len(symbols),
            "queued": True,
            "results": [
                {
                    "symbol": symbol,
                    "deep_ready": bool((status.get(symbol) or {}).get("deep_ready")),
                    "status": "pending"
                    if not (status.get(symbol) or {}).get("deep_ready")
                    else "ready",
                }
                for symbol in symbols
            ],
        }
    result = deep_module.ensure_deep(
        db_path, session.repo, session.compdb, symbols, jobs=jobs
    )
    return result


def _deep_status_tool(store: Any, db_path: str, arguments: dict[str, Any]) -> dict[str, Any]:
    from ca.cli import deep as deep_module

    data = deep_module.deep_stats(db_path, store)
    symbols = [str(item) for item in (arguments.get("symbols") or [])]
    if symbols:
        data["symbols"] = deep_module.deep_status_for(db_path, store, symbols)
    return data


def _prewarm_tool(_store: Any, db_path: str, arguments: dict[str, Any], session: "McpSession") -> dict[str, Any]:
    from ca.cli import deep as deep_module

    if not session.repo or not session.compdb:
        raise ToolError(
            "DEEP_UNAVAILABLE",
            "server started without --repo/--compdb; prewarm unavailable",
        )
    limit = max(1, int(arguments.get("limit") or 20))
    jobs = max(1, int(arguments.get("jobs") or 1))
    background = bool(arguments.get("background", False))
    if background:
        deep_module.prewarm_background(
            db_path, session.repo, session.compdb, limit=limit, jobs=jobs
        )
        return {"status": "started", "limit": limit, "jobs": jobs}
    return deep_module.prewarm(
        db_path, session.repo, session.compdb, limit=limit, jobs=jobs
    )


def _federated_deep_status(store: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    symbols = [str(item) for item in (arguments.get("symbols") or [])]
    data: dict[str, Any] = {
        "federated": True,
        "shards": [item.get("shard", "") for item in getattr(store, "shards", [])],
        "note": "deep state is merged read-only across shards; writes are per-database",
    }
    if symbols:
        resolved: dict[str, Any] = {}
        for symbol in symbols:
            try:
                detail = store.query_function_detail(symbol) or {}
            except Exception:
                detail = {}
            function = detail.get("function") or {}
            resolved[symbol] = {
                "function_id": function.get("id", ""),
                "name": function.get("name", ""),
                "shard": function.get("shard", ""),
                "deep_ready": bool(detail.get("deep_ready")),
                "found": bool(function),
            }
        data["symbols"] = resolved
    return data


def _batch(store: Any, arguments: dict[str, Any], federated: bool) -> dict[str, Any]:
    from ca.cli import query_ux

    kind = str(arguments.get("kind") or "")
    symbols = arguments.get("symbols") or []
    if not isinstance(symbols, list) or not symbols:
        raise ToolError("INVALID_ARGUMENT", "symbols must be a non-empty array")
    try:
        return query_ux.resolve_batch(
            store,
            kind,
            [str(symbol) for symbol in symbols],
            limit=arguments.get("limit"),
            offset=int(arguments.get("offset") or 0),
            fields=arguments.get("fields"),
            compact=bool(arguments.get("compact")),
            federated=federated,
        )
    except query_ux.QueryUxError as exc:
        raise ToolError("INVALID_ARGUMENT", str(exc)) from None


def _brief(store: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    from ca.cli import query_ux

    symbol = _require(arguments, "symbol")
    if not hasattr(store, "query_function_detail"):
        raise ToolError("DB_ERROR", "brief requires the function detail API")
    return query_ux.resolve_brief(store, symbol)


def _record_query(db_path: Any, name: str, arguments: dict[str, Any]) -> None:
    symbol = arguments.get("symbol") or arguments.get("pattern")
    if not symbol or name in {"stats", "ensure_deep", "prewarm", "deep_status"}:
        return
    try:
        from ca.cli import deep as deep_module

        deep_module.DeepQueue(db_path).record(str(symbol))
    except Exception:
        pass


_TOOL_IMPLEMENTATIONS: dict[str, Any] = {
    "find_symbol": _find_symbol,
    "functions": _find_symbol,
    "callers": _callers,
    "callees": _callees,
    "graph": _graph,
    "branches": _facts_tool("query_branches"),
    "assignments": _facts_tool("query_assignments"),
    "returns": _facts_tool("query_returns"),
    "resources": _facts_tool("query_resource_ops"),
    "leaks": _leaks,
    "detail": _detail,
    "stats": _stats,
    "callbacks": _callbacks,
    "batch": _stats,  # dispatched specially (needs the federated flag)
    "brief": _stats,
    # Dispatched specially in ``call_tool`` (they need the session/db path).
    "ensure_deep": _stats,
    "deep_status": _stats,
    "prewarm": _stats,
}


def call_tool(
    db_path: Any,
    name: str,
    arguments: dict[str, Any] | None = None,
    session: "McpSession | None" = None,
) -> dict[str, Any]:
    """Execute one MCP tool call and return the JSON-ready result envelope."""

    arguments = dict(arguments or {})
    implementation = _TOOL_IMPLEMENTATIONS.get(name)
    if implementation is None:
        raise ToolError("UNKNOWN_TOOL", f"unknown tool: {name!r}")
    session = session or McpSession(db_path)
    if session.federated and name in ("ensure_deep", "prewarm"):
        raise ToolError(
            "FEDERATED_READ_ONLY",
            "ensure_deep/prewarm write per-database state; start the server with"
            " a single --db for deep tools",
        )
    selector = session.db_paths if session.db_paths else db_path
    with _open_store(selector) as store:
        if name == "ensure_deep":
            data = _ensure_deep_tool(store, session.db_path or "", arguments, session)
        elif name == "prewarm":
            data = _prewarm_tool(store, session.db_path or "", arguments, session)
        elif name == "deep_status":
            data = (
                _federated_deep_status(store, arguments)
                if session.federated
                else _deep_status_tool(store, selector, arguments)
            )
        elif name == "detail":
            data = _detail_with_deep(store, selector, arguments)
        elif name == "graph":
            data = _graph_with_deep(store, selector, arguments)
        elif name in ("branches", "assignments", "returns", "resources"):
            fetcher = {
                "branches": "query_branches",
                "assignments": "query_assignments",
                "returns": "query_returns",
                "resources": "query_resource_ops",
            }[name]
            data = _facts_with_deep(fetcher, store, selector, arguments)
        elif name == "batch":
            data = _batch(store, arguments, session.federated)
        elif name == "brief":
            data = _brief(store, arguments)
        elif name == "leaks":
            data = _leaks_with_deep(store, selector, arguments)
        else:
            data = implementation(store, arguments)
    _record_query(selector, name, arguments)
    return {"ok": True, "tool": name, "data": data}


# --------------------------------------------------------------------------- #
# JSON-RPC / MCP protocol
# --------------------------------------------------------------------------- #


class McpSession:
    """Protocol handler; transport code feeds decoded JSON-RPC messages here."""

    def __init__(
        self,
        db_path: Any,
        repo: str = "",
        compdb: str = "",
        federated: bool = False,
    ) -> None:
        if isinstance(db_path, (str, Path)):
            paths = [str(db_path)]
        else:
            paths = [str(item) for item in db_path]
        self.db_paths = paths
        self.db_path = paths[0] if paths else ""
        self.federated = bool(federated or len(paths) > 1)
        self.repo = str(repo or "")
        self.compdb = str(compdb or "")
        self.initialized = False

    def handle(self, message: Any) -> dict[str, Any] | None:
        if not isinstance(message, dict):
            raise JsonRpcError(-32600, "invalid request: expected a JSON object")
        request_id = message.get("id")
        method = message.get("method")
        params = message.get("params") or {}
        if message.get("jsonrpc") != JSONRPC_VERSION or not isinstance(method, str):
            raise JsonRpcError(-32600, "invalid JSON-RPC request")
        if method.startswith("notifications/"):
            return None
        try:
            if method == "initialize":
                return _result(request_id, self._initialize(params))
            if method == "ping":
                return _result(request_id, {})
            if method == "tools/list":
                return _result(request_id, {"tools": TOOLS})
            if method == "tools/call":
                if not isinstance(params, dict):
                    raise JsonRpcError(-32602, "invalid params")
                name = params.get("name")
                arguments = params.get("arguments") or {}
                if not isinstance(name, str) or not isinstance(arguments, dict):
                    raise JsonRpcError(-32602, "tools/call requires name and arguments")
                try:
                    data = call_tool(self.db_path, name, arguments, self)
                    return _result(request_id, _tool_success(data))
                except ToolError as exc:
                    return _result(request_id, _tool_failure(name, exc))
            raise JsonRpcError(-32601, f"method not found: {method}")
        except JsonRpcError:
            raise
        except Exception as exc:  # never leak a traceback to the client
            raise JsonRpcError(-32603, f"internal error: {type(exc).__name__}: {exc}")

    def _initialize(self, params: Any) -> dict[str, Any]:
        requested = ""
        if isinstance(params, dict):
            requested = str(params.get("protocolVersion") or "")
        version = (
            requested
            if requested in SUPPORTED_PROTOCOL_VERSIONS
            else SUPPORTED_PROTOCOL_VERSIONS[0]
        )
        self.initialized = True
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": TOOL_VERSION},
        }

    def handle_line(self, line: str) -> list[dict[str, Any]]:
        """Decode one stdin line into responses (empty for notifications)."""

        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            raise JsonRpcError(-32700, f"parse error: {exc}") from exc
        request_id = message.get("id") if isinstance(message, dict) else None
        try:
            response = self.handle(message)
        except JsonRpcError as exc:
            response = _error_response(request_id, exc.code, exc.message, exc.data)
        return [response] if response is not None else []


def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result}


def _error_response(
    request_id: Any, code: int, message: str, data: Any = None
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "error": error}


def _tool_success(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [
            {"type": "text", "text": json.dumps(data, ensure_ascii=False, default=str)}
        ],
        "isError": False,
    }


def _tool_failure(name: str, error: ToolError) -> dict[str, Any]:
    payload = {"ok": False, "tool": name, "error": {"code": error.code, "message": error.message}}
    return {
        "content": [
            {"type": "text", "text": json.dumps(payload, ensure_ascii=False, default=str)}
        ],
        "isError": True,
    }


def run_stdio(
    db_path: Any, repo: str = "", compdb: str = "", federated: bool = False
) -> int:
    """Serve MCP on stdin/stdout; returns 0 on clean EOF."""

    session = McpSession(db_path, repo=repo, compdb=compdb, federated=federated)
    print(
        f"[mcp] stdio server ready: db={db_path} tools={len(TOOLS)}",
        file=sys.stderr,
        flush=True,
    )
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            responses = session.handle_line(line)
        except JsonRpcError as exc:  # parse error (no request id available)
            responses = [_error_response(None, exc.code, exc.message, exc.data)]
        for response in responses:
            print(json.dumps(response, ensure_ascii=False, default=str), flush=True)
    return 0


# --------------------------------------------------------------------------- #
# HTTP transport
# --------------------------------------------------------------------------- #


def _make_http_handler(session: McpSession) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        server_version = f"{SERVER_NAME}/{TOOL_VERSION}"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            print(f"[mcp] http: {format % args}", file=sys.stderr, flush=True)

        def _send_json(self, status: HTTPStatus, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(int(status))
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            if self.path.rstrip("/") == "/health":
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "status": "ok",
                        "server": SERVER_NAME,
                        "version": TOOL_VERSION,
                        "tools": len(TOOLS),
                    },
                )
            else:
                self._send_json(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    {"error": "POST JSON-RPC to /mcp; GET /health for liveness"},
                )

        def do_POST(self) -> None:  # noqa: N802 - http.server API
            if self.path.rstrip("/") != "/mcp":
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0 or length > 8 * 1024 * 1024:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid body length"})
                return
            raw = self.rfile.read(length)
            try:
                message = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    _error_response(None, -32700, "parse error"),
                )
                return
            try:
                if isinstance(message, list):  # JSON-RPC batch
                    responses = []
                    for item in message:
                        try:
                            response = session.handle(item)
                        except JsonRpcError as exc:
                            response = _error_response(
                                item.get("id") if isinstance(item, dict) else None,
                                exc.code,
                                exc.message,
                                exc.data,
                            )
                        if response is not None:
                            responses.append(response)
                    if not responses:
                        self._send_json(HTTPStatus.ACCEPTED, {})
                    else:
                        self._send_json(HTTPStatus.OK, responses)
                    return
                response = session.handle(message)
            except JsonRpcError as exc:
                request_id = (
                    message.get("id") if isinstance(message, dict) else None
                )
                self._send_json(
                    HTTPStatus.OK,
                    _error_response(request_id, exc.code, exc.message, exc.data),
                )
                return
            if response is None:
                self._send_json(HTTPStatus.ACCEPTED, {})
            else:
                self._send_json(HTTPStatus.OK, response)

    return _Handler


def run_http(
    db_path: Any,
    host: str = "127.0.0.1",
    port: int = DEFAULT_HTTP_PORT,
    repo: str = "",
    compdb: str = "",
    federated: bool = False,
) -> int:
    """Serve MCP over HTTP; blocks until interrupted."""

    session = McpSession(db_path, repo=repo, compdb=compdb, federated=federated)
    server = ThreadingHTTPServer((host, port), _make_http_handler(session))
    actual_port = server.server_address[1]
    print(
        f"[mcp] HTTP listening on http://{host}:{actual_port}/mcp"
        f" (health: /health, db={db_path})",
        file=sys.stderr,
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0
