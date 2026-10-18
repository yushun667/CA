"""End-to-end tests: cross-TU callback post-processing pass (``ca resolve-callbacks``).

A tiny repository registers a function pointer into a global ops struct in one
translation unit and invokes it through that struct in another TU. The normal
index cannot see the registration across TUs, so the post-processing pass must
create the candidate edge; the graph, CLI JSON and MCP graph/callbacks tools
must expose it. Re-running the pass must be a no-op.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))

from test_e2e_pipeline import run_cli  # noqa: E402


def _write_callback_repo(root: Path) -> Path:
    repo = root / "repo"
    (repo / "include").mkdir(parents=True)
    (repo / "include" / "ops.h").write_text(
        "struct ops {\n"
        "    int (*read)(int fd);\n"
        "};\n"
        "extern struct ops g_ops;\n"
        "extern int (*g_table[2])(int fd);\n",
        encoding="utf-8",
    )
    (repo / "register.c").write_text(
        '#include "ops.h"\n'
        "static int read_impl(int fd)\n"
        "{\n"
        "    return fd + 1;\n"
        "}\n"
        "static int write_impl(int fd)\n"
        "{\n"
        "    return fd + 2;\n"
        "}\n"
        "struct ops g_ops;\n"
        "int (*g_table[2])(int fd);\n"
        "void register_ops(void)\n"
        "{\n"
        "    g_ops.read = read_impl;\n"
        "    g_table[0] = write_impl;\n"
        "}\n",
        encoding="utf-8",
    )
    (repo / "main.c").write_text(
        '#include "ops.h"\n'
        "int caller(int fd)\n"
        "{\n"
        "    return g_ops.read(fd);\n"
        "}\n"
        "int call_table(int index, int fd)\n"
        "{\n"
        "    return g_table[index](fd);\n"
        "}\n",
        encoding="utf-8",
    )
    entries = []
    for name in ("register.c", "main.c"):
        entries.append(
            {
                "directory": str(repo),
                "file": str(repo / name),
                "arguments": [
                    "clang",
                    "-I",
                    str(repo / "include"),
                    "-c",
                    str(repo / name),
                ],
            }
        )
    (repo / "compile_commands.json").write_text(json.dumps(entries), encoding="utf-8")
    return repo


def _index(repo: Path, db: Path) -> None:
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
    assert summary.tus_parsed == 2, summary.errors


def _store_counts(db: Path) -> dict[str, int]:
    from ca.store import SqliteStore

    store = SqliteStore(str(db))
    store.open()
    try:
        return dict(store.stats()["tables"])
    finally:
        store.close()


def _graph_candidates(db: Path, caller: str) -> list[dict]:
    from ca.store import SqliteStore

    store = SqliteStore(str(db))
    store.open()
    try:
        graph = store.query_call_graph(caller, direction="forward", depth=1)
    finally:
        store.close()
    return [edge for edge in graph.get("edges", []) if edge.get("candidate")]


def test_resolve_callbacks_creates_cross_tu_edge(tmp_path: Path) -> None:
    from ca.cli.callbacks import resolve_callbacks

    repo = _write_callback_repo(tmp_path)
    db = tmp_path / "callbacks.db"
    _index(repo, db)
    before = _store_counts(db)
    assert before["callback_registrations"] == 0

    summary = resolve_callbacks(str(db), repo=str(repo), jobs=1)
    data = summary.to_dict()
    assert summary.units == 2 and summary.units_parsed == 2
    assert summary.registrations >= 1, data
    assert summary.candidate_edges >= 1, data
    assert summary.written_registrations >= 1
    assert summary.written_edges >= 1
    assert summary.written_links >= 1
    assert summary.elapsed_seconds > 0

    after = _store_counts(db)
    assert after["callback_registrations"] == summary.registrations
    assert after["call_targets"] == before["call_targets"] + summary.written_edges
    assert after["callback_target_links"] == summary.links

    candidates = _graph_candidates(db, "caller")
    assert candidates, "graph should expose the cross-TU candidate edge"
    edge = candidates[0]
    assert edge["callee_name"] == "read_impl"
    assert edge["source"] == "call_target"
    assert edge["candidate"] is True
    assert edge["kind"] == "indirect_candidate"
    assert 0.4 <= float(edge["confidence"]) <= 1.0

    # Idempotent: a second pass writes nothing and keeps counts stable.
    second = resolve_callbacks(str(db), repo=str(repo), jobs=1).to_dict()
    assert second["written"] == {
        "registrations": 0,
        "candidate_edges": 0,
        "links": 0,
    }
    assert _store_counts(db) == after


def test_resolve_callbacks_cli_and_stats(tmp_path: Path) -> None:
    repo = _write_callback_repo(tmp_path)
    db = tmp_path / "cli-callbacks.db"
    _index(repo, db)

    code, payload, err = _run_cli_json(
        ["resolve-callbacks", "--db", str(db), "--repo", str(repo), "--json"]
    )
    assert code == 0, err
    assert payload["registrations"] >= 1
    assert payload["candidate_edges"] >= 1
    assert payload["written"]["candidate_edges"] >= 1

    code, stats, err = _run_cli_json(["stats", "--db", str(db), "--json"])
    assert code == 0, err
    assert stats["callbacks"]["registrations"] == payload["registrations"]
    assert stats["tables"]["callback_registrations"] == payload["registrations"]
    assert stats["tables"]["callback_target_links"] == payload["links"]

    code = run_cli(
        [
            "index",
            "--repo",
            str(repo),
            "--db",
            str(tmp_path / "auto-callbacks.db"),
            "--compdb",
            str(repo / "compile_commands.json"),
            "--jobs",
            "1",
            "--resolve-callbacks",
        ]
    )
    assert code == 0
    auto_counts = _store_counts(tmp_path / "auto-callbacks.db")
    assert auto_counts["callback_registrations"] >= 1


def test_mcp_exposes_cross_tu_candidate_and_callbacks(tmp_path: Path) -> None:
    from ca.cli.callbacks import resolve_callbacks

    repo = _write_callback_repo(tmp_path)
    db = tmp_path / "mcp-callbacks.db"
    _index(repo, db)
    resolve_callbacks(str(db), repo=str(repo), jobs=1)

    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "graph", "arguments": {"symbol": "caller", "depth": 1}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "callbacks", "arguments": {"path": "g_ops"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "stats", "arguments": {}}},
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

    graph = envelope(2)
    assert graph["ok"] is True
    candidate_edges = [edge for edge in graph["data"]["edges"] if edge.get("candidate")]
    assert any(edge["callee_name"] == "read_impl" for edge in candidate_edges)

    callbacks = envelope(3)
    assert callbacks["ok"] is True
    assert callbacks["data"]["count"] >= 1
    assert any(item["object_path"].startswith("g_ops") for item in callbacks["data"]["items"])

    stats = envelope(4)
    assert stats["ok"] is True
    assert stats["data"]["callbacks"]["registrations"] >= 1


def _run_cli_json(argv: list[str]) -> tuple[int, dict, str]:
    import contextlib
    import io

    from ca.cli.main import main

    buffer = io.StringIO()
    errors = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(errors):
        code = main(argv)
    text = buffer.getvalue().strip()
    return code, (json.loads(text) if text else {}), errors.getvalue()
