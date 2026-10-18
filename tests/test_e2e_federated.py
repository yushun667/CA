"""End-to-end tests for federated (multi-shard) queries: CLI + MCP.

Two shard databases are built from the same fixture repository (identical
record ids -> dedupe), a third from a second repository with unique functions
(union), a fourth carries cross-TU callback registrations. The CLI ``--db-dir``
and MCP ``--db-dir`` modes must merge rows, add a ``shard`` source field, sort
deterministically, apply offset/limit after dedupe and skip unreadable shard
files. Single-``--db`` behaviour stays unchanged.
"""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))

from test_e2e_callbacks import _write_callback_repo  # noqa: E402
from test_e2e_pipeline import run_cli, write_fixture_repo  # noqa: E402


def _index(repo: Path, db: Path) -> None:
    from ca.build import load_compilation_database
    from ca.pipeline import run_index

    units = list(
        load_compilation_database(str(repo / "compile_commands.json"), str(repo))
    )
    summary = run_index(
        str(repo), units, str(db), jobs=1, analyze_scope="main", record_scope="all"
    )
    assert summary.tus_parsed == len(units), summary.errors


def _run_json(argv: list[str]) -> tuple[int, object, str]:
    from ca.cli.main import main

    buffer = io.StringIO()
    errors = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(errors):
        code = main(argv)
    text = buffer.getvalue().strip()
    payload = json.loads(text) if text else None
    return code, payload, errors.getvalue()


