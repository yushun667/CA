"""Call-graph traversal used by ``ca graph`` and the MCP ``graph`` tool.

Preferred path is ``SqliteStore.query_call_graph`` from the phase-3 store: it
merges resolved calls with ``call_targets`` candidates and already contains the
cycle/truncation logic. When the store predates that API (or a database has no
``call_targets`` table) a BFS over ``query_callers``/``query_callees`` keeps the
command working with direct calls only; candidates then require an upgraded
store, which is reported through the normal edge fields.

Stable output schema (shared by CLI JSON, DOT and MCP):

``nodes[]``
    ``id, name, file, line, depth, direction``
``edges[]``
    ``caller_id, caller_name, caller_file, caller_line, callee_id, callee_name,
    callee_file, callee_line, call_file, call_line, call_id, kind,
    is_resolved, depth, source, candidate, cycle``
    ``source`` is ``call`` (direct) or ``call_target`` (one of possibly several
    candidate targets for an indirect call site); dashed candidate edges are a
    possible-target set, not a ranked or scored prediction.
"""

from __future__ import annotations

from typing import Any, Literal

# CLI/MCP direction values match SqliteStore.query_call_graph:
# forward = follow callees, reverse = follow callers, both = union.
DIRECTIONS = ("forward", "reverse", "both")
MAX_DEPTH = 12
DEFAULT_DEPTH = 1
DEFAULT_LIMIT = 500
MAX_LIMIT = 5000

Direction = Literal["forward", "reverse", "both"]


class GraphError(ValueError):
    """Invalid graph request (bad direction, depth or limit)."""


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def validate_request(
    direction: str, depth: Any, limit: Any
) -> tuple[Direction, int, int]:
    if direction not in DIRECTIONS:
        raise GraphError(
            f"direction must be one of {', '.join(DIRECTIONS)}, got {direction!r}"
        )
    depth_value = _coerce_int(depth, DEFAULT_DEPTH)
    if depth_value < 0 or depth_value > MAX_DEPTH:
        raise GraphError(f"depth must be between 0 and {MAX_DEPTH}, got {depth}")
    limit_value = _coerce_int(limit, DEFAULT_LIMIT)
    if limit_value < 1 or limit_value > MAX_LIMIT:
        raise GraphError(f"limit must be between 1 and {MAX_LIMIT}, got {limit}")
    return direction, depth_value, limit_value  # type: ignore[return-value]


def resolve_root(store: Any, symbol: str) -> dict[str, Any] | None:
    """Best-effort resolution of ``symbol`` to a known function row."""

    try:
        rows = list(store.query_functions(symbol, limit=20))
    except Exception:
        return None
    for row in rows:
        if symbol in (row.get("id"), row.get("name"), row.get("qualified_name")):
            return row
    return rows[0] if rows else None


