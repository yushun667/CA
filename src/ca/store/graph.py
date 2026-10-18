"""Bounded call-graph traversal over merged direct + candidate edges.

Edges come from :meth:`ca.store.queries.QueryEngine.load_forward_edges` /
``load_reverse_edges``: direct ``CallRecord`` edges (``source="call"``) plus
``call_target``/``virtual_candidate`` target-set edges. Duplicate edges are
collapsed by natural key (caller, callee, call site) with the direct edge
winning and audit ``reason`` strings joined; there are no confidence scores.

Traversal is deterministic and always bounded: nodes/edges are ordered by
``(depth, call_file, call_line, caller_id, callee_id, source)``, ``depth`` is a
hard cap, ``limit`` caps collected graph edges, and the returned graph dict has a
``truncated`` flag. Path/cycle search is a bounded depth-first walk
(``max_depth`` + ``limit``) over the same indexed edges, so a hostile or cyclic
call graph cannot make a query unbounded.
"""

from __future__ import annotations

from typing import Any, Sequence

from ca.store.queries import QueryEngine

# Safety caps for CLI callers.
MAX_GRAPH_LIMIT = 5000
MAX_PATH_DEPTH = 12


def _node(info: dict[str, Any], depth: int, direction: str) -> dict[str, Any]:
    return {
        "id": info["id"],
        "name": info["name"],
        "file": info["file"],
        "line": info["line"],
        "depth": depth,
        "direction": direction,
    }


def _root_dict(root: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": root["id"],
        "name": root["name"],
        "file": root["file"],
        "line": root["line"],
    }