@pytest.fixture(scope="module")
def federated(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    root = tmp_path_factory.mktemp("ca-federated")
    shards = root / "shards"
    shards.mkdir()

    repo_a = write_fixture_repo(root / "repo-a")
    _index(repo_a, shards / "shard1.db")
    _index(repo_a, shards / "shard2.db")

    repo_b = write_fixture_repo(root / "repo-b")
    main_c = repo_b / "kernel" / "main.c"
    main_c.write_text(
        main_c.read_text(encoding="utf-8")
        + "\nint shard_b_unique(void)\n{\n    return 11;\n}\n",
        encoding="utf-8",
    )
    _index(repo_b, shards / "shard3.db")

    # Fourth shard: cross-TU callback registration facts + candidate edges.
    from ca.cli.callbacks import resolve_callbacks

    repo_c = _write_callback_repo(root / "repo-c")
    callback_db = shards / "shard4.db"
    _index(repo_c, callback_db)
    resolve_callbacks(str(callback_db), repo=str(repo_c), jobs=1)

    (shards / "broken.db").write_text("not a sqlite database", encoding="utf-8")
    return {"root": root, "shards": shards, "repo_a": repo_a}


def test_federated_functions_union_dedupe_sort_limit(
    federated: dict[str, Path]
) -> None:
    shards = federated["shards"]
    code, rows, err = _run_json(
        ["query", "functions", "proc", "--db-dir", str(shards), "--json"]
    )
    assert code == 0, err
    names = [row["name"] for row in rows]
    # shard1/shard2 carry identical ids -> deduped; each row names its shard.
    assert sorted(set(names)) == ["proc_alloc", "proc_free", "proc_init"]
    assert all(row.get("shard") for row in rows)
    assert {row["shard"] for row in rows} == {"shard1"}  # first shard wins
    assert "warning: skipping unreadable shard" in err

    # Deterministic order.
    _code, again, _err = _run_json(
        ["query", "functions", "proc", "--db-dir", str(shards), "--json"]
    )
    assert [(row["id"], row["shard"]) for row in rows] == [
        (row["id"], row["shard"]) for row in again
    ]

    # limit/offset applied after merge+dedupe.
    _code, limited, _err = _run_json(
        ["query", "functions", "proc", "--db-dir", str(shards), "--limit", "2", "--offset", "1", "--json"]
    )
    assert [row["name"] for row in limited] == [row["name"] for row in rows[1:3]]

    # Unique functions from the second repository are included.
    _code, unique, _err = _run_json(
        ["query", "functions", "shard_b", "--db-dir", str(shards), "--json"]
    )
    assert [row["name"] for row in unique] == ["shard_b_unique"]


def test_federated_stats_and_missing_databases(federated: dict[str, Path]) -> None:
    shards = federated["shards"]
    code, stats, err = _run_json(["stats", "--db-dir", str(shards), "--json"])
    assert code == 0, err
    assert stats["shard_count"] == 4
    assert len(stats["shards"]) == 4
    assert any(item["path"].endswith("broken.db") for item in stats["skipped"])
    single = _single_stats(federated)
    assert stats["tables"]["functions"] >= single["tables"]["functions"]
    assert stats["db_bytes"] > 0

    empty = federated["root"] / "empty"
    empty.mkdir(exist_ok=True)
    (empty / "broken.db").write_text("nope", encoding="utf-8")
    code, _payload, err = _run_json(["stats", "--db-dir", str(empty), "--json"])
    assert code == 1
    assert "no readable shard databases" in err


def _single_stats(federated: dict[str, Path]) -> dict:
    code, stats, err = _run_json(
        ["stats", "--db", str(federated["shards"] / "shard1.db"), "--json"]
    )
    assert code == 0, err
    return stats


def test_federated_graph_and_detail(federated: dict[str, Path]) -> None:
    shards = federated["shards"]
    code, graph, err = _run_json(
        ["graph", "--from", "main", "--db-dir", str(shards), "--format", "json"]
    )
    assert code == 0, err
    assert graph["root"]
    assert all(node.get("shard") for node in graph["nodes"])
    assert all(edge.get("shard") for edge in graph["edges"])
    assert graph["stats"]["nodes"] == len(graph["nodes"])

    code, detail, err = _run_json(
        ["query", "detail", "proc_alloc", "--db-dir", str(shards), "--json"]
    )
    assert code == 0, err
    assert detail["function"]["name"] == "proc_alloc"
    assert detail["function"].get("shard")

    code, branches, err = _run_json(
        ["query", "branches", "proc_alloc", "--db-dir", str(shards), "--json"]
    )
    assert code == 0, err
    assert branches and all(row.get("shard") for row in branches)


def test_federated_callbacks(federated: dict[str, Path]) -> None:
    shards = federated["shards"]
    code, stats, err = _run_json(["stats", "--db-dir", str(shards), "--json"])
    assert code == 0, err
    # shard4 carries the cross-TU callback registration facts.
    assert stats["callbacks"]["registrations"] >= 1
    assert stats["callbacks"]["shard_count"] == 4
    assert stats["callbacks"]["linked_call_targets"] >= 1

    # MCP callbacks tool merges shards and carries the source shard.
    payload = "\n".join(
        json.dumps(message)
        for message in [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "callbacks", "arguments": {"path": "g_ops"}}},
        ]
    ) + "\n"
    result = subprocess.run(
        [sys.executable, "-m", "ca", "mcp", "--db-dir", str(shards)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=180,
        env={
            "PYTHONPATH": str(REPO_ROOT / "src"),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(Path.home()),
        },
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, result.stderr[-2000:]
    responses = {
        item["id"]: item
        for item in (json.loads(line) for line in result.stdout.splitlines() if line.strip())
    }
    envelope = json.loads(responses[2]["result"]["content"][0]["text"])
    assert envelope["ok"] is True
    assert envelope["data"]["count"] >= 1
    assert all(item.get("shard") for item in envelope["data"]["items"])


def test_single_db_output_unchanged(federated: dict[str, Path]) -> None:
    db = federated["shards"] / "shard1.db"
    code, rows, err = _run_json(
        ["query", "functions", "proc", "--db", str(db), "--json"]
    )
    assert code == 0, err
    assert rows
    assert all("shard" not in row for row in rows)
    code, stats, err = _run_json(["stats", "--db", str(db), "--json"])
    assert code == 0, err
    assert "shards" not in stats
    assert "shard_count" not in stats


def test_federated_mcp_tools(federated: dict[str, Path]) -> None:
    shards = federated["shards"]
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "functions", "arguments": {"pattern": "proc"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "detail", "arguments": {"symbol": "proc_alloc"}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "graph", "arguments": {"symbol": "main", "depth": 1}}},
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "stats", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "ensure_deep", "arguments": {"symbols": ["proc_alloc"]}}},
    ]
    payload = "\n".join(json.dumps(message) for message in messages) + "\n"
    result = subprocess.run(
        [sys.executable, "-m", "ca", "mcp", "--db-dir", str(shards)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=180,
        env={
            "PYTHONPATH": str(REPO_ROOT / "src"),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(Path.home()),
        },
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, result.stderr[-2000:]
    responses = {
        item["id"]: item
        for item in (json.loads(line) for line in result.stdout.splitlines() if line.strip())
    }

    def envelope(response_id: int) -> dict:
        return json.loads(responses[response_id]["result"]["content"][0]["text"])

    assert len(responses[2]["result"]["tools"]) == 18
    functions = envelope(3)
    assert functions["ok"] is True
    assert functions["data"]["items"]
    assert all(item.get("shard") for item in functions["data"]["items"])
    detail = envelope(4)
    assert detail["data"]["function"]["shard"]
    graph = envelope(5)
    assert graph["data"]["nodes"] and all(node.get("shard") for node in graph["data"]["nodes"])
    stats = envelope(6)
    assert stats["data"]["shard_count"] == 4
    deep = envelope(7)
    assert deep["ok"] is False
    assert deep["error"]["code"] == "FEDERATED_READ_ONLY"
