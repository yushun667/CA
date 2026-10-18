"""Layered indexing: L0 base index, L1 lazy deep analysis, L2 prewarm queue.

L0 (``ca index --layer base``) parses every unique source file once with
``record_scope=all`` and no CFG/data-flow analysis: symbols, types, fields,
calls and locations are immediately queryable while ``deep_ready`` is false.

L1 (``ca ensure-deep`` / MCP ``ensure_deep``) resolves function symbols, parses
only their translation units with ``record_scope=main_file``, analyzes the
requested functions, writes the analysis records through the existing store
API and marks them deep-ready. A second call for the same symbols hits the
status store and returns in ~1 ms.

L2 (``ca prewarm`` / MCP ``prewarm``) keeps a small frecency table of symbols
agents asked about and can pre-analyze the hottest pending ones in a background
thread or a separate CLI run; it never blocks ordinary queries.

Deep status persistence is delegated to ``SqliteStore`` when it exposes the
phase-3 ``deep_status`` API (storage-engine task-19). Until then a small JSON
sidecar next to the database (``<db>.deep.json``) stores the ready set and a
mode marker so the CLI/MCP contract is already stable and only the backing
store changes.
"""

from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

SIDECAR_SUFFIX = ".deep.json"
SIDECAR_VERSION = 1
_FACT_KEYS = (
    "cfg_blocks",
    "cfg_edges",
    "branches",
    "assignments",
    "returns",
    "resource_ops",
    "findings",
    "call_targets",
)


# --------------------------------------------------------------------------- #
# Deep status persistence (store API first, JSON sidecar fallback)
# --------------------------------------------------------------------------- #


def sidecar_path(db_path: str | os.PathLike[str]) -> Path:
    return Path(str(db_path) + SIDECAR_SUFFIX)