class CallGraph:
    """Graph traversals backed by a :class:`QueryEngine` connection."""

    def __init__(self, engine: QueryEngine) -> None:
        self._engine = engine
        self._fn_cache: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _resolve(self, symbol: str) -> dict[str, Any] | None:
        rows = self._engine.query_functions(symbol, limit=1)
        return rows[0] if rows else None

    def _functions(self, ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        missing = [fid for fid in dict.fromkeys(ids) if fid and fid not in self._fn_cache]
        if missing:
            self._fn_cache.update(self._engine.function_summaries(missing))
        return {fid: self._fn_cache.get(fid, {}) for fid in ids}

    def _edges(self, direction: str, ids: Sequence[str], limit: int) -> list[dict[str, Any]]:
        if direction == "forward":
            return self._engine.load_forward_edges(ids, limit)
        return self._engine.load_reverse_edges(ids, limit)

    @staticmethod
    def _edge_sort_key(edge: dict[str, Any]) -> tuple[Any, ...]:
        return (
            edge.get("depth", 0),
            edge["call_file"],
            edge["call_line"],
            edge["caller_id"],
            edge["callee_id"],
            edge["source"],
        )

    # ------------------------------------------------------------------ #
    # query_call_graph
    # ------------------------------------------------------------------ #
    def query_call_graph(
        self,
        symbol: str,
        direction: str = "both",
        depth: int = 1,
        limit: int = 500,
    ) -> dict[str, Any]:
        """BFS the merged call graph around ``symbol``.

        Returns ``{root, direction, depth, nodes, edges, cycles, truncated}``:

        - ``root``: ``{id,name,file,line}`` or ``None`` when the symbol is absent;
        - ``nodes``: ``{id,name,file,line,depth,direction}`` (root depth 0);
        - ``edges``: ``{call_id,caller_id,caller_name,caller_file,caller_line,
          callee_id,callee_name,callee_file,callee_line,call_file,call_line,
          kind,is_resolved,source,candidate,reason,depth,direction}``;
        - ``cycles``: id lists found inside the collected view;
        - ``truncated``: True when the edge ``limit`` stopped the walk.
        """

        if direction not in ("forward", "reverse", "both"):
            raise ValueError("direction must be 'forward', 'reverse' or 'both'")
        if isinstance(depth, bool) or not isinstance(depth, int) or depth < 0:
            raise ValueError("depth must be a non-negative integer")
        if limit < 0:
            raise ValueError("limit must be non-negative")
        root = self._resolve(symbol)
        empty: dict[str, Any] = {
            "root": None,
            "deep_ready": False,
            "direction": direction,
            "depth": depth,
            "nodes": [],
            "edges": [],
            "cycles": [],
            "truncated": False,
        }
        if root is None or limit == 0:
            return empty
        cap = min(limit, MAX_GRAPH_LIMIT)
        root_id = root["id"]
        nodes: dict[str, dict[str, Any]] = {
            root_id: {**_node(root, 0, "root")},
        }
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
                batch = self._edges(walk, frontier, budget)
                if not batch:
                    break
                if len(batch) >= budget:
                    truncated = True
                next_frontier: list[str] = []
                for edge in batch:
                    key = (
                        edge["caller_id"],
                        edge["callee_id"],
                        edge.get("callee_name", ""),
                        edge["call_file"],
                        edge["call_line"],
                    )
                    if key in seen_edges:
                        continue
                    seen_edges.add(key)
                    neighbor = edge["callee_id"] if walk == "forward" else edge["caller_id"]
                    if not neighbor:
                        continue
                    if neighbor not in nodes:
                        info = self._functions([neighbor]).get(neighbor) or {
                            "id": neighbor,
                            "name": edge["callee_name"] if walk == "forward" else edge["caller_name"],
                            "file": "",
                            "line": 0,
                        }
                        nodes[neighbor] = _node(info, level, walk)
                    item = dict(edge)
                    item["depth"] = level
                    item["direction"] = walk
                    edges.append(item)
                    if neighbor not in visited:
                        visited.add(neighbor)
                        next_frontier.append(neighbor)
                if not next_frontier:
                    break
                frontier = next_frontier
        ordered_edges = sorted(edges, key=self._edge_sort_key)
        ordered_nodes = sorted(nodes.values(), key=lambda item: (item["depth"], item["id"]))
        ready = self._engine.deep_ready_map([node["id"] for node in ordered_nodes])
        for node in ordered_nodes:
            node["deep_ready"] = ready.get(node["id"], False)
        root_payload = _root_dict(root)
        root_payload["deep_ready"] = ready.get(root["id"], False)
        return {
            "root": root_payload,
            "deep_ready": ready.get(root["id"], False),
            "direction": direction,
            "depth": depth,
            "nodes": ordered_nodes,
            "edges": ordered_edges,
            "cycles": self._cycles_from_edges(ordered_edges, limit=20),
            "truncated": truncated,
        }

    @staticmethod
    def _cycles_from_edges(edges: Sequence[dict[str, Any]], limit: int) -> list[list[str]]:
        """Small cycle enumeration inside an already-collected edge list."""

        adjacency: dict[str, list[str]] = {}
        for edge in edges:
            caller = edge["caller_id"]
            callee = edge["callee_id"]
            if caller and callee:
                adjacency.setdefault(caller, [])
                if callee not in adjacency[caller]:
                    adjacency[caller].append(callee)
        cycles: list[list[str]] = []
        seen: set[tuple[str, ...]] = set()

        def canonical(path: list[str]) -> tuple[str, ...]:
            smallest = min(range(len(path)), key=lambda index: path[index])
            rotated = path[smallest:] + path[:smallest]
            return tuple(rotated)

        def walk(start: str, current: str, path: list[str], on_path: set[str]) -> None:
            if len(cycles) >= limit:
                return
            for neighbor in adjacency.get(current, ()):  # deterministic insertion order
                if neighbor == start:
                    key = canonical(path)
                    if key not in seen:
                        seen.add(key)
                        cycles.append(list(key))
                elif neighbor not in on_path:
                    on_path.add(neighbor)
                    path.append(neighbor)
                    walk(start, neighbor, path, on_path)
                    path.pop()
                    on_path.discard(neighbor)

        for start in sorted(adjacency):
            if len(cycles) >= limit:
                break
            walk(start, start, [start], {start})
        cycles.sort(key=lambda cycle: (len(cycle), cycle))
        return cycles

    # ------------------------------------------------------------------ #
    # query_call_paths
    # ------------------------------------------------------------------ #
    def query_call_paths(
        self,
        source: str,
        target: str | None = None,
        max_depth: int = 6,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Bounded simple paths from ``source`` (optionally ending at ``target``).

        Each result is ``{nodes, edges, length}`` with node/edge dicts in the
        same shape as :meth:`query_call_graph`. With ``target=None`` the first
        ``limit`` simple paths (any length in ``1..max_depth``) are returned;
        paths never repeat a function and search stops at ``limit``.
        """

        if limit <= 0 or max_depth <= 0:
            return []
        max_depth = min(max_depth, MAX_PATH_DEPTH)
        root = self._resolve(source)
        if root is None:
            return []
        target_id = None
        if target is not None:
            resolved = self._resolve(target)
            if resolved is None:
                return []
            target_id = resolved["id"]
        adjacency_cache: dict[str, list[dict[str, Any]]] = {}

        def outgoing(node_id: str) -> list[dict[str, Any]]:
            if node_id not in adjacency_cache:
                adjacency_cache[node_id] = self._engine.load_forward_edges([node_id], 200)
            return adjacency_cache[node_id]

        results: list[dict[str, Any]] = []

        def build(node_ids: list[str], path_edges: list[dict[str, Any]]) -> dict[str, Any]:
            infos = self._functions(node_ids)
            nodes = []
            for index, node_id in enumerate(node_ids):
                info = infos.get(node_id) or {"id": node_id, "name": "", "file": "", "line": 0}
                nodes.append(_node(info, index, "forward"))
            edges = []
            for index, edge in enumerate(path_edges):
                item = dict(edge)
                item["depth"] = index + 1
                item["direction"] = "forward"
                edges.append(item)
            return {"nodes": nodes, "edges": edges, "length": len(path_edges)}

        def dfs(node_id: str, node_ids: list[str], path_edges: list[dict[str, Any]]) -> None:
            if len(results) >= limit:
                return
            if len(path_edges) >= max_depth:
                return
            for edge in outgoing(node_id):
                neighbor = edge["callee_id"]
                if not neighbor or neighbor in node_ids:
                    continue
                next_edges = [*path_edges, edge]
                next_ids = [*node_ids, neighbor]
                if target_id is not None:
                    if neighbor == target_id:
                        results.append(build(next_ids, next_edges))
                        if len(results) >= limit:
                            return
                        continue
                    dfs(neighbor, next_ids, next_edges)
                else:
                    results.append(build(next_ids, next_edges))
                    if len(results) >= limit:
                        return
                    dfs(neighbor, next_ids, next_edges)
                if len(results) >= limit:
                    return

        dfs(root["id"], [root["id"]], [])
        return results

    # ------------------------------------------------------------------ #
    # find_call_cycles
    # ------------------------------------------------------------------ #
    def find_call_cycles(
        self,
        symbol: str,
        max_depth: int = 6,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Enumerate simple cycles reachable from ``symbol`` (bounded DFS).

        Each result is ``{nodes, edges, length}`` (same shape as
        :meth:`query_call_paths`); a cycle of length ``n`` has ``n`` edges and
        the first and last node ids differ (the closing edge is last).
        """

        if limit <= 0 or max_depth <= 0:
            return []
        max_depth = min(max_depth, MAX_PATH_DEPTH)
        root = self._resolve(symbol)
        if root is None:
            return []
        adjacency_cache: dict[str, list[dict[str, Any]]] = {}
        expansions = 0
        max_expansions = 20000

        def outgoing(node_id: str) -> list[dict[str, Any]]:
            if node_id not in adjacency_cache:
                adjacency_cache[node_id] = self._engine.load_forward_edges([node_id], 200)
            return adjacency_cache[node_id]

        cycles: list[dict[str, Any]] = []
        seen: set[tuple[str, ...]] = set()

        def canonical(ids: list[str]) -> tuple[str, ...]:
            smallest = min(range(len(ids)), key=lambda index: ids[index])
            return tuple(ids[smallest:] + ids[:smallest])

        def build(node_ids: list[str], path_edges: list[dict[str, Any]]) -> dict[str, Any]:
            infos = self._functions(node_ids)
            nodes = []
            for index, node_id in enumerate(node_ids):
                info = infos.get(node_id) or {"id": node_id, "name": "", "file": "", "line": 0}
                nodes.append(_node(info, index, "forward"))
            edges = []
            for index, edge in enumerate(path_edges):
                item = dict(edge)
                item["depth"] = index + 1
                item["direction"] = "forward"
                edges.append(item)
            return {"nodes": nodes, "edges": edges, "length": len(path_edges)}

        def dfs(node_id: str, node_ids: list[str], path_edges: list[dict[str, Any]]) -> None:
            nonlocal expansions
            if len(cycles) >= limit or expansions >= max_expansions:
                return
            if len(path_edges) >= max_depth:
                return
            for edge in outgoing(node_id):
                expansions += 1
                if expansions >= max_expansions:
                    return
                neighbor = edge["callee_id"]
                if not neighbor:
                    continue
                if neighbor in node_ids:
                    index = node_ids.index(neighbor)
                    cycle_ids = node_ids[index:]
                    if len(cycle_ids) < 2:
                        continue
                    key = canonical([*cycle_ids, neighbor])
                    if key in seen:
                        continue
                    seen.add(key)
                    cycle_edges = [*path_edges[index:], edge]
                    cycles.append(build([*cycle_ids, neighbor], cycle_edges))
                    if len(cycles) >= limit:
                        return
                    continue
                dfs(neighbor, [*node_ids, neighbor], [*path_edges, edge])
                if len(cycles) >= limit:
                    return

        dfs(root["id"], [root["id"]], [])
        cycles.sort(key=lambda cycle: (cycle["length"], [node["id"] for node in cycle["nodes"]]))
        return cycles
