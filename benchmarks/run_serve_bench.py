#!/usr/bin/env python3
"""Benchmark the ``ca serve`` HTTP backend: query/MCP latency + job loop.

Starts an in-process backend on an ephemeral port against a real database and a
temporary state DB, then measures:

* ``GET /api/health|search|stats`` and ``GET /api/jobs`` p50/p95;
* MCP ``/mcp`` initialize + ``detail`` tool call p50/p95;
* cross-client loop latency: HTTP create -> MCP claim -> MCP submit -> done;
* SSE event delivery for the job lifecycle.

Usage::

    .venv/bin/python benchmarks/run_serve_bench.py \
        --db .tmp/agent-qa/xv6_cb2.db --out benchmarks/results/server_bench.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
import urllib.request
from pathlib import Path

import _common

_common.ensure_import_path()

REPO_ROOT = Path(__file__).resolve().parents[1]


def _measure(fn, n: int = 20) -> dict[str, float]:
    times: list[float] = []
    for _ in range(n):
        started = time.perf_counter()
        fn()
        times.append((time.perf_counter() - started) * 1000.0)
    times.sort()
    return {
        "n": n,
        "p50_ms": round(statistics.median(times), 2),
        "p95_ms": round(times[min(len(times) - 1, int(len(times) * 0.95))], 2),
        "max_ms": round(max(times), 2),
    }


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _post(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        body = response.read().decode("utf-8")
    return json.loads(body) if body else {}


def _mcp(base: str, messages: list[dict]) -> list[dict]:
    request = urllib.request.Request(
        base + "/mcp",
        data=json.dumps(messages).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def _envelope(response: dict) -> dict:
    return json.loads(response["result"]["content"][0]["text"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--symbol", default="kfree")
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    from ca.server import ServeConfig, make_server

    db = str(Path(args.db).expanduser())
    state_db = Path(_common.resolve_result_path(args.out, "server_bench.json")).parent / "serve-bench-state.db"
    state_db.parent.mkdir(parents=True, exist_ok=True)
    if state_db.exists():
        state_db.unlink()
    server = make_server(
        ServeConfig(db_values=[db], host="127.0.0.1", port=0, state_db=str(state_db))
    )
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
    )
    thread.start()
    base = server.url
    try:
        results: dict[str, object] = {
            "generated_at": _common.utc_timestamp(),
            "command": _common.command_line(),
            "machine": _common.machine_info(),
            "db": _common.display_path(Path(db)),
            "http": {
                "health": _measure(lambda: _get(base + "/api/health")),
                "search": _measure(lambda: _get(base + "/api/search?q=" + args.symbol)),
                "stats": _measure(lambda: _get(base + "/api/stats")),
                "jobs_list": _measure(lambda: _get(base + "/api/jobs?limit=50")),
            },
            "mcp": {},
            "cross_client": {},
        }
        _mcp(base, [{"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "bench", "version": "0"}}}])
        results["mcp"]["detail"] = _measure(
            lambda: _mcp(
                base,
                [{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "detail", "arguments": {"symbol": args.symbol}}}],
            )
        )
        results["mcp"]["tools_list"] = _measure(
            lambda: _mcp(base, [{"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}])
        )

        # Cross-client loop: HTTP create -> MCP claim -> MCP submit -> done.
        loop_times: list[float] = []
        for index in range(5):
            started = time.perf_counter()
            created = _post(
                base + "/api/jobs",
                {"type": "explain", "symbol": args.symbol, "payload": {"round": index}},
            )
            job_id = created["data"]["job"]["id"]
            claimed = _envelope(
                _mcp(
                    base,
                    [{"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "explain_jobs", "arguments": {"action": "claim", "worker": "bench"}}}],
                )[0]
            )
            assert claimed["ok"] and claimed["data"]["job"]["id"] == job_id, claimed
            submitted = _envelope(
                _mcp(
                    base,
                    [{"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "submit_explanation", "arguments": {"job_id": job_id, "explanation": "bench explanation", "worker": "bench"}}}],
                )[0]
            )
            assert submitted["ok"] and submitted["data"]["job"]["status"] == "done"
            loop_times.append((time.perf_counter() - started) * 1000.0)
        loop_times.sort()
        results["cross_client"] = {
            "n": len(loop_times),
            "p50_ms": round(statistics.median(loop_times), 2),
            "p95_ms": round(loop_times[int(len(loop_times) * 0.95)], 2),
            "steps": "POST /api/jobs -> explain_jobs claim -> submit_explanation done",
        }
        # SSE replay of the five lifetimes: created/claimed/done events.
        stream = urllib.request.urlopen(base + "/events?since_id=0", timeout=15)
        try:
            events = 0
            buffer = b""
            deadline = time.time() + 10
            while time.time() < deadline and events < 5:
                line = stream.readline()
                if not line:
                    break
                buffer += line
                if b"\n\n" in buffer:
                    block, buffer = buffer.split(b"\n\n", 1)
                    if b"job.updated" in block:
                        events += 1
            results["sse"] = {"replayed_events_until_first_5": events}
        finally:
            stream.close()
        target = _common.resolve_result_path(args.out, "server_bench.json")
        _common.write_result(target, results)
        print(
            f"http health p50={results['http']['health']['p50_ms']}ms"  # type: ignore[index]
            f" search={results['http']['search']['p50_ms']}ms"  # type: ignore[index]
            f" stats={results['http']['stats']['p50_ms']}ms"  # type: ignore[index]
            f" | mcp detail p50={results['mcp']['detail']['p50_ms']}ms"  # type: ignore[index]
            f" | loop p50={results['cross_client']['p50_ms']}ms"  # type: ignore[index]
        )
        print(f"result: {_common.display_path(target)}")
    finally:
        server.shutdown()
        server.server_close()
        server.service.close()
        thread.join(timeout=5)
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(state_db) + suffix)
            if path.exists():
                try:
                    path.unlink()
                except OSError:
                    pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