def _load_sidecar(db_path: str | os.PathLike[str]) -> dict[str, Any]:
    path = sidecar_path(db_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("version") == SIDECAR_VERSION:
            data.setdefault("mode", "mixed")
            data.setdefault("ready", {})
            return data
    except (OSError, ValueError):
        pass
    return {"version": SIDECAR_VERSION, "mode": "mixed", "ready": {}}


def _save_sidecar(db_path: str | os.PathLike[str], data: dict[str, Any]) -> None:
    path = sidecar_path(db_path)
    data["version"] = SIDECAR_VERSION
    data["updated_at"] = time.time()
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        pass


def _deep_api(store: Any, name: str) -> Any:
    method = getattr(store, name, None)
    return method if callable(method) else None


def set_index_mode(db_path: str | os.PathLike[str], store: Any, mode: str) -> None:
    """Record that the database was built by a base/mixed/full index pass."""

    api = _deep_api(store, "set_deep_mode")
    if api is not None:
        try:
            api(mode)
            return
        except Exception:
            pass
    data = _load_sidecar(db_path)
    data["mode"] = mode
    data["ready"] = {}
    _save_sidecar(db_path, data)


def deep_stats(db_path: str | os.PathLike[str], store: Any) -> dict[str, Any]:
    api = _deep_api(store, "deep_stats")
    if api is not None:
        try:
            result = dict(api())
            result.setdefault("mode", _load_sidecar(db_path).get("mode", "mixed"))
            return result
        except Exception:
            pass
    data = _load_sidecar(db_path)
    tables = {}
    try:
        tables = dict(store.stats().get("tables") or {})
    except Exception:
        tables = {}
    total = int(tables.get("functions", 0))
    ready = len(data.get("ready") or {})
    if data.get("mode") == "full":
        ready = total
    return {
        "mode": data.get("mode", "mixed"),
        "total_functions": total,
        "deep_ready": ready,
        "pending": max(0, total - ready),
        "coverage": round(ready / total, 4) if total else 0.0,
    }


def invalidate_deep(db_path: str | os.PathLike[str], store: Any, tu_ids: Sequence[str] | None = None) -> None:
    """Conservative invalidation after ``ca update`` (per-TU once store supports it)."""

    api = _deep_api(store, "invalidate_deep")
    if api is not None:
        try:
            if tu_ids is not None:
                api(tu_ids=list(tu_ids))
            return
        except Exception:
            pass
    data = _load_sidecar(db_path)
    ready = data.get("ready") or {}
    if tu_ids:
        for function_id in list(ready):
            if ready[function_id].get("tu_id") in tu_ids:
                ready.pop(function_id, None)
    else:
        ready = {}
    data["ready"] = ready
    if tu_ids is None:
        data["mode"] = "base"
    _save_sidecar(db_path, data)


def _store_ready_map(store: Any, function_ids: Sequence[str]) -> dict[str, dict[str, Any]] | None:
    api = _deep_api(store, "get_deep_status")
    if api is None:
        return None
    try:
        return {str(k): dict(v) for k, v in dict(api(list(function_ids))).items()}
    except Exception:
        return None


def _function_has_facts(store: Any, function_id: str) -> bool:
    try:
        row = store.query_function_detail(function_id)
    except Exception:
        return False
    function = (row or {}).get("function") or {}
    if not function or function.get("id") != function_id:
        return False
    return any(row.get(key) for key in ("branches", "assignments", "returns", "resource_ops", "findings"))


def deep_status_for(
    db_path: str | os.PathLike[str],
    store: Any,
    symbols: Sequence[str],
) -> dict[str, dict[str, Any]]:
    """Return {requested symbol: {function_id, deep_ready, ...}} for UI/MCP."""

    resolved: dict[str, dict[str, Any]] = {}
    function_ids: list[str] = []
    for symbol in symbols:
        try:
            function = ((store.query_function_detail(symbol) or {}).get("function") or {})
        except Exception:
            function = {}
        if not function:
            resolved[symbol] = {"function_id": "", "deep_ready": False, "found": False}
            continue
        function_id = str(function.get("id") or "")
        resolved[symbol] = {
            "function_id": function_id,
            "name": function.get("name", ""),
            "file": function.get("file", ""),
            "line": function.get("line", 0),
            "found": True,
        }
        if function_id:
            function_ids.append(function_id)

    ready_map = _store_ready_map(store, function_ids)
    sidecar = _load_sidecar(db_path)
    mode = sidecar.get("mode", "mixed")
    sidecar_ready = sidecar.get("ready") or {}
    for symbol, entry in resolved.items():
        function_id = entry.get("function_id")
        if not entry.get("found") or not function_id:
            entry["deep_ready"] = False
            continue
        if ready_map is not None:
            entry["deep_ready"] = bool((ready_map.get(function_id) or {}).get("deep_ready"))
            if ready_map.get(function_id):
                entry["tu_id"] = ready_map[function_id].get("tu_id", "")
            continue
        if function_id in sidecar_ready:
            entry["deep_ready"] = True
            entry["tu_id"] = sidecar_ready[function_id].get("tu_id", "")
        elif mode == "full":
            entry["deep_ready"] = True
        elif mode == "base":
            entry["deep_ready"] = False
        else:  # mixed: infer from existing analysis facts
            entry["deep_ready"] = _function_has_facts(store, function_id)
    return resolved


# --------------------------------------------------------------------------- #
# L1: ensure_deep
# --------------------------------------------------------------------------- #


@dataclass
class DeepResult:
    symbol: str
    function_id: str = ""
    name: str = ""
    status: str = "not_found"  # ready | computed | not_found | error | pending
    deep_ready: bool = False
    latency_ms: float = 0.0
    tu: str = ""
    facts: dict[str, int] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "function_id": self.function_id,
            "name": self.name,
            "status": self.status,
            "deep_ready": self.deep_ready,
            "latency_ms": round(self.latency_ms, 3),
            "tu": self.tu,
            "facts": self.facts,
            "error": self.error,
        }


