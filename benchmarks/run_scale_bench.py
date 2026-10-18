#!/usr/bin/env python3
"""Large-corpus indexing scale benchmark (jobs x analyze scope).

Measures the cost of one index pass per configuration and extrapolates to the
acceptance corpus size (default: the ~70k unique OpenHarmony source files):

* wall / total seconds, parse+analyze+write CPU split from ``IndexSummary``;
* worker CPU per TU and wall per TU (the jobs target is CPU/TU);
* ParseResult pickle bytes per TU before/after the opt-in declaration filter;
* database size and persisted row counts;
* projected wall-clock ETA for the full corpus at the same job count.

``analyze_scope=all`` runs the two-phase ownership analysis: phase A parses
every TU to determine definition-file owners, phase B re-parses and analyzes
each definition once, so its reported CPU includes both parse passes.

Usage::

    .venv/bin/python benchmarks/run_scale_bench.py \
        --repo .tmp/corpus/xv6-riscv \
        --compdb .tmp/corpus/xv6-riscv/compile_commands.json \
        --jobs 8,20 --scopes main,all,none --max-tus 300
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

import _common

_common.ensure_import_path()

DEFAULT_DB_DIR = _common.REPO_ROOT / ".tmp" / "agent-qa" / "scale"
DEFAULT_FULL_FILES = 70_000
TARGET_CPU_PER_TU = 5.0
TARGET_ETA_HOURS = 12.0


def _parse_list(value: str, cast: type) -> list:
    return [cast(item.strip()) for item in value.split(",") if item.strip()]


def _remove_db(db_path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(db_path) + suffix)
        if candidate.exists():
            candidate.unlink()


def _run_config(
    repo: Path,
    units: list,
    jobs: int,
    analyze_scope: str,
    transport_filter: str,
    db_dir: Path,
    record_scope: str = "auto",
) -> dict:
    from ca.pipeline import run_index
    from ca.pipeline import ANALYZE_SCOPES  # noqa: F401  (documents supported values)

    db_path = db_dir / (
        f"scale_{record_scope}_{analyze_scope}_{transport_filter}_{jobs}.db"
    )
    db_dir.mkdir(parents=True, exist_ok=True)
    _remove_db(db_path)
    produced: dict[str, int] = {}
    metrics: dict[str, object] = {}
    started = time.perf_counter()
    summary = run_index(
        str(repo),
        units,
        str(db_path),
        jobs=jobs,
        produced=produced,
        analyze_scope=analyze_scope,
        record_scope=record_scope,
        transport_filter=transport_filter,
        measure_transport=True,
        metrics=metrics,
    )
    wall = time.perf_counter() - started
    phase_a_cpu = float(metrics.get("phase_a_cpu_seconds") or 0.0)
    parse_cpu = summary.parse_seconds + phase_a_cpu
    analyze_cpu = summary.analyze_seconds
    write_cpu = summary.write_seconds
    total_cpu = parse_cpu + analyze_cpu + write_cpu
    parsed = max(1, summary.tus_parsed)
    row = {
        "scope": analyze_scope,
        "record_scope": metrics.get("record_scope", record_scope),
        "record_scope_requested": record_scope,
        "transport_filter": transport_filter,
        "jobs": jobs,
        "tus_total": summary.tus_total,
        "tus_parsed": summary.tus_parsed,
        "tus_failed": summary.tus_failed,
        "wall_seconds": round(wall, 3),
        "summary_total_seconds": round(summary.total_seconds, 3),
        "parse_seconds_cpu": round(parse_cpu, 3),
        "analyze_seconds_cpu": round(analyze_cpu, 3),
        "write_seconds_cpu": round(write_cpu, 3),
        "phase_a_cpu_seconds": round(phase_a_cpu, 3),
        "phase_a_wall_seconds": metrics.get("phase_a_seconds", 0.0),
        "cpu_per_tu": round(total_cpu / parsed, 3),
        "wall_per_tu": round(wall / parsed, 3),
        "analyzed_functions": summary.analyzed_functions,
        "definitions_total": metrics.get("definitions_total"),
        "definitions_analyzed": metrics.get("definitions_analyzed"),
        "definitions_suppressed": metrics.get("definitions_suppressed"),
        "pickle_mb_per_tu": round(float(metrics.get("pickle_bytes_avg") or 0.0) / 1e6, 3),
        "transmit_mb_per_tu": round(float(metrics.get("transmit_bytes_avg") or 0.0) / 1e6, 3),
        "functions": summary.functions,
        "types": summary.types,
        "calls": summary.calls,
        "branches": summary.branches,
        "assignments": summary.assignments,
        "returns": summary.returns,
        "resource_ops": summary.resource_ops,
        "findings": summary.findings,
        "files": summary.files,
        "lines_of_code": summary.lines_of_code,
        "db_bytes": db_path.stat().st_size if db_path.is_file() else 0,
        "error_count": len(summary.errors),
        "errors_sample": list(summary.errors[:3]),
    }
    return row


def _extrapolate(rows: list[dict], full_files: int) -> None:
    for row in rows:
        row["eta_wall_hours"] = round(
            row["wall_per_tu"] * full_files / 3600.0, 2
        )
        row["eta_cpu_hours_at_jobs"] = round(
            row["cpu_per_tu"] * full_files / row["jobs"] / 3600.0, 2
        )
        row["cpu_target_ok"] = row["cpu_per_tu"] <= TARGET_CPU_PER_TU
        row["eta_target_ok"] = row["eta_wall_hours"] <= TARGET_ETA_HOURS


def _print_table(rows: list[dict]) -> None:
    header = (
        f"{'rec':<9} {'scope':<6} {'jobs':>4} {'tus':>5} {'wall':>8} {'cpu/TU':>7}"
        f" {'wall/TU':>7} {'parse':>7} {'analyze':>8} {'write':>6}"
        f" {'pickle MB':>9} {'xmit MB':>8} {'DB MB':>7} {'ETA h':>7}"
    )
    print(header)
    print("-" * len(header))
    for row in rows:
        print(
            f"{row['record_scope']:<9} {row['scope']:<6} {row['jobs']:>4} {row['tus_parsed']:>5}"
            f" {row['wall_seconds']:>7.1f}s {row['cpu_per_tu']:>7.2f}"
            f" {row['wall_per_tu']:>6.2f}s {row['parse_seconds_cpu']:>7.2f}"
            f" {row['analyze_seconds_cpu']:>8.2f} {row['write_seconds_cpu']:>6.2f}"
            f" {row['pickle_mb_per_tu']:>9.3f} {row['transmit_mb_per_tu']:>8.3f}"
            f" {row['db_bytes'] / 1e6:>7.1f} {row['eta_wall_hours']:>7.2f}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--compdb", required=True)
    parser.add_argument("--jobs", default="8,20", help="comma-separated worker counts")
    parser.add_argument("--scopes", default="main,all,none",
                        help="comma-separated analyze scopes")
    parser.add_argument("--record-scopes", default="auto",
                        help="comma-separated extraction scopes (auto/all/main_file)")
    parser.add_argument("--max-tus", type=int, default=None)
    parser.add_argument("--full-files", type=int, default=DEFAULT_FULL_FILES,
                        help=f"corpus size for ETA extrapolation (default {DEFAULT_FULL_FILES})")
    parser.add_argument("--db-dir", default=str(DEFAULT_DB_DIR))
    parser.add_argument("--out", default=None,
                        help="result JSON (default: benchmarks/results/scale_bench.json)")
    parser.add_argument("--transport-check", action="store_true",
                        help="also run scope=main with the declaration transport filter")
    args = parser.parse_args(argv)

    from ca.build import load_compilation_database

    repo = Path(args.repo).resolve()
    compdb = Path(args.compdb).resolve()
    if not repo.is_dir() or not compdb.is_file():
        print(f"error: repo/compdb not found: {repo} {compdb}", file=sys.stderr)
        return 1
    units = list(load_compilation_database(str(compdb), str(repo)))
    if args.max_tus is not None:
        units = units[: args.max_tus]
    if not units:
        print("error: no translation units loaded", file=sys.stderr)
        return 1

    jobs_list = _parse_list(args.jobs, int)
    scopes = [scope.strip() for scope in args.scopes.split(",") if scope.strip()]
    record_scopes = [
        scope.strip() for scope in args.record_scopes.split(",") if scope.strip()
    ]
    db_dir = Path(args.db_dir).resolve()
    rows: list[dict] = []
    for record_scope in record_scopes:
        for scope in scopes:
            for jobs in jobs_list:
                print(
                    f"[scale] record={record_scope} scope={scope} jobs={jobs}"
                    f" tus={len(units)} ...",
                    flush=True,
                )
                rows.append(
                    _run_config(
                        repo, units, jobs, scope, "none", db_dir, record_scope
                    )
                )
    if args.transport_check:
        jobs = max(jobs_list)
        print(f"[scale] transport check scope=main jobs={jobs} ...", flush=True)
        rows.append(
            _run_config(
                repo, units, jobs, "main", "declarations", db_dir, record_scopes[0]
            )
        )

    _extrapolate(rows, args.full_files)
    corpus_metadata: dict = {}
    metadata_path = compdb.parent / "metadata.json"
    if metadata_path.is_file():
        try:
            corpus_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            corpus_metadata = {}
    result = {
        "generated_at": _common.utc_timestamp(),
        "command": _common.command_line(),
        "machine": _common.machine_info(),
        "corpus": {
            "repo": _common.display_path(repo),
            "compdb": _common.display_path(compdb),
            "compdb_sha256": _common.sha256_file(compdb),
            "tus_loaded": len(units),
            "metadata": corpus_metadata,
        },
        "extrapolation": {
            "full_files": args.full_files,
            "target_cpu_per_tu": TARGET_CPU_PER_TU,
            "target_eta_hours": TARGET_ETA_HOURS,
        },
        "rows": rows,
    }
    written = _common.write_result(
        _common.resolve_result_path(args.out, "scale_bench.json"), result
    )
    _print_table(rows)
    print(f"\nresult: {_common.display_path(written)}")
    print(
        f"extrapolated to {args.full_files} unique files;"
        f" target: scope=main/jobs=20 cpu/TU<={TARGET_CPU_PER_TU}s,"
        f" full ETA<={TARGET_ETA_HOURS}h"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
