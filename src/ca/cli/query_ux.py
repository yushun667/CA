"""P0 query-experience helpers: detail projection, batch queries, brief summary.

Pure read-side helpers shared by the CLI and MCP server:

* :func:`project_detail` / :func:`compact_detail` shrink the 13-23 KB
  ``query_function_detail`` payload to the requested fields (or a compact
  preset) without changing the default output;
* :func:`resolve_batch` runs one query kind over many symbols in input order,
  isolating bad symbols, for agents that would otherwise call the CLI/MCP 20+
  times;
* :func:`resolve_brief` is the summary-first entry: identity, counts, key
  conditions and caller/callee samples for a single symbol.

Everything here is read-only (safe for federated stores) and reuses the normal
``SqliteStore`` / ``FederatedStore`` query methods; rows keep their ``shard``
key when the store is federated.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Sequence

COMPACT_FUNCTION_FIELDS: tuple[str, ...] = (
    "id",
    "name",
    "qualified_name",
    "usr",
    "kind",
    "file",
    "line",
    "col",
    "end_line",
    "is_definition",
    "return_type",
    "signature",
    "is_static",
    "is_inline",
    "is_variadic",
    "params",
)
_COMPACT_PARAM_FIELDS = ("name", "type_spelling", "index")
_FACT_KEYS = (
    "branches",
    "assignments",
    "returns",
    "resource_ops",
    "findings",
)
DEFAULT_LIMITS = {
    "functions": 50,
    "callers": 200,
    "callees": 200,
    "branches": 200,
    "assignments": 200,
    "returns": 200,
    "resources": 200,
    "leaks": 200,
}


class QueryUxError(ValueError):
    """Invalid projection/batch request (maps to INVALID_ARGUMENT)."""


# --------------------------------------------------------------------------- #
# Detail projection
# --------------------------------------------------------------------------- #


def parse_fields(values: Any) -> list[str]:
    """Accept a comma string or a sequence of dot paths."""

    if values is None:
        return []
    if isinstance(values, str):
        raw = values.split(",")
    else:
        raw = []
        for item in values:
            raw.extend(str(item).split(","))
    fields = [field.strip() for field in raw if field.strip()]
    if not fields:
        raise QueryUxError("fields must not be empty")
    return fields


def _walk_path(value: Any, parts: Sequence[str], original: str) -> Any:
    current = value
    for part in parts:
        if isinstance(current, dict):
            if part not in current:
                raise QueryUxError(f"unknown field: {original}")
            current = current[part]
        elif isinstance(current, list):
            # A list of dicts: project each element on the remaining path.
            projected = []
            for item in current:
                projected.append(_walk_path(item, parts[parts.index(part):], original))
            return projected
        else:
            raise QueryUxError(f"unknown field: {original}")
    return current


def project_detail(detail: dict[str, Any], fields: Iterable[str]) -> dict[str, Any]:
    """Return only the requested dot paths from a detail payload."""

    requested = list(fields)
    if not requested:
        return dict(detail)
    projected: dict[str, Any] = {}
    for field in requested:
        parts = [part for part in field.split(".") if part]
        if not parts:
            raise QueryUxError(f"unknown field: {field}")
        value = _copy_json(detail)
        for index, part in enumerate(parts):
            if not isinstance(value, dict) or part not in value:
                raise QueryUxError(f"unknown field: {field}")
            if index == len(parts) - 1:
                projected[parts[-1] if len(parts) == 1 else field] = _copy_json(value[part])
                break
            value = value[part]
    return projected


def _copy_json(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False))
    except (TypeError, ValueError):
        return value


def compact_detail(detail: dict[str, Any]) -> dict[str, Any]:
    """Compact detail preset: identity/counts/findings, no fact bodies (<=3-5 KB)."""

    function = dict(detail.get("function") or {})
    compact_function: dict[str, Any] = {}
    for key in COMPACT_FUNCTION_FIELDS:
        if key in function:
            compact_function[key] = function[key]
    params = function.get("params")
    if isinstance(params, list):
        compact_function["params"] = [
            {key: param[key] for key in _COMPACT_PARAM_FIELDS if key in param}
            for param in params
            if isinstance(param, dict)
        ]
    compact: dict[str, Any] = {
        "function": compact_function,
        "counts": {key: len(detail.get(key) or []) for key in _FACT_KEYS},
    }
    findings = detail.get("findings") or []
    if findings:
        compact["findings"] = [
            {
                key: finding.get(key)
                for key in ("id", "kind", "severity", "message", "resource_kind")
                if key in finding
            }
            for finding in findings[:10]
        ]
        compact["findings_truncated"] = len(findings) > 10
    if "deep_ready" in detail:
        compact["deep_ready"] = detail["deep_ready"]
    if "shard" in (detail.get("function") or {}):
        compact["shard"] = detail["function"]["shard"]
    if "shard" in detail:
        compact["shard"] = detail["shard"]
    return compact


def detail_size_bytes(detail: dict[str, Any]) -> int:
    return len(json.dumps(detail, ensure_ascii=False).encode("utf-8"))


# --------------------------------------------------------------------------- #
# Batch queries
# --------------------------------------------------------------------------- #


def parse_symbols(text: str) -> list[str]:
    """Newline/comma separated symbols; ``#`` comments and blanks ignored."""

    symbols: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        for token in line.split(","):
            symbol = token.strip()
            if not symbol or symbol.startswith("#"):
                continue
            if symbol in seen:
                continue
            seen.add(symbol)
            symbols.append(symbol)
    return symbols