def _analyze_tu_targets(
    unit: Any, target_ids: Sequence[str]
) -> list[tuple[str, Any | None, str]]:
    """Worker: parse one TU and analyze only ``target_ids``.

    Returns (function_id, AnalysisResult|None, error) tuples. Parse options use
    ``record_scope=main_file`` so the payload stays small; the store already has
    the base symbols from L0.
    """

    from ca.analyze import analyze_function
    from ca.parse import ParseOptions, parse_compilation_unit

    try:
        parsed = parse_compilation_unit(unit, ParseOptions(record_scope="main_file"))
    except Exception as exc:  # pragma: no cover - environment dependent
        return [(fid, None, f"parse failed: {type(exc).__name__}: {exc}") for fid in target_ids]
    by_id = {str(getattr(fn, "id", "")): fn for fn in parsed.functions}
    missing = [fid for fid in target_ids if fid not in by_id]
    if missing:
        # Header-defined inline functions are skipped by record_scope=main_file;
        # they are still valid deep-analysis targets, so retry with full records.
        try:
            full = parse_compilation_unit(unit, ParseOptions(record_scope="all"))
            by_id.update({str(getattr(fn, "id", "")): fn for fn in full.functions})
            parsed = full
        except Exception:
            pass
    results: list[tuple[str, Any | None, str]] = []
    for function_id in target_ids:
        function = by_id.get(function_id)
        if function is None:
            results.append((function_id, None, "function definition not found in TU"))
            continue
        try:
            cursor = parsed.definition_cursor(function_id)
        except Exception:
            cursor = None
        if cursor is None:
            results.append((function_id, None, "definition cursor not found"))
            continue
        try:
            results.append((function_id, analyze_function(parsed, function), ""))
        except Exception as exc:
            results.append((function_id, None, f"analyze failed: {type(exc).__name__}: {exc}"))
    return results


def _facts_of(analysis: Any) -> dict[str, int]:
    return {key: len(getattr(analysis, key, []) or []) for key in _FACT_KEYS}


def resolve_targets(store: Any, symbols: Sequence[str]) -> tuple[list[dict[str, Any]], list[DeepResult]]:
    """Resolve symbols to indexed function rows; returns (targets, failures)."""

    targets: list[dict[str, Any]] = []
    failures: list[DeepResult] = []
    seen: set[str] = set()
    for symbol in symbols:
        symbol = str(symbol)
        try:
            function = ((store.query_function_detail(symbol) or {}).get("function") or {})
        except Exception as exc:
            failures.append(DeepResult(symbol, status="error", error=str(exc)))
            continue
        if not function:
            failures.append(DeepResult(symbol, status="not_found"))
            continue
        function_id = str(function.get("id") or "")
        if not function_id or function_id in seen:
            continue
        seen.add(function_id)
        targets.append(
            {
                "symbol": symbol,
                "function_id": function_id,
                "name": function.get("name", ""),
                "tu_id": str(function.get("tu_id") or ""),
                "file": function.get("file", ""),
            }
        )
    return targets, failures


