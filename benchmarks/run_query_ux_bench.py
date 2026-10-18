#!/usr/bin/env python3
"""Query-experience benchmark: projection/compact/brief size and batch latency.

Measures, for a real database (xv6 or an OH shard copy):

* average JSON bytes of a full ``query detail`` payload vs ``--compact`` and
  ``--fields function.name,function.line,deep_ready``;
* average JSON bytes of ``ca brief``;
* latency of ``N`` individual ``query detail`` calls vs one ``--symbols`` batch
  (in-process CLI calls, so the comparison excludes process startup).

Usage::

    .venv/bin/python benchmarks/run_query_ux_bench.py \
        --db .tmp/agent-qa/xv6_cb2.db --symbols 20 \
        --out benchmarks/results/query_ux_bench.json
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import statistics
import sys
import time
from pathlib import Path

import _common

_common.ensure_import_path()

REPO_ROOT = Path(__file__).resolve().parents[1]


def _pick_symbols(db: str, limit: int) -> list[str]:
    from ca.store import SqliteStore

    store = SqliteStore(db)
    store.open()
    try:
        names: list[str] = []
        seen: set[str] = set()
        for row in store.iter_records(kinds=("function",), limit=20000):
            if not row.get("is_definition"):
                continue
            name = str(row.get("name") or "")
            if name and name not in seen:
                seen.add(name)
                names.append(name)
            if len(names) >= limit:
                break
        return names
    finally:
        store.close()


def _run(argv: list[str], stdin_text: str | None = None) -> tuple[float, str]:
    from ca.cli.main import main

    buffer, errors = io.StringIO(), io.StringIO()
    old_stdin = sys.stdin
    if stdin_text is not None:
        sys.stdin = io.StringIO(stdin_text)
    started = time.perf_counter()
    try:
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(errors):
            code = main(argv)
    finally:
        sys.stdin = old_stdin
    elapsed = time.perf_counter() - started
    if code not in (0, 2):
        raise RuntimeError(f"{argv}: exit {code}: {errors.getvalue()[-300:]}")
    return elapsed * 1000.0, buffer.getvalue()


def _json_size(text: str) -> int:
    try:
        payload = json.loads(text)
    except ValueError:
        return len(text.encode("utf-8"))
    return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--symbols", type=int, default=20)
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    db = str(Path(args.db).expanduser())
    symbols = _pick_symbols(db, max(1, args.symbols))
    if not symbols:
        print("error: no definition symbols in database", file=sys.stderr)
        return 1

    full_sizes: list[int] = []
    compact_sizes: list[int] = []
    projected_sizes: list[int] = []
    brief_sizes: list[int] = []
    single_times: list[float] = []
    for symbol in symbols:
        _elapsed, text = _run(["query", "detail", symbol, "--db", db, "--json"])
        full_sizes.append(_json_size(text))
        _elapsed, text = _run(
            ["query", "detail", symbol, "--db", db, "--json", "--compact"]
        )
        compact_sizes.append(_json_size(text))
        _elapsed, text = _run(
            [
                "query",
                "detail",
                symbol,
                "--db",
                db,
                "--json",
                "--fields",
                "function.name,function.line,deep_ready",
            ]
        )
        projected_sizes.append(_json_size(text))
        _elapsed, text = _run(["brief", symbol, "--db", db, "--json"])
        brief_sizes.append(_json_size(text))
        elapsed_ms, _text = _run(["query", "detail", symbol, "--db", db, "--json"])
        single_times.append(elapsed_ms)

    stdin_text = "\n".join(symbols) + "\n"
    batch_ms, batch_text = _run(
        ["query", "detail", "--symbols", "-", "--db", db, "--json"], stdin_text
    )
    batch_payload = json.loads(batch_text)

    result = {
        "generated_at": _common.utc_timestamp(),
        "command": _common.command_line(),
        "machine": _common.machine_info(),
        "db": _common.display_path(Path(db)),
        "symbols": len(symbols),
        "detail_full_bytes": {
            "mean": round(statistics.mean(full_sizes), 1),
            "max": max(full_sizes),
        },
        "detail_compact_bytes": {
            "mean": round(statistics.mean(compact_sizes), 1),
            "max": max(compact_sizes),
        },
        "detail_projected_bytes": {
            "mean": round(statistics.mean(projected_sizes), 1),
            "max": max(projected_sizes),
        },
        "brief_bytes": {
            "mean": round(statistics.mean(brief_sizes), 1),
            "max": max(brief_sizes),
        },
        "single_detail_ms": {
            "mean": round(statistics.mean(single_times), 2),
            "total": round(sum(single_times), 2),
        },
        "batch_ms": round(batch_ms, 2),
        "batch_items": batch_payload.get("count", 0),
        "batch_vs_single_speedup": round(sum(single_times) / batch_ms, 2)
        if batch_ms
        else 0.0,
        "targets": {
            "compact_max_5kb": max(compact_sizes) <= 5000,
            "brief_max_2kb": max(brief_sizes) <= 2048,
        },
    }
    target = _common.resolve_result_path(args.out, "query_ux_bench.json")
    _common.write_result(target, result)
    print(
        f"detail full mean={result['detail_full_bytes']['mean']}B;"
        f" compact mean={result['detail_compact_bytes']['mean']}B"
        f" max={result['detail_compact_bytes']['max']}B;"
        f" projected mean={result['detail_projected_bytes']['mean']}B"
    )
    print(
        f"brief mean={result['brief_bytes']['mean']}B max={result['brief_bytes']['max']}B;"
        f" single detail total={result['single_detail_ms']['total']}ms"
        f" vs batch={result['batch_ms']}ms"
        f" ({result['batch_vs_single_speedup']}x)"
    )
    print(f"targets: {result['targets']}; result: {_common.display_path(target)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