def _paged(
    method: Any, symbol: str, limit: int, offset: int, federated: bool
) -> list[dict[str, Any]]:
    if federated:
        return list(method(symbol, limit=limit, offset=offset))
    if offset:
        rows = list(method(symbol, limit=limit + offset))
        return rows[offset:]
    return list(method(symbol, limit=limit))


def _findings_rows(store: Any, limit: int, offset: int, federated: bool) -> list[dict[str, Any]]:
    if federated:
        return list(store.query_findings(limit=limit, offset=offset))
    if offset:
        return list(store.query_findings(limit=limit + offset))[offset:]
    return list(store.query_findings(limit=limit))


def fetch_rows(
    store: Any,
    kind: str,
    symbol: str,
    limit: int,
    offset: int = 0,
    federated: bool = False,
) -> list[dict[str, Any]]:
    """Fetch one query kind for one symbol (shared by batch/CLI/MCP)."""

    if kind == "functions":
        if federated:
            return list(store.query_functions(symbol, limit=limit, offset=offset))
        rows = list(store.query_functions(symbol, limit=limit + offset))
        return rows[offset:]
    method = {
        "callers": "query_callers",
        "callees": "query_callees",
        "branches": "query_branches",
        "assignments": "query_assignments",
        "returns": "query_returns",
        "resources": "query_resource_ops",
    }.get(kind)
    if method is None:
        if kind == "leaks":
            return _findings_rows(store, limit, offset, federated)
        raise QueryUxError(f"unsupported batch kind: {kind}")
    return _paged(getattr(store, method), symbol, limit, offset, federated)


