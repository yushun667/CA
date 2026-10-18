#!/usr/bin/env python3
"""Parse/index throughput benchmark on a real build corpus (xv6 by default).

Measures end-to-end indexing: compile-database load, multi-process parse,
CFG/data-flow analysis, SQLite writes and finalize. Parse throughput is based on
the CPU seconds summed across workers; wall time and peak memory are reported
next to it so a slow disk cannot masquerade as slow parsing.

Usage::

    .venv/bin/python benchmarks/run_parse_bench.py
    .venv/bin/python benchmarks/run_parse_bench.py --jobs 8 --max-tus 20
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from collections import Counter
from pathlib import Path

import _common

_common.ensure_import_path()

DEFAULT_REPO = _common.REPO_ROOT / ".tmp" / "corpus" / "xv6-riscv"
DEFAULT_DB = _common.REPO_ROOT / ".tmp" / "agent-qa" / "parse_bench.db"
DEFAULT_CACHE = _common.REPO_ROOT / ".tmp" / "agent-qa" / "parse_bench-cache"


def _peak_memory_mb() -> dict[str, float]:
    import resource

    # macOS reports ru_maxrss in bytes, Linux in KiB.
    scale = 1 / 1_000_000.0 if sys.platform == "darwin" else 1 / 1024.0
    return {
        "parent_peak_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale, 1),
        "child_peak_mb": round(
            resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * scale, 1
        ),
    }


def _remove_db(db_path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(db_path) + suffix)
        if candidate.exists():
            candidate.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=str(DEFAULT_REPO))
    parser.add_argument("--compdb", default=None,
                        help="compile_commands.json (default: <repo>/compile_commands.json)")
    parser.add_argument("--jobs", type=int, default=8)
    parser.add_argument("--max-tus", type=int, default=None)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--incremental", action="store_true",
                        help="also measure a warm incremental re-index after the cold run")
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE),
                        help="incremental cache directory (default: .tmp/agent-qa/parse_bench-cache)")
    parser.add_argument("--out", default=None,
                        help="result JSON (default: benchmarks/results/parse_bench.json)")
    args = parser.parse_args(argv)

    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        print(f"error: corpus repo not found: {repo}", file=sys.stderr)
        return 1
    compdb = Path(args.compdb).resolve() if args.compdb else repo / "compile_commands.json"
    if not compdb.is_file():
        print(
            f"error: compile database not found: {compdb}\n"
            "       build the corpus first (see .tmp/corpus/xv6-riscv) or pass --compdb",
            file=sys.stderr,
        )
        return 1

    from ca.build import load_compilation_database
    from ca.pipeline import run_index

    load_started = time.perf_counter()
    units = list(load_compilation_database(str(compdb), str(repo)))
    load_seconds = time.perf_counter() - load_started
    if not units:
        print(f"error: no translation units in {compdb}", file=sys.stderr)
        return 1

    db_path = Path(args.db).resolve()
    _remove_db(db_path)
    cache_dir = Path(args.cache_dir).resolve() if args.incremental else None
    if cache_dir is not None and cache_dir.exists():
        shutil.rmtree(cache_dir)
    cache_stats: dict[str, object] = {}
    summary = run_index(
        str(repo),
        units,
        str(db_path),
        jobs=args.jobs,
        max_tus=args.max_tus,
        cache_dir=str(cache_dir) if cache_dir else None,
        cache_stats=cache_stats,
    )
    incremental: dict[str, object] | None = None
    if cache_dir is not None:
        cold_total = summary.total_seconds
        cold_parse = summary.parse_seconds
        cold_analyze = summary.analyze_seconds
        cold_seconds = time.perf_counter()
        _remove_db(db_path)
        warm = run_index(
            str(repo),
            units,
            str(db_path),
            jobs=args.jobs,
            max_tus=args.max_tus,
            cache_dir=str(cache_dir),
            cache_stats=cache_stats,
        )
        warm_seconds = time.perf_counter() - cold_seconds
        incremental = {
            "cache_dir": _common.display_path(cache_dir),
            "hits": cache_stats.get("hits"),
            "misses": cache_stats.get("misses"),
            "hit_rate": cache_stats.get("hit_rate"),
            "cold_total_seconds": round(cold_total, 3),
            "cold_parse_seconds_cpu": round(cold_parse, 3),
            "cold_analyze_seconds_cpu": round(cold_analyze, 3),
            "warm_total_seconds": round(warm.total_seconds, 3),
            "warm_parse_seconds_cpu": round(warm.parse_seconds, 3),
            "warm_analyze_seconds_cpu": round(warm.analyze_seconds, 3),
            "warm_index_wall_seconds": round(warm_seconds, 3),
            "speedup": round(cold_total / warm.total_seconds, 2) if warm.total_seconds else 0.0,
            "counts_match": all(
                getattr(warm, field) == getattr(summary, field)
                for field in (
                    "tus_parsed",
                    "functions",
                    "types",
                    "calls",
                    "branches",
                    "assignments",
                    "returns",
                    "resource_ops",
                    "findings",
                    "files",
                    "lines_of_code",
                )
            ),
        }

    parsed = summary.tus_parsed
    parse_seconds = summary.parse_seconds
    analyze_seconds = summary.analyze_seconds
    total_seconds = summary.total_seconds
    metrics: dict[str, object] = {
        "tus_total": summary.tus_total,
        "tus_parsed": parsed,
        "tus_failed": summary.tus_failed,
        "functions": summary.functions,
        "types": summary.types,
        "calls": summary.calls,
        "analyzed_functions": summary.analyzed_functions,
        "branches": summary.branches,
        "assignments": summary.assignments,
        "returns": summary.returns,
        "resource_ops": summary.resource_ops,
        "findings": summary.findings,
        "files": summary.files,
        "lines_of_code": summary.lines_of_code,
        "load_compdb_seconds": round(load_seconds, 3),
        "parse_seconds_cpu": round(parse_seconds, 3),
        "analyze_seconds_cpu": round(analyze_seconds, 3),
        "write_seconds": round(summary.write_seconds, 3),
        "total_seconds": round(total_seconds, 3),
        "tu_per_second_parse_cpu": round(parsed / parse_seconds, 2) if parse_seconds else 0.0,
        "tu_per_second_wall": round(parsed / total_seconds, 2) if total_seconds else 0.0,
        "loc_per_second_parse_cpu": (
            round(summary.lines_of_code / parse_seconds, 1) if parse_seconds else 0.0
        ),
        "seconds_per_tu_parse_cpu": round(parse_seconds / parsed, 4) if parsed else 0.0,
        "db_bytes": db_path.stat().st_size if db_path.is_file() else 0,
        "errors": list(summary.errors[:10]),
        "error_count": len(summary.errors),
    }
    metrics.update(_peak_memory_mb())
    if incremental is not None:
        metrics["incremental_speedup"] = incremental["speedup"]
        metrics["incremental_hit_rate"] = incremental["hit_rate"]

    sources = Counter(unit.source for unit in units)
    languages = Counter(unit.language for unit in units)
    corpus_metadata: dict = {}
    metadata_path = compdb.parent / "metadata.json"
    if metadata_path.is_file():
        try:
            import json

            corpus_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            corpus_metadata = {}
    result = {
        "generated_at": _common.utc_timestamp(),
        "command": _common.command_line(),
        "machine": _common.machine_info(),
        "corpus": {
            "repo": _common.display_path(repo),
            "git_revision": _common.git_revision(repo),
            "compdb": _common.display_path(compdb),
            "compdb_sha256": _common.sha256_file(compdb),
            "tus_in_compdb": len(units),
            "compile_argument_sources": dict(sources),
            "languages": dict(languages),
            "metadata": corpus_metadata,
        },
        "configuration": {
            "jobs": args.jobs,
            "max_tus": args.max_tus,
            "db": _common.display_path(db_path),
            "first_run": True,
            "incremental": args.incremental,
            "cache_dir": _common.display_path(cache_dir) if cache_dir else "",
        },
        "metrics": metrics,
        "incremental": incremental,
    }
    written = _common.write_result(
        _common.resolve_result_path(args.out, "parse_bench.json"), result
    )

    print(f"corpus: {_common.display_path(repo)} @ {result['corpus']['git_revision'] or '?'}")
    print(
        f"indexed {parsed}/{summary.tus_total} TUs ({summary.tus_failed} failed)"
        f" with jobs={args.jobs}"
    )
    print(
        f"parse: {metrics['tu_per_second_parse_cpu']} TU/s,"
        f" {metrics['loc_per_second_parse_cpu']} LOC/s,"
        f" {metrics['seconds_per_tu_parse_cpu'] * 1000:.1f} ms/TU (CPU sum)"
    )
    print(
        f"analysis: {summary.analyzed_functions} functions,"
        f" {metrics['analyze_seconds_cpu']}s CPU"
    )
    print(
        f"wall: {total_seconds:.2f}s (write {summary.write_seconds:.2f}s),"
        f" db {metrics['db_bytes'] / 1e6:.2f} MB,"
        f" peak memory parent {metrics['parent_peak_mb']} MB / child {metrics['child_peak_mb']} MB"
    )
    if incremental is not None:
        print(
            f"incremental: hits={incremental['hits']}/{summary.tus_parsed}"
            f" ({float(incremental['hit_rate'] or 0.0) * 100.0:.1f}%),"
            f" cold {incremental['cold_total_seconds']:.3f}s ->"
            f" warm {incremental['warm_total_seconds']:.3f}s,"
            f" speedup {incremental['speedup']}x,"
            f" counts_match={incremental['counts_match']}"
        )
    print(f"result: {_common.display_path(written)}")
    return 0 if parsed > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
