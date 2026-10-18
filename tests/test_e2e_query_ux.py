"""End-to-end tests for the P0 query experience (task-25).

Covers detail projection/compact presets, batch queries from files/stdin,
the summary-first ``brief`` command, MCP tools (18 total) and the additive
definition/declaration grouping. Header-declaration indexing tests activate once
the parser exposes ``emit_header_declarations``.
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

from test_e2e_pipeline import write_fixture_repo  # noqa: E402


def _index(repo: Path, db: Path, *extra: str) -> None:
    from ca.build import load_compilation_database
    from ca.pipeline import run_index

    units = list(
        load_compilation_database(str(repo / "compile_commands.json"), str(repo))
    )
    summary = run_index(
        str(repo),
        units,
        str(db),
        jobs=1,
        analyze_scope="main",
        record_scope="all",
    )
    assert summary.tus_parsed == len(units), summary.errors


def _run_json(argv: list[str], stdin_text: str | None = None) -> tuple[int, object, str]:
    from ca.cli.main import main

    buffer = io.StringIO()
    errors = io.StringIO()
    old_stdin = sys.stdin
    if stdin_text is not None:
        sys.stdin = io.StringIO(stdin_text)
    try:
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(errors):
            code = main(argv)
    finally:
        sys.stdin = old_stdin
    text = buffer.getvalue().strip()
    return code, (json.loads(text) if text else None), errors.getvalue()


def _parser_supports_header_declarations() -> bool:
    from ca.parse import ParseOptions

    return "emit_header_declarations" in getattr(ParseOptions, "__dataclass_fields__", {})


@pytest.fixture(scope="module")
def indexed(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    root = tmp_path_factory.mktemp("ca-query-ux")
    repo = write_fixture_repo(root / "repo")
    # Header-only declaration (no definition anywhere) for declaration queries.
    header = repo / "include" / "os.h"
    header.write_text(
        header.read_text(encoding="utf-8")
        + "\nint qa_header_only_declaration(int value);\n",
        encoding="utf-8",
    )
    db = root / "ca.db"
    _index(repo, db)
    return {"root": root, "repo": repo, "db": db}


def test_detail_projection_and_compact(indexed: dict[str, Path]) -> None:
    db = indexed["db"]
    code, full, err = _run_json(
        ["query", "detail", "proc_alloc", "--db", str(db), "--json"]
    )
    assert code == 0, err
    assert set(full) >= {"function", "branches", "assignments", "returns", "deep_ready"}

    code, compact, err = _run_json(
        ["query", "detail", "proc_alloc", "--db", str(db), "--json", "--compact"]
    )
    assert code == 0, err
    assert {"function", "counts", "deep_ready"} <= set(compact)
    assert compact["function"]["name"] == "proc_alloc"
    assert compact["counts"]["branches"] == len(full["branches"])
    full_bytes = len(json.dumps(full, ensure_ascii=False).encode())
    compact_bytes = len(json.dumps(compact, ensure_ascii=False).encode())
    assert compact_bytes < full_bytes
    assert compact_bytes <= 5000

    code, projected, err = _run_json(
        [
            "query",
            "detail",
            "proc_alloc",
            "--db",
            str(db),
            "--json",
            "--fields",
            "function.name,function.line,deep_ready",
        ]
    )
    assert code == 0, err
    assert projected == {
        "function.name": "proc_alloc",
        "function.line": full["function"]["line"],
        "deep_ready": full["deep_ready"],
    }

    code, _payload, err = _run_json(
        [
            "query",
            "detail",
            "proc_alloc",
            "--db",
            str(db),
            "--json",
            "--fields",
            "function.nope",
        ]
    )
    assert code == 1
    assert "unknown field" in err

    code, _payload, err = _run_json(
        ["query", "callers", "main", "--db", str(db), "--json", "--compact"]
    )
    assert code == 1
    assert "--compact only applies" in err


def test_batch_queries_file_and_stdin(indexed: dict[str, Path], tmp_path: Path) -> None:
    db = indexed["db"]
    symbols_file = tmp_path / "symbols.txt"
    symbols_file.write_text(
        "# comment\nproc_alloc\nproc_free\nno_such_symbol\nproc_init\n",
        encoding="utf-8",
    )
    code, batch, err = _run_json(
        [
            "query",
            "callers",
            "--symbols",
            str(symbols_file),
            "--db",
            str(db),
            "--json",
            "--limit",
            "5",
        ]
    )
    assert code == 0, err
    assert batch["kind"] == "callers"
    assert batch["count"] == 4
    assert [item["symbol"] for item in batch["items"]] == [
        "proc_alloc",
        "proc_free",
        "no_such_symbol",
        "proc_init",
    ]
    statuses = {item["symbol"]: item["ok"] for item in batch["items"]}
    assert statuses["proc_alloc"] is True
    # Unknown symbols have no callers: empty list, not an error at this level.
    unknown = next(item for item in batch["items"] if item["symbol"] == "no_such_symbol")
    assert unknown["ok"] is True and unknown["data"] == []
    assert all(len(item["data"]) <= 5 for item in batch["items"] if item["ok"])

    code, stdin_batch, err = _run_json(
        ["query", "detail", "--symbols", "-", "--db", str(db), "--json", "--compact"],
        stdin_text="proc_alloc\nmain\n",
    )
    assert code == 0, err
    assert stdin_batch["kind"] == "detail"
    assert [item["symbol"] for item in stdin_batch["items"]] == ["proc_alloc", "main"]
    assert all(item["data"]["function"]["name"] == item["symbol"] for item in stdin_batch["items"])


def test_brief_counts_and_size(indexed: dict[str, Path]) -> None:
    db = indexed["db"]
    code, brief, err = _run_json(["brief", "proc_alloc", "--db", str(db), "--json"])
    assert code == 0, err
    assert brief["found"] is True
    assert brief["function"]["name"] == "proc_alloc"
    assert brief["function"]["signature"]
    assert brief["counts"]["branches"] >= 0
    assert brief["counts"]["direct_callers"] >= 1
    assert isinstance(brief["key_conditions"], list)
    assert brief["deep_ready"] in (True, False)
    assert len(json.dumps(brief, ensure_ascii=False).encode()) <= 2048

    code, _payload, err = _run_json(["brief", "no_such_symbol", "--db", str(db), "--json"])
    assert code == 2
    assert "no function matching" in err


def test_mcp_query_ux_tools(indexed: dict[str, Path]) -> None:
    db = indexed["db"]
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "detail", "arguments": {"symbol": "proc_alloc", "compact": True}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "batch", "arguments": {"kind": "callers", "symbols": ["proc_alloc", "proc_free"], "limit": 3}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "brief", "arguments": {"symbol": "proc_alloc"}}},
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "find_symbol", "arguments": {"pattern": "proc"}}},
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "detail", "arguments": {"symbol": "proc_alloc", "fields": "function.name,deep_ready"}}},
    ]
    payload = "\n".join(json.dumps(message) for message in messages) + "\n"
    result = subprocess.run(
        [sys.executable, "-m", "ca", "mcp", "--db", str(db)],
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

    tools = {tool["name"] for tool in responses[2]["result"]["tools"]}
    assert len(tools) == 18
    assert {"batch", "brief"} <= tools
    compact = envelope(3)["data"]
    assert {"function", "counts", "deep_ready"} <= set(compact)
    batch = envelope(4)["data"]
    assert batch["count"] == 2 and batch["items"][0]["ok"]
    brief = envelope(5)["data"]
    assert brief["found"] is True and brief["counts"]["callers"] >= 1
    found = envelope(6)["data"]
    assert "definitions" in found and "declarations" in found
    projected = envelope(7)["data"]
    assert set(projected) == {"function.name", "deep_ready"}

    error = json.loads(
        json.dumps(envelope(7)) if False else responses[7]["result"]["content"][0]["text"]
    )
    assert error["ok"] is True


def test_federated_query_ux(indexed: dict[str, Path]) -> None:
    shards = indexed["root"] / "shards"
    shards.mkdir(exist_ok=True)
    import shutil

    for name in ("shard1.db", "shard2.db"):
        for suffix in ("", "-wal", "-shm"):
            source = Path(str(indexed["db"]) + suffix)
            if source.exists():
                shutil.copy2(source, shards / f"{name}{suffix}")
    code, rows, err = _run_json(
        ["query", "functions", "proc_alloc", "--db-dir", str(shards), "--json"]
    )
    assert code == 0, err
    assert rows and rows[0]["shard"] == "shard1"
    code, projected, err = _run_json(
        [
            "query",
            "detail",
            "proc_alloc",
            "--db-dir",
            str(shards),
            "--json",
            "--fields",
            "function.name,shard",
        ]
    )
    assert code == 0, err
    assert projected["function.name"] == "proc_alloc"
    assert projected["shard"] == "shard1"
    code, batch, err = _run_json(
        [
            "query",
            "callers",
            "--symbols",
            "-",
            "--db-dir",
            str(shards),
            "--json",
        ],
        stdin_text="proc_alloc\n",
    )
    assert code == 0, err
    assert batch["items"][0]["data"][0]["shard"] == "shard1"


@pytest.mark.skipif(
    not _parser_supports_header_declarations(),
    reason="parser header-declaration emission not available yet",
)
def test_header_declaration_indexed(tmp_path: Path) -> None:
    from ca.build import load_compilation_database
    from ca.pipeline import run_index

    repo = write_fixture_repo(tmp_path / "decl-repo")
    header = repo / "include" / "os.h"
    header.write_text(
        header.read_text(encoding="utf-8")
        + "\nint qa_header_only_declaration(int value);\n",
        encoding="utf-8",
    )
    db = tmp_path / "decl.db"
    units = list(
        load_compilation_database(str(repo / "compile_commands.json"), str(repo))
    )
    summary = run_index(
        str(repo),
        units,
        str(db),
        jobs=1,
        analyze_scope="main",
        record_scope="main_file",
        header_declarations=True,
    )
    assert summary.tus_parsed == len(units)
    code, rows, err = _run_json(
        ["query", "functions", "qa_header_only_declaration", "--db", str(db), "--json"]
    )
    assert code == 0, err
    assert rows, err
    assert rows[0]["is_definition"] is False
    assert rows[0]["file"] == "include/os.h"
    code, brief, err = _run_json(
        ["brief", "qa_header_only_declaration", "--db", str(db), "--json"]
    )
    assert code == 0, err
    assert brief["definition_missing"] is True
    assert brief["declaration"]["file"] == "include/os.h"