def build_call_graph(
    store: Any,
    symbol: str,
    *,
    direction: str = "callee",
    depth: Any = DEFAULT_DEPTH,
    limit: Any = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Build the normalized call graph rooted at ``symbol``."""

    direction, depth, limit = validate_request(direction, depth, limit)
    if not symbol:
        raise GraphError("symbol must not be empty")

    raw: dict[str, Any] | None = None
    method = getattr(store, "query_call_graph", None)
    if callable(method):
        raw = _call_graph_method(method, symbol, direction, depth, limit)
    if raw is None:
        raw = _fallback_graph(store, symbol, direction, depth, limit)
    return _normalize(raw, symbol, direction, depth, limit)


def _call_graph_method(
    method: Any, symbol: str, direction: str, depth: int, limit: int
) -> dict[str, Any] | None:
    try:
        return dict(method(symbol, direction=direction, depth=depth, limit=limit))
    except TypeError:
        try:
            return dict(method(symbol, direction, depth, limit))
        except Exception:
            return None
    except Exception:
        return None


def _normalize(
    raw: dict[str, Any], symbol: str, direction: str, depth: int, limit: int
) -> dict[str, Any]:
    nodes = [dict(node) for node in raw.get("nodes") or []]
    edges = [dict(edge) for edge in raw.get("edges") or []]
    nodes.sort(
        key=lambda node: (
            _coerce_int(node.get("depth"), 0),
            str(node.get("name") or ""),
            str(node.get("id") or ""),
        )
    )
    edges.sort(
        key=lambda edge: (
            str(edge.get("caller_name") or edge.get("caller_id") or ""),
            str(edge.get("callee_name") or edge.get("callee_id") or ""),
            _coerce_int(edge.get("depth"), 0),
            str(edge.get("source") or "call"),
        )
    )
    for edge in edges:
        source = str(edge.get("source") or "direct")
        if source == "call":  # tolerate older store rows
            source = "direct"
        edge["source"] = source
        candidate = source in ("candidate", "call_target", "virtual_candidate")
        edge.setdefault(
            "kind", "indirect_candidate" if candidate else "direct"
        )
        edge["candidate"] = candidate
        edge.setdefault("cycle", False)
    cycles = raw.get("cycles") or []
    root_value = raw.get("root") or symbol
    if isinstance(root_value, dict):
        root_value = root_value.get("name") or root_value.get("id") or symbol
    return {
        "root": str(root_value),
        "direction": str(raw.get("direction") or direction),
        "depth": _coerce_int(raw.get("depth"), depth),
        "limit": limit,
        "nodes": nodes,
        "edges": edges,
        "cycles": cycles,
        "truncated": bool(raw.get("truncated")),
        "stats": {"nodes": len(nodes), "edges": len(edges), "cycles": len(cycles)},
    }


# --------------------------------------------------------------------------- #
# Fallback BFS over direct calls (pre-phase-3 stores / databases)
# --------------------------------------------------------------------------- #


def _edge_from_row(
    row: dict[str, Any], caller: str, callee: str, depth: int, source: str
) -> dict[str, Any]:
    return {
        "caller_id": str(row.get("caller_id") or ""),
        "caller_name": str(row.get("caller_name") or caller),
        "caller_file": str(row.get("caller_file") or ""),
        "caller_line": _coerce_int(row.get("caller_line"), 0),
        "callee_id": str(row.get("callee_id") or ""),
        "callee_name": str(row.get("callee_name") or callee),
        "callee_file": str(row.get("callee_file") or ""),
        "callee_line": _coerce_int(row.get("callee_line"), 0),
        "call_file": str(row.get("call_file") or ""),
        "call_line": _coerce_int(row.get("call_line"), 0),
        "call_id": str(row.get("call_id") or ""),
        "kind": str(row.get("kind") or "direct"),
        "is_resolved": bool(row.get("is_resolved", True)),
        "depth": depth,
        "source": source,
        "candidate": source == "candidate",
    }


def _ancestor_path(parent: dict[str, str | None], name: str) -> list[str]:
    path = [name]
    seen = {name}
    while parent.get(path[-1]):
        ancestor = parent[path[-1]]
        if ancestor in seen:  # defensive: never loop forever
            break
        path.append(ancestor)
        seen.add(ancestor)
    return path


def _fallback_graph(
    store: Any, symbol: str, direction: str, depth: int, limit: int
) -> dict[str, Any]:
    root_row = resolve_root(store, symbol)
    root_name = str((root_row or {}).get("name") or symbol)
    nodes: dict[str, dict[str, Any]] = {
        root_name: {
            "id": str((root_row or {}).get("id") or ""),
            "name": root_name,
            "file": str((root_row or {}).get("file") or ""),
            "line": _coerce_int((root_row or {}).get("line"), 0),
            "depth": 0,
            "direction": direction,
        }
    }
    edges: dict[tuple[str, ...], dict[str, Any]] = {}
    cycles: list[dict[str, Any]] = []
    parent: dict[str, str | None] = {root_name: None}
    frontier: list[tuple[str, int]] = [(root_name, 0)]
    truncated = False

    def add_edge(edge: dict[str, Any], neighbour: str, depth_value: int) -> None:
        nonlocal truncated
        key = (
            edge["caller_name"],
            edge["callee_name"],
            edge["call_file"] or edge["call_id"],
            str(edge["call_line"]),
            edge["source"],
            str(edge["kind"]),
        )
        if key in edges:
            return
        edges[key] = edge
        if neighbour in nodes:
            # A back edge to an already-visited node: mark it and record the cycle.
            if neighbour in parent and _reachable(parent, neighbour, edge["caller_name"]):
                edge["cycle"] = True
                cycles.append(
                    {
                        "nodes": [
                            edge["callee_name"],
                            edge["caller_name"],
                            neighbour,
                        ][:3],
                        "length": 1,
                    }
                )
            return
        if len(nodes) >= limit:
            truncated = True
            return
        nodes[neighbour] = {
            "id": edge["callee_id"] if edge["callee_name"] == neighbour else edge["caller_id"],
            "name": neighbour,
            "file": edge["callee_file"] if edge["callee_name"] == neighbour else edge["caller_file"],
            "line": edge["callee_line"] if edge["callee_name"] == neighbour else edge["caller_line"],
            "depth": depth_value,
            "direction": direction,
        }
        parent[neighbour] = edge["caller_name"] if edge["callee_name"] == neighbour else edge["callee_name"]

    while frontier and not truncated:
        next_frontier: list[tuple[str, int]] = []
        for name, current_depth in frontier:
            if current_depth >= depth:
                continue
            if direction in ("forward", "both"):
                rows = store.query_callees(name, limit=limit)
                for row in rows:
                    callee = str(row.get("callee_name") or row.get("callee_id") or "")
                    if not callee:
                        continue
                    edge = _edge_from_row(
                        row,
                        name,
                        callee,
                        current_depth + 1,
                        str(row.get("source") or "direct"),
                    )
                    add_edge(edge, callee, current_depth + 1)
                    if callee in nodes and nodes[callee]["depth"] == current_depth + 1:
                        next_frontier.append((callee, current_depth + 1))
            if direction in ("reverse", "both"):
                rows = store.query_callers(name, limit=limit)
                for row in rows:
                    caller = str(row.get("caller_name") or row.get("caller_id") or "")
                    if not caller:
                        continue
                    edge = _edge_from_row(
                        row,
                        caller,
                        name,
                        current_depth + 1,
                        str(row.get("source") or "direct"),
                    )
                    add_edge(edge, caller, current_depth + 1)
                    if caller in nodes and nodes[caller]["depth"] == current_depth + 1:
                        next_frontier.append((caller, current_depth + 1))
        seen_frontier: set[str] = set()
        frontier = []
        for item in next_frontier:
            if item[0] in seen_frontier:
                continue
            seen_frontier.add(item[0])
            frontier.append(item)

    return {
        "root": root_name,
        "direction": direction,
        "depth": depth,
        "nodes": list(nodes.values()),
        "edges": list(edges.values()),
        "cycles": cycles,
        "truncated": truncated,
    }


def _reachable(parent: dict[str, str | None], target: str, start: str) -> bool:
    """True when ``target`` is an ancestor of ``start`` in the BFS tree."""

    path = _ancestor_path(parent, start)
    return target in path


# --------------------------------------------------------------------------- #
# Rendering helpers
# --------------------------------------------------------------------------- #


def to_dot(graph: dict[str, Any], title: str = "ca_call_graph") -> str:
    """Render a normalized graph as Graphviz DOT."""

    def quote(value: Any) -> str:
        text = str(value or "")
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'

    lines = [f"digraph {title} {{", "  rankdir=LR;", "  node [shape=box, fontsize=10];"]
    for node in graph.get("nodes") or []:
        label = node.get("name") or node.get("id") or ""
        if node.get("depth"):
            label = f"{label}\\n(depth {node.get('depth')})"
        lines.append(f"  {quote(node.get('name') or node.get('id'))} [label={quote(label)}];")
    for edge in graph.get("edges") or []:
        attributes = []
        if edge.get("candidate") or edge.get("source") in ("candidate", "call_target"):
            attributes.append('style=dashed')
            attributes.append(f"label={quote(edge.get('kind'))}")
        elif edge.get("kind") and edge.get("kind") != "direct":
            attributes.append(f"label={quote(edge.get('kind'))}")
        if edge.get("cycle"):
            attributes.append("color=red")
        source = quote(edge.get("caller_name") or edge.get("caller_id"))
        target = quote(edge.get("callee_name") or edge.get("callee_id"))
        suffix = f" [{', '.join(attributes)}]" if attributes else ""
        lines.append(f"  {source} -> {target}{suffix};")
    lines.append("}")
    return "\n".join(lines) + "\n"


def nodes_table_rows(graph: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "name": node.get("name"),
            "depth": node.get("depth"),
            "file": node.get("file"),
            "line": node.get("line"),
        }
        for node in graph.get("nodes") or []
    ]


def edges_table_rows(graph: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "caller": edge.get("caller_name"),
            "callee": edge.get("callee_name"),
            "source": edge.get("source"),
            "kind": edge.get("kind"),
            "resolved": edge.get("is_resolved"),
            "call_file": edge.get("call_file"),
            "call_line": edge.get("call_line"),
            "cycle": edge.get("cycle"),
        }
        for edge in graph.get("edges") or []
    ]
