#!/usr/bin/env python3
"""SQLite write-throughput and query-latency benchmark on synthetic records.

Data is generated in memory (functions, types, fields, calls, CFG blocks/edges,
branches, assignments, returns, resource operations, findings) and written
through the public ``ca.store.SqliteStore`` API, never by issuing SQL here. Then
the ReadStore query methods are timed to report p50/p95 latencies.

Usage::

    .venv/bin/python benchmarks/run_db_bench.py
    .venv/bin/python benchmarks/run_db_bench.py --functions 20000 --queries 200
"""

from __future__ import annotations

import argparse
import math
import random
import sys
import time
from pathlib import Path

import _common

_common.ensure_import_path()

DEFAULT_DB = _common.REPO_ROOT / ".tmp" / "agent-qa" / "db_bench.db"


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile / 100.0 * len(ordered)) - 1))
    return round(ordered[index], 3)


def _remove_db(db_path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(db_path) + suffix)
        if candidate.exists():
            candidate.unlink()


def _sloc(file: str, line: int, col: int = 1) -> "Sloc":
    from ca.core.schema import Sloc

    return Sloc(file=file, line=line, col=col, end_line=line, end_col=col + 1)


def generate_dataset(functions: int, calls_per_function: int):
    """Return ``(parse_units, analyses, row_count)`` with deterministic content."""

    from ca.core.ids import (
        assignment_id,
        branch_id,
        call_id,
        cfg_block_id,
        cfg_edge_id,
        field_id,
        function_id,
        resource_op_id,
        return_id,
        finding_id,
        type_id,
        tu_id,
    )
    from ca.core.schema import (
        AnalysisResult,
        AssignmentRecord,
        BranchRecord,
        CallRecord,
        CfgBlockRecord,
        CfgEdgeRecord,
        CompilationUnit,
        FieldRecord,
        FindingRecord,
        FunctionRecord,
        ParseResult,
        ParamRecord,
        ResourceOpRecord,
        ReturnRecord,
        TypeRecord,
    )

    rng = random.Random(20261017)
    files = [f"kernel/file_{index:03d}.c" for index in range(64)]
    rows = 0
    parse_units: list[ParseResult] = []
    analyses: list[AnalysisResult] = []
    chunk = 100

    for start in range(0, functions, chunk):
        names = [f"kernel_fn_{index:06d}" for index in range(start, min(start + chunk, functions))]
        file = files[(start // chunk) % len(files)]
        tu_id_value = tu_id(file, "kernel", ("clang", "-c", file))
        unit = CompilationUnit(
            id=tu_id_value,
            file=file,
            directory="kernel",
            repo_root=".",
            arguments=("clang", "-c", file),
            language="c",
            source="synthetic",
            driver="clang",
        )
        funcs: list[FunctionRecord] = []
        calls: list[CallRecord] = []
        types: list[TypeRecord] = []
        fields: list[FieldRecord] = []
        for offset, name in enumerate(names):
            index = start + offset
            sloc = _sloc(file, index + 1)
            fn = FunctionRecord(
                id=function_id(name=name, usr=f"c:@F@{name}", sloc=sloc, linkage="external"),
                tu_id=tu_id_value,
                name=name,
                qualified_name=name,
                usr=f"c:@F@{name}",
                kind="function",
                sloc=sloc,
                is_definition=True,
                return_type="int",
                signature="int ()",
                linkage="external",
                params=(ParamRecord(name="arg", type_spelling="int", index=0, sloc=sloc),),
            )
            funcs.append(fn)
            if index % 4 == 0:
                type_name = f"struct type_{index // 4:05d}"
                type_value = TypeRecord(
                    id=type_id(name=type_name, usr=f"c:@S@{type_name}", sloc=sloc, kind="struct"),
                    tu_id=tu_id_value,
                    name=type_name,
                    qualified_name=type_name,
                    usr=f"c:@S@{type_name}",
                    kind="struct",
                    sloc=sloc,
                    is_definition=True,
                    size_bytes=32,
                    align_bytes=8,
                )
                types.append(type_value)
                fields.append(
                    FieldRecord(
                        id=field_id(type_id_value=type_value.id, name="next", sloc=sloc, index=0),
                        tu_id=tu_id_value,
                        type_id=type_value.id,
                        name="next",
                        type_spelling="void *",
                        sloc=sloc,
                        offset_bits=0,
                        index=0,
                    )
                )
            for call_index in range(calls_per_function):
                target = (index * 7 + call_index) % max(1, functions)
                target_name = f"kernel_fn_{target:06d}"
                call_sloc = _sloc(file, index + 1, call_index + 2)
                calls.append(
                    CallRecord(
                        id=call_id(
                            caller_id=fn.id,
                            callee_usr=f"c:@F@{target_name}",
                            callee_name=target_name,
                            sloc=call_sloc,
                        ),
                        tu_id=tu_id_value,
                        caller_id=fn.id,
                        sloc=call_sloc,
                        callee_usr=f"c:@F@{target_name}",
                        callee_name=target_name,
                        callee_qualified_name=target_name,
                        kind="direct",
                        is_resolved=True,
                        arg_count=1,
                    )
                )
        parse_units.append(ParseResult(tu=unit, functions=funcs, types=types, fields=fields, calls=calls))
        rows += 1 + len(funcs) + len(types) + len(fields) + len(calls)

        for offset, fn in enumerate(funcs):
            index = start + offset
            block = cfg_block_id(fn.id, 0)
            blocks = [
                CfgBlockRecord(id=cfg_block_id(fn.id, i), function_id=fn.id, index=i,
                               kind=kind, terminator=terminator, sloc=_sloc(file, index + 1),
                               statement_count=1)
                for i, (kind, terminator) in enumerate(
                    (("entry", "unconditional"), ("branch", "conditional"), ("exit", "return"))
                )
            ]
            edges = [
                CfgEdgeRecord(
                    id=cfg_edge_id(fn.id, blocks[i].id, blocks[i + 1].id, "unconditional"),
                    function_id=fn.id,
                    src=blocks[i].id,
                    dst=blocks[i + 1].id,
                    kind="unconditional",
                )
                for i in range(len(blocks) - 1)
            ]
            branches = [
                BranchRecord(
                    id=branch_id(fn.id, _sloc(file, index + 1, i + 1), i),
                    function_id=fn.id,
                    block_id=block,
                    sloc=_sloc(file, index + 1, i + 1),
                    kind="if",
                    condition=f"x{index} > {i}",
                    var_names=(f"x{index}",),
                )
                for i in range(2)
            ]
            assignments = [
                AssignmentRecord(
                    id=assignment_id(fn.id, _sloc(file, index + 1, i + 1), i),
                    function_id=fn.id,
                    block_id=block,
                    sloc=_sloc(file, index + 1, i + 1),
                    lhs=f"x{index}",
                    rhs=str(rng.randint(0, 100)),
                    var_names=(f"x{index}",),
                )
                for i in range(2)
            ]
            returns = [
                ReturnRecord(
                    id=return_id(fn.id, _sloc(file, index + 1, 3), 0),
                    function_id=fn.id,
                    block_id=block,
                    sloc=_sloc(file, index + 1, 3),
                    expr=f"x{index}",
                    var_names=(f"x{index}",),
                )
            ]
            resource_ops = (
                [
                    ResourceOpRecord(
                        id=resource_op_id(fn.id, _sloc(file, index + 1, 4), "kmalloc", 0),
                        function_id=fn.id,
                        block_id=block,
                        sloc=_sloc(file, index + 1, 4),
                        op="acquire",
                        resource_kind="memory",
                        api_name="kmalloc",
                        target=f"ptr{index}",
                        target_vars=(f"ptr{index}",),
                        matcher="kmalloc",
                    )
                ]
                if index % 10 == 0
                else []
            )
            findings = (
                [
                    FindingRecord(
                        id=finding_id(fn.id, "leak", resource_ops[0].id),
                        function_id=fn.id,
                        kind="leak",
                        resource_kind="memory",
                        message=f"kmalloc acquiring 'ptr{index}' may leak",
                        severity="warning",
                        acquire_id=resource_ops[0].id,
                        evidence=("synthetic",),
                    )
                ]
                if resource_ops
                else []
            )
            analyses.append(
                AnalysisResult(
                    function_id=fn.id,
                    cfg_blocks=blocks,
                    cfg_edges=edges,
                    branches=branches,
                    assignments=assignments,
                    returns=returns,
                    resource_ops=resource_ops,
                    findings=findings,
                )
            )
            rows += (
                len(blocks)
                + len(edges)
                + len(branches)
                + len(assignments)
                + len(returns)
                + len(resource_ops)
                + len(findings)
            )
    return parse_units, analyses, rows


def _measure_queries(store, symbols: list[str], queries: int) -> dict[str, dict[str, float]]:
    timings: dict[str, list[float]] = {name: [] for name in (
        "functions", "callers", "callees", "branches", "assignments",
        "returns", "resource_ops", "detail", "findings",
    )}
    plan = [
        ("functions", lambda: store.query_functions(symbols[0][:12], limit=50)),
        ("callers", lambda: store.query_callers(symbols[1], limit=200)),
        ("callees", lambda: store.query_callees(symbols[2], limit=200)),
        ("branches", lambda: store.query_branches(symbols[3])),
        ("assignments", lambda: store.query_assignments(symbols[4])),
        ("returns", lambda: store.query_returns(symbols[5])),
        ("resource_ops", lambda: store.query_resource_ops(symbols[6])),
        ("detail", lambda: store.query_function_detail(symbols[7])),
        ("findings", lambda: store.query_findings(kind="leak", limit=200)),
    ]
    for _ in range(queries):
        for name, operation in plan:
            started = time.perf_counter()
            operation()
            timings[name].append((time.perf_counter() - started) * 1000.0)
    report: dict[str, dict[str, float]] = {}
    for name, values in timings.items():
        report[name] = {
            "samples": len(values),
            "p50_ms": _percentile(values, 50.0),
            "p95_ms": _percentile(values, 95.0),
            "max_ms": round(max(values), 3) if values else 0.0,
        }
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--functions", type=int, default=10000)
    parser.add_argument("--calls-per-function", type=int, default=4)
    parser.add_argument("--queries", type=int, default=100)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--out", default=None,
                        help="result JSON (default: benchmarks/results/db_bench.json)")
    args = parser.parse_args(argv)

    from ca.store import SqliteStore

    generate_started = time.perf_counter()
    parse_units, analyses, expected_rows = generate_dataset(
        args.functions, args.calls_per_function
    )
    generate_seconds = time.perf_counter() - generate_started

    db_path = Path(args.db).resolve()
    _remove_db(db_path)
    store = SqliteStore(str(db_path))
    store.open()
    write_seconds = 0.0
    try:
        store.begin()
        window_started = time.perf_counter()
        row_counter = 0
        commit_every = max(1, min(50, len(parse_units)))
        for index, parse in enumerate(parse_units):
            store.add_parse_result(parse)
            row_counter += 1 + sum(
                len(bucket)
                for bucket in (parse.functions, parse.types, parse.fields, parse.calls)
            )
            if (index + 1) % commit_every == 0 and index + 1 < len(parse_units):
                store.commit()
                store.begin()
        for analysis in analyses:
            store.add_analysis_result(analysis)
            row_counter += sum(
                len(bucket)
                for bucket in (
                    analysis.branches,
                    analysis.assignments,
                    analysis.returns,
                    analysis.resource_ops,
                    analysis.findings,
                    analysis.cfg_blocks,
                    analysis.cfg_edges,
                )
            )
        store.commit()
        write_seconds = time.perf_counter() - window_started
        finalize_started = time.perf_counter()
        store.finalize()
        finalize_seconds = time.perf_counter() - finalize_started
        db_bytes = db_path.stat().st_size if db_path.is_file() else 0
        names = [fn.name for parse in parse_units for fn in parse.functions]
        symbols = [names[(index * 37) % len(names)] for index in range(16)]
        queries_started = time.perf_counter()
        latency = _measure_queries(store, symbols, max(1, args.queries))
        query_seconds = time.perf_counter() - queries_started
    finally:
        store.close()

    total_seconds = write_seconds + finalize_seconds
    result = {
        "generated_at": _common.utc_timestamp(),
        "command": _common.command_line(),
        "machine": _common.machine_info(),
        "dataset": {
            "functions": args.functions,
            "calls_per_function": args.calls_per_function,
            "rows_expected": expected_rows,
            "rows_added": row_counter,
            "parse_results": len(parse_units),
            "analyses": len(analyses),
            "generation_seconds": round(generate_seconds, 3),
        },
        "write": {
            "seconds": round(write_seconds, 3),
            "finalize_seconds": round(finalize_seconds, 3),
            "rows_per_second": round(row_counter / write_seconds, 1) if write_seconds else 0.0,
            "db_bytes": db_bytes,
            "db_mb": round(db_bytes / 1e6, 2),
        },
        "query": {
            "iterations": max(1, args.queries),
            "total_seconds": round(query_seconds, 3),
            "latency": latency,
        },
    }
    written = _common.write_result(
        _common.resolve_result_path(args.out, "db_bench.json"), result
    )

    print(
        f"dataset: {args.functions} functions, {row_counter} rows,"
        f" generated in {generate_seconds:.2f}s"
    )
    print(
        f"write: {result['write']['rows_per_second']} rows/s in {write_seconds:.2f}s"
        f" (+finalize {finalize_seconds:.2f}s), db {result['write']['db_mb']} MB"
    )
    print(f"query latency over {max(1, args.queries)} iterations (ms):")
    for name, stats in latency.items():
        print(f"  {name:<12} p50={stats['p50_ms']:>8.3f}  p95={stats['p95_ms']:>8.3f}  max={stats['max_ms']:>8.3f}")
    print(f"result: {_common.display_path(written)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
