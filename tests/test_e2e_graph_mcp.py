"""End-to-end tests for ``ca graph``, ``ca update`` and the MCP server.

The self-contained repository from ``test_e2e_pipeline`` is indexed once and the
resulting database is shared by graph/update/MCP tests. MCP is exercised through
a real subprocess with a pure-stdlib JSON-RPC client (no MCP SDK dependency).
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))

from test_e2e_pipeline import run_cli, write_fixture_repo  # noqa: E402


@pytest.fixture(scope="module")
def indexed(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    root = tmp_path_factory.mktemp("ca-graph-mcp")
    repo = write_fixture_repo(root / "repo")
    db = root / "ca.db"
    from ca.build import load_compilation_database
    from ca.pipeline import run_index

    units = load_compilation_database(str(repo / "compile_commands.json"), str(repo))
    summary = run_index(str(repo), units, str(db), jobs=1)
    assert summary.tus_parsed == 2, summary.errors
    return {"repo": repo, "db": db, "root": root}


# --------------------------------------------------------------------------- #
# ca graph
# --------------------------------------------------------------------------- #


def _graph(indexed: dict[str, Path], *argv: str) -> tuple[int, dict]:
    # Use the CLI parser + library path so the test exercises both the option
    # wiring and the traversal without parsing table output.
    from ca.cli import graph as graph_module
    from ca.cli.main import build_parser
    from ca.store import SqliteStore

    parser = build_parser()
    args = parser.parse_args(
        ["graph", "--db", str(indexed["db"]), "--format", "json", *argv]
    )
    store = SqliteStore(str(indexed["db"]))
    store.open()
    try:
        data = graph_module.build_call_graph(
            store,
            args.from_symbol,
            direction="both"
            if args.both
            else ("reverse" if args.reverse else "forward"),
            depth=args.depth,
            limit=args.limit,
        )
    finally:
        store.close()
    return 0, data


def test_graph_forward_depth1(indexed: dict[str, Path]) -> None:
    code, data = _graph(indexed, "--from", "main", "--depth", "1")
    assert code == 0
    assert data["root"] == "main"
    names = {node["name"] for node in data["nodes"]}
    assert {"main", "proc_alloc", "proc_init"} <= names
    assert data["stats"]["edges"] >= 2
    assert any(
        edge["caller_name"] == "main" and edge["callee_name"] == "proc_alloc"
        for edge in data["edges"]
    )
    assert all(edge["source"] in ("direct", "candidate") for edge in data["edges"])


def test_graph_forward_depth2_includes_second_hop(indexed: dict[str, Path]) -> None:
    _code, depth1 = _graph(indexed, "--from", "main", "--depth", "1")
    _code, depth2 = _graph(indexed, "--from", "main", "--depth", "2")
    names2 = {node["name"] for node in depth2["nodes"]}
    assert "kmalloc" in names2  # main -> proc_alloc -> kmalloc
    assert depth2["stats"]["nodes"] >= depth1["stats"]["nodes"]
    assert all(node["depth"] <= 2 for node in depth2["nodes"])


def test_graph_reverse_reaches_callers(indexed: dict[str, Path]) -> None:
    _code, data = _graph(indexed, "--from", "kmalloc", "--depth", "2", "--reverse")
    names = {node["name"] for node in data["nodes"]}
    assert "proc_alloc" in names
    assert "main" in names  # main -> proc_alloc -> kmalloc
    assert any(
        edge["caller_name"] == "proc_alloc" and edge["callee_name"] == "kmalloc"
        for edge in data["edges"]
    )


def test_graph_dot_and_invalid_requests(
    indexed: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    code = run_cli(
        ["graph", "--from", "main", "--depth", "1", "--db", str(indexed["db"]), "--format", "dot"]
    )
    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.startswith("digraph")
    assert "->" in captured.out

    assert run_cli(["graph", "--from", "main", "--db", str(indexed["db"]), "--depth", "99"]) == 1
    assert run_cli(["graph", "--from", "no_such_symbol", "--db", str(indexed["db"]), "--depth", "1"]) == 2


# --------------------------------------------------------------------------- #
# ca update
# --------------------------------------------------------------------------- #


def test_update_dry_run(indexed: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    code = run_cli(
        [
            "update",
            "--repo",
            str(indexed["repo"]),
            "--db",
            str(indexed["db"]),
            "--compdb",
            str(indexed["repo"] / "compile_commands.json"),
            "--changed-files",
            "kernel/proc.c",
            "--dry-run",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0, captured.err
    plan = json.loads(captured.out)
    assert plan["affected_count"] == 1
    assert plan["header_change"] is False


def test_update_reparses_changed_tu(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = write_fixture_repo(tmp_path / "repo")
    db = tmp_path / "ca.db"
    common = [
        "--repo",
        str(repo),
        "--db",
        str(db),
        "--compdb",
        str(repo / "compile_commands.json"),
        "--jobs",
        "1",
    ]
    assert run_cli(["index", *common, "--incremental"]) == 0
    capsys.readouterr()
    proc_c = repo / "kernel" / "proc.c"
    proc_c.write_text(
        proc_c.read_text(encoding="utf-8")
        + "\nint proc_update_marker(void)\n{\n    return 7;\n}\n",
        encoding="utf-8",
    )
    code = run_cli(["update", *common, "--changed-files", "kernel/proc.c"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert "affected 1/2 TUs" in captured.err
    assert "cache hits=1/2" in captured.err  # main.c unchanged
    from ca.store import SqliteStore

    store = SqliteStore(str(db))
    store.open()
    try:
        assert any(row["name"] == "proc_update_marker" for row in store.query_functions("proc_update_marker"))
    finally:
        store.close()


def test_update_header_change_clears_cache(
    indexed: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    header = indexed["repo"] / "include" / "os.h"
    original = header.read_text(encoding="utf-8")
    header.write_text(original + "\nint header_probe(int value);\n", encoding="utf-8")
    try:
        code = run_cli(
            [
                "update",
                "--repo",
                str(indexed["repo"]),
                "--db",
                str(indexed["db"]),
                "--compdb",
                str(indexed["repo"] / "compile_commands.json"),
                "--changed-files",
                "include/os.h",
            ]
        )
        captured = capsys.readouterr()
        assert code == 0, captured.err
        assert "header" in captured.err.lower()
        assert "affected 2/2 TUs" in captured.err
    finally:
        header.write_text(original, encoding="utf-8")


# --------------------------------------------------------------------------- #
# MCP: stdio JSON-RPC client (pure stdlib)
# --------------------------------------------------------------------------- #


def _mcp_stdio_messages(indexed: dict[str, Path]) -> list[dict]:
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "find_symbol", "arguments": {"pattern": "proc_alloc"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "callers", "arguments": {"symbol": "kmalloc"}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "graph", "arguments": {"symbol": "main", "depth": 1}}},
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "stats", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "detail", "arguments": {"symbol": "proc_alloc"}}},
        {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "no_such_tool", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 9, "method": "bogus/method", "params": {}},
    ]
    return messages


def test_mcp_stdio_server(indexed: dict[str, Path]) -> None:
    env = {
        "PYTHONPATH": str(REPO_ROOT / "src"),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(Path.home()),
    }
    payload = "\n".join(json.dumps(message) for message in _mcp_stdio_messages(indexed)) + "\n"
    result = subprocess.run(
        [sys.executable, "-m", "ca", "mcp", "--db", str(indexed["db"])],
        input=payload,
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, result.stderr[-2000:]
    responses = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    by_id = {response.get("id"): response for response in responses}
    initialize = by_id[1]["result"]
    assert initialize["serverInfo"]["name"] == "ca-analyzer"
    assert "tools" in initialize["capabilities"]
    tools = {tool["name"] for tool in by_id[2]["result"]["tools"]}
    assert {
        "find_symbol", "functions", "callers", "callees", "graph", "branches",
        "assignments", "returns", "resources", "leaks", "detail", "stats",
    } <= tools
    def envelope(response_id: int) -> dict:
        content = by_id[response_id]["result"]["content"][0]["text"]
        return json.loads(content)
    found = envelope(3)
    assert found["ok"] and any(item["name"] == "proc_alloc" for item in found["data"]["items"])
    callers = envelope(4)
    assert any(item["caller_name"] == "proc_alloc" for item in callers["data"]["items"])
    graph = envelope(5)
    assert {node["name"] for node in graph["data"]["nodes"]} >= {"main", "proc_alloc"}
    stats = envelope(6)
    assert stats["data"]["tables"]["functions"] > 0
    detail = envelope(7)
    assert detail["data"]["function"]["name"] == "proc_alloc"
    unknown = envelope(8)
    assert unknown["ok"] is False and unknown["error"]["code"] == "UNKNOWN_TOOL"
    assert by_id[9]["error"]["code"] == -32601


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_mcp_http_server(indexed: dict[str, Path]) -> None:
    port = _free_port()
    env = {
        "PYTHONPATH": str(REPO_ROOT / "src"),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(Path.home()),
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "ca", "mcp", "--db", str(indexed["db"]), "--transport", "http", "--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
    )
    try:
        health_url = f"http://127.0.0.1:{port}/health"
        deadline = time.monotonic() + 30
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(health_url, timeout=1) as response:
                    health = json.loads(response.read().decode("utf-8"))
                break
            except Exception as exc:  # server not listening yet
                last_error = exc
                time.sleep(0.1)
        else:  # pragma: no cover - startup timeout
            raise AssertionError(f"MCP HTTP server did not start: {last_error}")

        assert health["status"] == "ok"
        request = json.dumps(
            {"jsonrpc": "2.0", "id": 10, "method": "tools/list", "params": {}}
        ).encode("utf-8")
        http_request = urllib.request.Request(
            f"http://127.0.0.1:{port}/mcp",
            data=request,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(http_request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        assert payload["id"] == 10
        assert any(tool["name"] == "graph" for tool in payload["result"]["tools"])
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            process.kill()
