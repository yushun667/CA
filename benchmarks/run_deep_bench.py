#!/usr/bin/env python3
"""Layered-indexing benchmark: L0 base index cost, L1 ensure_deep latency.

Measures on a real corpus:

* L0 base index: wall/DB/rows with ``--layer base`` semantics (unique files,
  symbols/types/calls only, no analysis);
* deep coverage before/after ``ensure_deep`` on a sample of function symbols;
* L1 first-call latency (parse+analyze+write) vs second-call cache-hit latency;
* per-symbol fact yield and error rate.

Usage::

    .venv/bin/python benchmarks/run_deep_bench.py \
        --repo .tmp/corpus/xv6-riscv \
        --compdb .tmp/corpus/xv6-riscv/compile_commands.json \
        --symbols 10 --jobs 2
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import _common

_common.ensure_import_path()

DEFAULT_DB = _common.REPO_ROOT / ".tmp" / "agent-qa" / "deep_bench.db"


def _pick_symbols(db_path: Path, limit: int) -> list[str]:
    from ca.store import SqliteStore

    store = SqliteStore(str(db_path))
    store.open()
    try:
        records = list(store.iter_records(kinds=("function",), limit=20000))
        symbols: list[str] = []
        seen: set[str] = set()
        for record in records:
            if not record.get("is_definition"):
                continue
            name = str(record.get("name") or "")
            if not name or name in seen:
                continue
            seen.add(name)
            symbols.append(name)
            if len(symbols) >= limit:
                break
        return symbols
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--compdb", required=True)
    parser.add_argument("--symbols", type=int, default=20,
                        help="number of function symbols to ensure_deep (default 20)")
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--max-tus", type=int, default=None)
    parser.add_argument("--db-dir", default=str(_common.REPO_ROOT / ".tmp" / "agent-qa"))
    parser.add_argument("--out", default=None,
                        help="result JSON (default: benchmarks/results/deep_bench.json)")
    args = parser.parse_args(argv)

    from ca.build import load_compilation_database
    from ca.cli import deep as deep_module
    from ca.pipeline import run_index
    from ca.store import SqliteStore

    repo = Path(args.repo).resolve()
    compdb = Path(args.compdb).resolve()
    if not repo.is_dir() or not compdb.is_file():
        print(f"error: repo/compdb not found: {repo} {compdb}", file=sys.stderr)
        return 1
    units = list(load_compilation_database(str(compdb), str(repo)))
    if args.max_tus is not None:
        units = units[: args.max_tus]
    if not units:
        print("error: no translation units", file=sys.stderr)
        return 1

    db = Path(args.db_dir) / "deep_bench.db"
    for suffix in ("", "-wal", "-shm", ".deep.json", ".deep.queue.json"):
        candidate = Path(str(db) + suffix)
        if candidate.exists():
            candidate.unlink()
    db.parent.mkdir(parents=True, exist_ok=True)

    base_started = time.perf_counter()
    base = run_index(
        str(repo),
        units,
        str(db),
        jobs=args.jobs,
        analyze_scope="none",
        record_scope="all",
        dedupe_files=True,
    )
    base_wall = time.perf_counter() - base_started
    deep_module.set_index_mode(str(db), None, "base")
    base_row = {
        "tus_total": base.tus_total,
        "tus_parsed": base.tus_parsed,
        "tus_failed": base.tus_failed,
        "wall_seconds": round(base_wall, 3),
        "records_seconds": round(base.total_seconds, 3),
        "functions": base.functions,
        "types": base.types,
        "calls": base.calls,
        "db_bytes": db.stat().st_size if db.is_file() else 0,
        "files": base.files,
        "lines_of_code": base.lines_of_code,
    }

    symbols = _pick_symbols(db, max(1, args.symbols))
    late_started = time.perf_counter()
    first = deep_module.ensure_deep(str(db), str(repo), str(compdb), symbols, jobs=args.jobs)
    second = deep_module.ensure_deep(str(db), str(repo), str(compdb), symbols, jobs=args.jobs)
    ensure_wall = time.perf_counter() - late_started
    store = SqliteStore(str(db))
    store.open()
    try:
        stats = deep_module.deep_stats(str(db), store)
        total_facts = 0
        for result in first["results"]:
            total_facts += sum((result.get("facts") or {}).values())
    finally:
        store.close()

    facts_by_kind: dict[str, int] = {}
    for result in first["results"]:
        for key, value in (result.get("facts") or {}).items():
            facts_by_kind[key] = facts_by_kind.get(key, 0) + int(value)

    result = {
        "generated_at": _common.utc_timestamp(),
        "command": _common.command_line(),
        "machine": _common.machine_info(),
        "corpus": {
            "repo": _common.display_path(repo),
            "compdb": _common.display_path(compdb),
            "compdb_sha256": _common.sha256_file(compdb),
            "tus_loaded": len(units),
        },
        "base_layer": base_row,
        "deep_layer": {
            "symbols_requested": len(symbols),
            "first": first["metrics"],
            "second": second["metrics"],
            "ensure_wall_seconds": round(ensure_wall, 3),
            "facts_total": total_facts,
            "facts_by_kind": facts_by_kind,
            "coverage_after": stats.get("coverage"),
            "deep_ready_after": stats.get("deep_ready"),
            "total_functions": stats.get("total_functions"),
        },
    }
    written = _common.write_result(
        _common.resolve_result_path(args.out, "deep_bench.json"), result
    )
    b = base_row
    d = result["deep_layer"]
    print(
        f"L0 base: {b['tus_parsed']}/{b['tus_total']} TUs, {b['wall_seconds']}s wall,"
        f" {b['functions']} functions, {b['db_bytes'] / 1e6:.1f} MB DB, deep_ready=0"
    )
    print(
        f"L1 ensure_deep: {len(symbols)} symbols, first {d['first']['first_latency_ms']}ms"
        f" (parse {d['first']['parse_ms']}ms, analyze {d['first']['analyze_ms']}ms,"
        f" write {d['first']['write_ms']}ms), hit {d['second']['hit_latency_ms']}ms,"
        f" computed {d['first']['computed']}, errors {d['first']['errors']},"
        f" facts {d['facts_total']}, coverage {d['coverage_after']}"
    )
    print(f"result: {_common.display_path(written)}")
    return 0 if b["tus_parsed"] > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