def load_units_by_id(
    repo: str,
    compdb: str,
    store: Any | None = None,
    tu_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Resolve TU ids to CompilationUnits, preferring the database over compdb.

    Loading a large shard compile database can take tens of seconds; the store
    already persisted every TU command (absolute file/directory + argv), so the
    lazy-deep path reconstructs only the TUs it needs from the ``tus`` table and
    falls back to the compile database only for missing rows.
    """

    from ca.core.schema import CompilationUnit

    units: dict[str, Any] = {}
    wanted = {str(tu_id) for tu_id in (tu_ids or []) if tu_id}
    if store is not None and wanted:
        try:
            for row in store.iter_records(kinds=("compilation_unit",)):
                row_id = str(row.get("id") or "")
                if row_id not in wanted or row_id in units:
                    continue
                arguments = row.get("arguments") or ()
                units[row_id] = CompilationUnit(
                    id=row_id,
                    file=str(row.get("file") or ""),
                    directory=str(row.get("directory") or ""),
                    repo_root=str(row.get("repo_root") or repo),
                    arguments=tuple(str(arg) for arg in arguments),
                    language=str(row.get("language") or "c"),
                    source=str(row.get("source") or "compile_commands"),
                    driver=str(row.get("driver") or ""),
                )
                if len(units) == len(wanted):
                    break
        except Exception:
            pass
    missing = wanted - set(units)
    if missing:
        from ca.build import load_compilation_database

        loaded = load_compilation_database(str(compdb), str(repo))
        units.update(
            {str(unit.id): unit for unit in loaded if str(unit.id) in missing}
        )
    return units


def ensure_deep(
    db_path: str,
    repo: str,
    compdb: str,
    symbols: Sequence[str],
    *,
    jobs: int = 1,
    batch_size: int = 64,
) -> dict[str, Any]:
    """Compute deep facts for ``symbols``; returns per-symbol results + metrics."""

    from ca.store import SqliteStore

    symbols = [str(symbol) for symbol in symbols if str(symbol)]
    started = time.perf_counter()
    store = SqliteStore(str(db_path))
    store.open()
    status = deep_status_for(str(db_path), store, symbols)
    results: list[DeepResult] = []
    pending_by_tu: dict[str, list[dict[str, Any]]] = {}
    for target in resolve_targets(store, symbols)[0]:
        entry = status.get(target["symbol"]) or {}
        if entry.get("deep_ready"):
            results.append(
                DeepResult(
                    target["symbol"],
                    function_id=target["function_id"],
                    name=target["name"],
                    status="ready",
                    deep_ready=True,
                    latency_ms=(time.perf_counter() - started) * 1000.0,
                    tu=target["tu_id"],
                    facts=(entry.get("facts") or {}),
                )
            )
        else:
            pending_by_tu.setdefault(target["tu_id"], []).append(target)
    for failure in resolve_targets(store, symbols)[1]:
        results.append(failure)

    computed = 0
    parse_wall = 0.0
    analyze_wall = 0.0
    errors = 0
    if pending_by_tu:
        units = load_units_by_id(
            repo, compdb, store=store, tu_ids=list(pending_by_tu)
        )
        items: list[tuple[str, list[str], Any, list[dict[str, Any]]]] = []
        for tu_id, targets in pending_by_tu.items():
            unit = units.get(tu_id)
            if unit is None:
                for target in targets:
                    results.append(
                        DeepResult(
                            target["symbol"],
                            function_id=target["function_id"],
                            name=target["name"],
                            status="error",
                            error=f"translation unit not in compile database: {tu_id}",
                        )
                    )
                    errors += 1
                continue
            items.append((tu_id, [t["function_id"] for t in targets], unit, targets))

        analysis_rows: list[Any] = []
        ready_records: list[dict[str, Any]] = []
        per_symbol: dict[str, DeepResult] = {}
        wall_started = time.perf_counter()
        workers = max(1, min(int(jobs), len(items)))
        if workers > 1 and len(items) > 1:
            with ProcessPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(_analyze_tu_targets, unit, ids) for _, ids, unit, _ in items]
                outcomes = [future.result() for future in futures]
        else:
            outcomes = [_analyze_tu_targets(unit, ids) for _, ids, unit, _ in items]
        parse_wall = time.perf_counter() - wall_started
        for (tu_id, _ids, _unit, targets), outcome in zip(items, outcomes):
            target_by_id = {t["function_id"]: t for t in targets}
            for function_id, analysis, error in outcome:
                target = target_by_id[function_id]
                if analysis is None:
                    errors += 1
                    results.append(
                        DeepResult(
                            target["symbol"],
                            function_id=function_id,
                            name=target["name"],
                            status="error",
                            error=error,
                            tu=tu_id,
                        )
                    )
                    continue
                started_analyze = time.perf_counter()
                analysis_rows.append(analysis)
                elapsed = (time.perf_counter() - started_analyze) * 1000.0
                analyze_wall += elapsed
                computed += 1
                result = DeepResult(
                    target["symbol"],
                    function_id=function_id,
                    name=target["name"],
                    status="computed",
                    deep_ready=True,
                    latency_ms=parse_wall * 1000.0 / max(1, sum(len(t) for _, _, _, t in items))
                    + elapsed,
                    tu=tu_id,
                    facts=_facts_of(analysis),
                )
                per_symbol[target["symbol"]] = result
                ready_records.append(
                    {
                        "function_id": function_id,
                        "tu_id": tu_id,
                        "analyzed_at": time.time(),
                        "facts": result.facts,
                    }
                )
        write_started = time.perf_counter()
        if analysis_rows:
            store.begin()
            for analysis in analysis_rows:
                store.add_analysis_result(analysis)
            store.commit()
            results.extend(per_symbol.values())
            _mark_ready(str(db_path), store, ready_records)
        write_wall = time.perf_counter() - write_started
    else:
        write_wall = 0.0

    store.close()
    total_ms = (time.perf_counter() - started) * 1000.0
    hits = sum(1 for result in results if result.status == "ready")
    computed_results = [result for result in results if result.status == "computed"]
    return {
        "requested": len(symbols),
        "results": [result.to_dict() for result in results],
        "metrics": {
            "hits": hits,
            "computed": len(computed_results),
            "not_found": sum(1 for result in results if result.status == "not_found"),
            "errors": errors,
            "hit_rate": round(hits / max(1, len(symbols)), 4),
            "parse_ms": round(parse_wall * 1000.0, 3),
            "analyze_ms": round(analyze_wall, 3),
            "write_ms": round(write_wall * 1000.0, 3),
            "total_ms": round(total_ms, 3),
            "hit_latency_ms": round(
                sum(r.latency_ms for r in results if r.status == "ready") / max(1, hits), 3
            ),
            "first_latency_ms": round(
                sum(r.latency_ms for r in computed_results) / max(1, len(computed_results)), 3
            ),
        },
    }


def _mark_failed(db_path: str, store: Any, records: Sequence[dict[str, Any]]) -> None:
    api = _deep_api(store, "mark_deep_failed")
    if api is not None:
        try:
            api(list(records))
        except Exception:
            pass


def _mark_ready(db_path: str, store: Any, records: Sequence[dict[str, Any]]) -> None:
    api = _deep_api(store, "mark_deep_ready")
    if api is not None:
        try:
            api(list(records))
            return
        except Exception:
            pass
    data = _load_sidecar(db_path)
    ready = data.get("ready") or {}
    for record in records:
        ready[str(record["function_id"])] = dict(record)
    data["ready"] = ready
    data["mode"] = "mixed"
    _save_sidecar(db_path, data)


# --------------------------------------------------------------------------- #
# L2: query-frequency prewarm queue
# --------------------------------------------------------------------------- #


class DeepQueue:
    """Small persisted frecency table + prewarm driver.

    The queue is deliberately independent from the offline index: it only
    records which symbols agents queried, then pre-analyzes the hottest pending
    ones in a bounded worker loop (CLI batch or MCP background thread).
    """

    def __init__(self, db_path: str | os.PathLike[str]) -> None:
        self.db_path = str(db_path)
        self.path = sidecar_path(db_path).with_suffix(".queue.json")
        self._lock = threading.Lock()

    def record(self, symbol: str, weight: float = 1.0) -> None:
        symbol = str(symbol)
        if not symbol:
            return
        with self._lock:
            data = self._load()
            entry = data["symbols"].setdefault(symbol, {"score": 0.0, "count": 0, "last": 0.0})
            entry["score"] = float(entry.get("score", 0.0)) + weight
            entry["count"] = int(entry.get("count", 0)) + 1
            entry["last"] = time.time()
            self._save(data)

    def top(self, limit: int = 20) -> list[str]:
        data = self._load()
        ranked = sorted(
            data["symbols"].items(),
            key=lambda item: (-float(item[1].get("score", 0.0)), -float(item[1].get("last", 0.0)), item[0]),
        )
        return [symbol for symbol, _ in ranked[: max(0, limit)]]

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("symbols"), dict):
                return data
        except (OSError, ValueError):
            pass
        return {"version": 1, "symbols": {}}

    def _save(self, data: dict[str, Any]) -> None:
        data["updated_at"] = time.time()
        temporary = self.path.with_name(self.path.name + f".{os.getpid()}.tmp")
        try:
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError:
            pass


def prewarm(
    db_path: str,
    repo: str,
    compdb: str,
    *,
    limit: int = 20,
    jobs: int = 1,
    symbols: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Analyze the hottest pending queried symbols (or an explicit list)."""

    if symbols:
        candidates = [str(symbol) for symbol in symbols][:limit]
    else:
        candidates = DeepQueue(db_path).top(limit)
    if not candidates:
        return {
            "requested": 0,
            "results": [],
            "metrics": {"hits": 0, "computed": 0, "not_found": 0, "errors": 0},
        }
    return ensure_deep(db_path, repo, compdb, candidates, jobs=jobs)


_BACKGROUND_THREADS: set[threading.Thread] = set()


def prewarm_background(
    db_path: str, repo: str, compdb: str, *, limit: int = 20, jobs: int = 1
) -> None:
    """Run :func:`prewarm` on a daemon thread (MCP background queue)."""

    def _run() -> None:
        try:
            prewarm(db_path, repo, compdb, limit=limit, jobs=jobs)
        except Exception:
            pass

    thread = threading.Thread(target=_run, name="ca-deep-prewarm", daemon=True)
    _BACKGROUND_THREADS.add(thread)
    thread.start()
