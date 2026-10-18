"""End-to-end tests for layered indexing (L0 base, L1 lazy deep, L2 prewarm).

The self-contained repository from ``test_e2e_pipeline`` is indexed as a base
(L0) database: symbols/types/calls exist, deep facts do not. Lazy ``ensure_deep``
then computes CFG/data-flow facts on demand and must hit the status store on the
second call. The MCP tests drive the real stdio server (no SDK dependency).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))

from test_e2e_pipeline import run_cli, write_fixture_repo  # noqa: E402


@pytest.fixture(scope="module")
def layered(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    root = tmp_path_factory.mktemp("ca-layered")
    repo = write_fixture_repo(root / "repo")
    db = root / "ca.db"
    code = run_cli(
        [
            "index",
            "--repo",
            str(repo),
            "--db",
            str(db),
            "--compdb",
            str(repo / "compile_commands.json"),
            "--jobs",
            "1",
            "--layer",
            "base",
        ]
    )
    assert code == 0
    return {"repo": repo, "db": db, "root": root}


def _run_json(argv: list[str]) -> tuple[int, dict, str]:
    import io
    import contextlib

    from ca.cli.main import main

    buffer = io.StringIO()
    errors = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(errors):
        code = main(argv)
    text = buffer.getvalue().strip()
    return code, (json.loads(text) if text else {}), errors.getvalue()


def _store_ready(db: Path, symbol: str) -> tuple[bool, int]:
    from ca.store import SqliteStore

    store = SqliteStore(str(db))
    store.open()
    try:
        detail = store.query_function_detail(symbol)
        branches = len(detail.get("branches") or [])
        ready = bool(detail.get("deep_ready"))
    finally:
        store.close()
    return ready, branches


def test_base_layer_defers_deep_analysis(layered: dict[str, Path]) -> None:
    db = layered["db"]
    ready, branches = _store_ready(db, "proc_alloc")
    assert ready is False
    assert branches == 0
    code, status, err = _run_json(["deep-status", "--db", str(db), "--json"])
    assert code == 0, err
    assert status["mode"] == "base"
    assert status["deep_ready"] == 0
    assert status["total_functions"] > 0
    assert status["coverage"] == 0.0


def test_ensure_deep_computes_then_hits(layered: dict[str, Path]) -> None:
    db = layered["db"]
    repo = layered["repo"]
    common = [
        "--db",
        str(db),
        "--repo",
        str(repo),
        "--compdb",
        str(repo / "compile_commands.json"),
    ]
    code, first, err = _run_json(["ensure-deep", *common, "--symbols", "proc_alloc", "--json"])
    assert code == 0, err
    assert first["metrics"]["computed"] == 1
    assert first["results"][0]["deep_ready"] is True
    facts = first["results"][0]["facts"]
    assert facts["branches"] >= 1 and facts["returns"] >= 1
    assert first["metrics"]["first_latency_ms"] > 0

    code, second, err = _run_json(["ensure-deep", *common, "--symbols", "proc_alloc", "--json"])
    assert code == 0, err
    assert second["metrics"]["hits"] == 1
    assert second["metrics"]["computed"] == 0
    assert second["metrics"]["hit_latency_ms"] < first["metrics"]["first_latency_ms"]

    ready, branches = _store_ready(db, "proc_alloc")
    assert ready is True and branches >= 1


def test_prewarm_explicit_symbols(layered: dict[str, Path]) -> None:
    repo = layered["repo"]
    code, result, err = _run_json(
        [
            "prewarm",
            "--db",
            str(layered["db"]),
            "--repo",
            str(repo),
            "--compdb",
            str(repo / "compile_commands.json"),
            "--symbols",
            "proc_free",
        ]
    )
    assert code == 0, err
    assert result["metrics"]["computed"] == 1


def test_update_invalidates_deep_status(layered: dict[str, Path]) -> None:
    repo = layered["repo"]
    db = layered["db"]
    common = [
        "--db",
        str(db),
        "--repo",
        str(repo),
        "--compdb",
        str(repo / "compile_commands.json"),
    ]
    code, _first, err = _run_json(["ensure-deep", *common, "--symbols", "proc_init", "--json"])
    assert code == 0, err
    proc_c = repo / "kernel" / "proc.c"
    proc_c.write_text(
        proc_c.read_text(encoding="utf-8") + "\nint proc_after_update(void)\n{\n    return 0;\n}\n",
        encoding="utf-8",
    )
    code = run_cli(
        ["update", *common, "--jobs", "1", "--layer", "base", "--changed-files", "kernel/proc.c"]
    )
    assert code == 0
    code, status, err = _run_json(["deep-status", "--db", str(db), "--json"])
    assert code == 0, err
    assert status["coverage"] == 0.0


def test_mcp_lazy_deep_tools(tmp_path: Path) -> None:
    repo = write_fixture_repo(tmp_path / "repo")
    db = tmp_path / "mcp-base.db"
    assert (
        run_cli(
            [
                "index", "--repo", str(repo), "--db", str(db),
                "--compdb", str(repo / "compile_commands.json"),
                "--jobs", "1", "--layer", "base",
            ]
        )
        == 0
    )
    env = {
        "PYTHONPATH": str(REPO_ROOT / "src"),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(Path.home()),
    }
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "detail", "arguments": {"symbol": "proc_alloc"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "ensure_deep", "arguments": {"symbols": ["proc_alloc"]}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "detail", "arguments": {"symbol": "proc_alloc"}}},
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "branches", "arguments": {"symbol": "proc_alloc"}}},
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "deep_status", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "prewarm", "arguments": {"limit": 1, "background": False}}},
    ]
    payload = "\n".join(json.dumps(message) for message in messages) + "\n"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ca",
            "mcp",
            "--db",
            str(db),
            "--repo",
            str(repo),
            "--compdb",
            str(repo / "compile_commands.json"),
        ],
        input=payload,
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, result.stderr[-2000:]
    responses = {item["id"]: item for item in (json.loads(line) for line in result.stdout.splitlines() if line.strip())}
    tools = {tool["name"] for tool in responses[2]["result"]["tools"]}
    assert {"ensure_deep", "deep_status", "prewarm"} <= tools

    def envelope(response_id: int) -> dict:
        return json.loads(responses[response_id]["result"]["content"][0]["text"])

    before = envelope(3)
    assert before["data"]["deep_ready"] is False
    computed = envelope(4)
    assert computed["ok"] and computed["data"]["metrics"]["computed"] == 1
    after = envelope(5)
    assert after["data"]["deep_ready"] is True
    assert after["data"]["branches"]
    branches = envelope(6)
    assert branches["data"]["deep_ready"] is True
    assert branches["data"]["pending"] is False
    status = envelope(7)
    assert status["data"]["total_functions"] > 0
    prewarm = envelope(8)
    assert prewarm["ok"] and "metrics" in prewarm["data"]