def resolve_batch(
    store: Any,
    kind: str,
    symbols: Sequence[str],
    *,
    limit: int | None = None,
    offset: int = 0,
    fields: Any = None,
    compact: bool = False,
    federated: bool = False,
) -> dict[str, Any]:
    """Query many symbols once; per-symbol data/error, input order preserved."""

    effective_limit = int(limit) if limit is not None else DEFAULT_LIMITS.get(kind, 200)
    projected_fields = parse_fields(fields) if fields is not None else []
    if compact and kind != "detail":
        raise QueryUxError("--compact only applies to detail")
    items: list[dict[str, Any]] = []
    for symbol in symbols:
        try:
            if kind == "detail":
                detail = dict(store.query_function_detail(symbol))
                function = detail.get("function")
                if not function:
                    items.append({"symbol": symbol, "ok": False, "error": "not found"})
                    continue
                data = (
                    compact_detail(detail)
                    if compact
                    else project_detail(detail, projected_fields)
                    if projected_fields
                    else detail
                )
            else:
                rows = fetch_rows(
                    store, kind, symbol, effective_limit, offset, federated
                )
                data = rows
            items.append({"symbol": symbol, "ok": True, "data": data})
        except QueryUxError:
            raise
        except Exception as exc:  # isolate bad symbols, keep the batch alive
            items.append(
                {"symbol": symbol, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
            )
    return {"items": items, "count": len(items), "kind": kind, "limit": effective_limit}


# --------------------------------------------------------------------------- #
# Brief
# --------------------------------------------------------------------------- #


def _caller_names(
    rows: Sequence[dict[str, Any]], name_key: str, limit: int, direct_only: bool
) -> tuple[list[str], int, bool]:
    names: list[str] = []
    direct = 0
    seen: set[str] = set()
    for row in rows:
        if direct_only and row.get("candidate"):
            continue
        if not row.get("candidate"):
            direct += 1
        name = str(row.get(name_key) or "")
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names[:limit], direct, len(rows) >= limit


def resolve_brief(
    store: Any,
    symbol: str,
    *,
    caller_limit: int = 200,
    callee_limit: int = 200,
    name_limit: int = 5,
    condition_limit: int = 3,
) -> dict[str, Any]:
    """Summary-first payload for one symbol (<=2 KB for normal functions)."""

    detail = dict(store.query_function_detail(symbol))
    function = detail.get("function") or {}
    if not function:
        return {"symbol": symbol, "found": False}
    callers = list(store.query_callers(symbol, limit=caller_limit))
    callees = list(store.query_callees(symbol, limit=callee_limit))
    caller_names, direct_callers, callers_truncated = _caller_names(
        callers, "caller_name", name_limit, direct_only=True
    )
    callee_names, direct_callees, callees_truncated = _caller_names(
        callees, "callee_name", name_limit, direct_only=True
    )
    branches = sorted(
        detail.get("branches") or [],
        key=lambda row: (row.get("line") or 0, row.get("col") or 0),
    )
    key_conditions = [
        {
            "line": branch.get("line", 0),
            "condition": branch.get("condition", ""),
            "kind": branch.get("kind", ""),
        }
        for branch in branches[:condition_limit]
        if branch.get("condition")
    ]
    counts = {
        "callers": len(callers),
        "direct_callers": direct_callers,
        "callees": len(callees),
        "direct_callees": direct_callees,
        **{key: len(detail.get(key) or []) for key in _FACT_KEYS},
    }
    findings = detail.get("findings") or []
    brief: dict[str, Any] = {
        "symbol": symbol,
        "found": True,
        "function": {
            key: function.get(key)
            for key in (
                "id",
                "name",
                "qualified_name",
                "file",
                "line",
                "end_line",
                "return_type",
                "signature",
                "is_definition",
                "is_static",
            )
            if key in function
        },
        "deep_ready": bool(detail.get("deep_ready")),
        "counts": counts,
        "key_conditions": key_conditions,
        "callers": caller_names,
        "callees": callee_names,
        "truncated": {
            "callers": callers_truncated,
            "callees": callees_truncated,
        },
    }
    if function.get("shard"):
        brief["function"]["shard"] = function["shard"]
    if "shard" in detail:
        brief["shard"] = detail["shard"]
    if findings:
        brief["findings"] = [
            {
                key: finding.get(key)
                for key in ("kind", "severity", "message", "resource_kind")
                if key in finding
            }
            for finding in findings[:3]
        ]
    # Declaration-only matches (header declarations indexed without definition).
    if not function.get("is_definition"):
        related = list(store.query_functions(function.get("name") or symbol, limit=20))
        definitions = [row for row in related if row.get("is_definition")]
        brief["definition_missing"] = not definitions
        brief["declaration"] = {
            key: function.get(key)
            for key in ("name", "file", "line", "end_line", "signature", "return_type")
            if key in function
        }
        if "shard" in function:
            brief["declaration"]["shard"] = function["shard"]
    else:
        brief["definition_missing"] = False
    return brief


def brief_size_bytes(brief: dict[str, Any]) -> int:
    return len(json.dumps(brief, ensure_ascii=False).encode("utf-8"))
