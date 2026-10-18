#!/usr/bin/env python3
"""Verify the MCP interface against a finished CA database (server-side).

Checks both transports with a pure-stdlib client:

* stdio: initialize, tools/list (21 tools), detail/graph, deep_status,
  ensure_deep first call and cache-hit call latency;
* HTTP: start the server on a fresh port, poll ``GET /health`` and call
  ``tools/list`` over ``POST /mcp``.

Runs with the same environment as the server (for OH pass
``CA_LIBCLANG_LIBRARY`` / ``PYTHONPATH`` exactly like the index run).
Writes a JSON report consumed by the phase-3 acceptance notes.

Usage::

    python scripts/verify_shard_mcp.py \
        --db /home/yu/CA_OH/index/oh_S1.db \
        --repo /home/yu/CA_OH/openharmony \
        --compdb /home/yu/CA_OH/corpus/openharmony/shards/shard_S1.json \
        --out /home/yu/CA_OH/verify_mcp_s1.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _mcp_command(db: str, repo: str, compdb: str, extra: list[str] | None = None) -> list[str]:
    return [
        sys.executable,
        "-m",
        "ca",
        "mcp",
        "--db",
        db,
        "--repo",
        repo,
        "--compdb",
        compdb,
        *(extra or []),
    ]


def _pick_symbols(db: str, limit: int = 3, force_cold: bool = True) -> list[str]:
    """Pick real definition symbols and make at least one cold for latency.

    Header declarations have no definition in the TU, so ``ensure_deep`` on them
    pays a full parse and then errors. To measure a meaningful first-call
    latency we prefer definitions that are not deep-ready; if all definitions in
    the first window are ready we invalidate a few status rows (deep facts stay
    in the DB) so ``ensure_deep`` recomputes and re-marks them.
    """

    sys.path.insert(0, str(ROOT / "src"))
    from ca.store import SqliteStore

    store = SqliteStore(db)
    store.open()
    try:
        candidates: list[tuple[str, str]] = []
        seen: set[str] = set()
        for record in store.iter_records(kinds=("function",), limit=5000):
            if not record.get("is_definition"):
                continue
            name = str(record.get("name") or "")
            function_id = str(record.get("id") or "")
            if not name or not function_id or name in seen:
                continue
            seen.add(name)
            candidates.append((function_id, name))
            if len(candidates) >= 200:
                break
        if hasattr(store, "get_deep_status") and candidates:
            try:
                status = store.get_deep_status([item[0] for item in candidates])
                pending = [
                    name
                    for function_id, name in candidates
                    if not (status.get(function_id) or {}).get("deep_ready")
                ]
                if pending:
                    return pending[:limit]
            except Exception:
                pass
            # The first hundreds of definitions may all be deep-ready after a
            # full index; pick pending rows (usually header/inline functions) so
            # the reported first-call latency is a real lazy parse.
            if force_cold and candidates and hasattr(store, "invalidate_deep"):
                # All early definitions are deep-ready: invalidate a few status
                # rows to exercise the real lazy parse+analyze+write path.
                cold_ids = [function_id for function_id, _name in candidates[:limit]]
                try:
                    store.invalidate_deep(cold_ids)
                    return [name for _function_id, name in candidates[:limit]]
                except Exception:
                    pass
        return [name for _function_id, name in candidates[:limit]]
    finally:
        store.close()


def _run_stdio(db: str, repo: str, compdb: str, symbols: list[str]) -> dict:
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "verify", "version": "0"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "deep_status", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "detail", "arguments": {"symbol": symbols[0]}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "ensure_deep", "arguments": {"symbols": symbols, "jobs": 4}}},
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "graph", "arguments": {"symbol": symbols[0], "depth": 1}}},
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "ensure_deep", "arguments": {"symbols": symbols, "jobs": 4}}},
        {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "prewarm", "arguments": {"limit": 2}}},
    ]
    payload = "\n".join(json.dumps(message) for message in messages) + "\n"
    started = time.perf_counter()
    result = subprocess.run(
        _mcp_command(db, repo, compdb),
        input=payload,
        capture_output=True,
        text=True,
        timeout=900,
    )
    elapsed = time.perf_counter() - started
    if result.returncode != 0:
        return {"ok": False, "error": result.stderr[-3000:], "elapsed_seconds": elapsed}
    responses = {
        item["id"]: item
        for item in (json.loads(line) for line in result.stdout.splitlines() if line.strip())
    }

    def envelope(response_id: int) -> dict:
        response = responses.get(response_id) or {}
        content = (response.get("result") or {}).get("content") or []
        if not content:
            return {"ok": False, "error": response.get("error")}
        return json.loads(content[0]["text"])

    tools = [tool["name"] for tool in responses[2]["result"]["tools"]]
    deep_status = envelope(3).get("data", {})
    detail = envelope(4).get("data", {})
    first = envelope(5).get("data", {}).get("metrics", {})
    graph = envelope(6).get("data", {})
    hit = envelope(7).get("data", {}).get("metrics", {})
    prewarm = envelope(8).get("data", {})
    return {
        "ok": True,
        "elapsed_seconds": round(elapsed, 3),
        "tools_count": len(tools),
        "tools": sorted(tools),
        "deep_status": {key: deep_status.get(key) for key in ("total_functions", "deep_ready", "pending", "coverage", "mode")},
        "detail": {"symbol": symbols[0], "deep_ready": detail.get("deep_ready"), "branches": len(detail.get("branches") or [])},
        "graph": {"root": graph.get("root"), "nodes": (graph.get("stats") or {}).get("nodes"), "deep_ready": graph.get("deep_ready")},
        "ensure_deep_first": first,
        "ensure_deep_hit": hit,
        "prewarm": {"requested": prewarm.get("requested"), "metrics": prewarm.get("metrics")},
    }


def _free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _run_http(db: str, repo: str, compdb: str, port: int) -> dict:
    process = subprocess.Popen(
        _mcp_command(db, repo, compdb, ["--transport", "http", "--port", str(port)]),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    health_url = f"http://127.0.0.1:{port}/health"
    try:
        deadline = time.monotonic() + 120
        health = None
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(health_url, timeout=1) as response:
                    health = json.loads(response.read().decode("utf-8"))
                break
            except Exception:
                time.sleep(0.2)
        if health is None:
            return {"ok": False, "error": "HTTP server did not start"}
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/mcp",
            data=json.dumps(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
        tools = [tool["name"] for tool in payload["result"]["tools"]]
        return {"ok": True, "health": health, "tools_count": len(tools)}
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover
            process.kill()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--compdb", required=True)
    parser.add_argument("--symbols", nargs="*", default=None)
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    if not Path(args.db).is_file():
        print(f"error: database not found: {args.db}", file=sys.stderr)
        return 1
    symbols = args.symbols or _pick_symbols(args.db)
    if not symbols:
        print("error: no function symbols found in database", file=sys.stderr)
        return 1

    report: dict = {"db": args.db, "repo": args.repo, "compdb": args.compdb, "symbols": symbols}
    report["stdio"] = _run_stdio(args.db, args.repo, args.compdb, symbols)
    port = _free_port()
    report["http"] = _run_http(args.db, args.repo, args.compdb, port)
    report["ok"] = bool(report["stdio"].get("ok") and report["http"].get("ok"))

    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"report: {args.out}")
    print(json.dumps(report, indent=1, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
