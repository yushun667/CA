"""End-to-end tests for in-place incremental DB writes (``ca update``/watch).

Covers: only affected TU rows change, header dependency scoping, FTS updates,
callback candidate refresh (same result as a fresh full index) and watch metrics.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))

from test_e2e_includes import _repo_with_extra_tu  # noqa: E402
from test_e2e_pipeline import run_cli, write_fixture_repo  # noqa: E402

from ca.store import SqliteStore  # noqa: E402


def _index(repo: Path, compdb: Path, db: Path, *extra: str) -> None:
    code = run_cli(
        [
            "index",
            "--repo",
            str(repo),
            "--compdb",
            str(compdb),
            "--db",
            str(db),
            "--jobs",
            "1",
            "--incremental",
            *extra,
        ]
    )
    assert code == 0


def _update(repo: Path, compdb: Path, db: Path, changed: list[str], *extra: str):
    from ca.cli.commands import run_update

    return run_update(
        repo=str(repo),
        db=str(db),
        compdb=str(compdb),
        changed_files=changed,
        jobs=1,
        *extra,
    )


def _rows(db: Path, table: str) -> list[dict]:
    store = SqliteStore(db)
    store.open()
    try:
        return [dict(row) for row in store.connection.execute(f"SELECT * FROM {table}")]
    finally:
        store.close()


def _functions(db: Path, pattern: str) -> list[str]:
    store = SqliteStore(db)
    store.open()
    try:
        return [row["name"] for row in store.query_functions(pattern, limit=20)]
    finally:
        store.close()


def test_source_update_replaces_one_tu_in_place(tmp_path: Path) -> None:
    repo, compdb = _repo_with_extra_tu(tmp_path)
    db = tmp_path / "index.db"
    _index(repo, compdb, db)
    before = {
        table: _rows(db, table)
        for table in ("tus", "functions", "types", "fields", "calls", "tu_includes")
    }
    proc = repo / "kernel" / "proc.c"
    proc.write_text(
        proc.read_text(encoding="utf-8")
        + "\nint incremental_probe(int value)\n{\n    return value + 2;\n}\n",
        encoding="utf-8",
    )
    result = _update(repo, compdb, db, ["kernel/proc.c"])
    assert result.exit_code == 0, result.error
    assert result.db_write["mode"] == "incremental"
    assert result.db_write["tu_count"] == 1
    assert result.incremental_rows["removed"] > 0
    assert result.incremental_rows["added"] > 0
    assert result.cache_stats["hits"] == 2 and result.cache_stats["misses"] == 1

    after = {
        table: _rows(db, table)
        for table in ("tus", "functions", "types", "fields", "calls", "tu_includes")
    }
    # The two non-affected TUs' rows are byte-identical (same ids/contents).
    affected_tu_ids = set(result.db_write["tu_ids"])

    def key(row: dict) -> bytes:
        return json.dumps(row, sort_keys=True, default=str).encode("utf-8")

    for table in ("functions", "calls", "types", "fields"):
        before_rows = sorted(
            (row for row in before[table] if row.get("tu_id") not in affected_tu_ids),
            key=key,
        )
        after_rows = sorted(
            (row for row in after[table] if row.get("tu_id") not in affected_tu_ids),
            key=key,
        )
        assert before_rows == after_rows, table
    before_tus = sorted(
        (row for row in before["tus"] if row.get("id") not in affected_tu_ids), key=key
    )
    after_tus = sorted(
        (row for row in after["tus"] if row.get("id") not in affected_tu_ids), key=key
    )
    assert before_tus == after_tus

    # FTS was maintained incrementally: the new symbol is searchable.
    assert "incremental_probe" in _functions(db, "incremental_probe")
    assert "extra_tu_function" in _functions(db, "extra_tu_function")
    assert result.incremental_rows["fts_deleted"] > 0
    assert result.incremental_rows["fts_inserted"] > 0


def test_header_update_scopes_dependent_tus(tmp_path: Path) -> None:
    repo, compdb = _repo_with_extra_tu(tmp_path)
    db = tmp_path / "header.db"
    _index(repo, compdb, db)
    before = {table: _rows(db, table) for table in ("tus", "functions", "tu_includes")}
    header = repo / "include" / "os.h"
    header.write_text(
        header.read_text(encoding="utf-8")
        + "\nint incremental_header_probe(int value);\n",
        encoding="utf-8",
    )
    result = _update(repo, compdb, db, ["include/os.h"])
    assert result.exit_code == 0, result.error
    assert result.db_write["mode"] == "incremental"
    assert result.db_write["tu_count"] == 2
    assert result.cache_stats["hits"] == 1 and result.cache_stats["misses"] == 2
    assert "incremental_header_probe" in _functions(db, "incremental_header_probe")

    after = {table: _rows(db, table) for table in ("tus", "functions", "tu_includes")}
    affected_tu_ids = set(result.db_write["tu_ids"])
    extra_id = next(row["id"] for row in before["tus"] if row["file"].endswith("extra.c"))
    assert extra_id not in affected_tu_ids
    assert [row for row in after["tus"] if row["id"] == extra_id] == [
        row for row in before["tus"] if row["id"] == extra_id
    ]
    assert [row for row in after["functions"] if row.get("tu_id") == extra_id] == [
        row for row in before["functions"] if row.get("tu_id") == extra_id
    ]


def test_build_file_falls_back_to_full_rebuild(tmp_path: Path) -> None:
    repo, compdb = _repo_with_extra_tu(tmp_path)
    db = tmp_path / "fallback.db"
    _index(repo, compdb, db)
    build_file = repo / "Makefile"
    build_file.write_text("all:\n\ttrue\n", encoding="utf-8")
    # The planner treats build files as a full rebuild (same as old behaviour).
    result = _update(repo, compdb, db, ["Makefile"])
    assert result.exit_code == 0, result.error
    assert result.db_write["mode"] == "full_rebuild"


def test_callback_refresh_matches_full_index(tmp_path: Path) -> None:
    from test_e2e_callbacks import _write_callback_repo

    repo = _write_callback_repo(tmp_path)
    db = tmp_path / "incremental-callbacks.db"
    _index(repo, repo / "compile_commands.json", db, "--resolve-callbacks")
    # Change the registration target in one TU.
    registration = repo / "register.c"
    registration.write_text(
        registration.read_text(encoding="utf-8").replace(
            "g_ops.read = read_impl;", "g_ops.read = write_impl;"
        ),
        encoding="utf-8",
    )
    result = _update(repo, repo / "compile_commands.json", db, ["register.c"])
    assert result.exit_code == 0, result.error
    assert result.callback_refresh["mode"] == "rerun"
    assert result.callback_refresh["candidate_edges"] >= 1

    # Fresh full index with the same callback pass is the reference.
    fresh = tmp_path / "fresh.db"
    _index(repo, repo / "compile_commands.json", fresh, "--resolve-callbacks")

    def edges(path: Path) -> list[tuple]:
        store = SqliteStore(path)
        store.open()
        try:
            return sorted(
                (
                    row["callee_name"],
                    row["kind"],
                    row.get("call_file"),
                    row.get("call_line"),
                )
                for row in store.query_callees("caller", limit=100)
            )
        finally:
            store.close()

    assert edges(db) == edges(fresh)
    # The old registration target no longer appears as a candidate.
    assert "read_impl" not in {edge[0] for edge in edges(db) if edge[1] == "indirect_candidate"}


def test_watch_once_reports_incremental_metrics(tmp_path: Path) -> None:
    repo, compdb = _repo_with_extra_tu(tmp_path)
    db = tmp_path / "watch.db"
    _index(repo, compdb, db)
    # Establish the baseline snapshot.
    code = run_cli(
        [
            "watch", "--repo", str(repo), "--compdb", str(compdb), "--db", str(db),
            "--jobs", "1", "--debounce", "0.1", "--json", "--once",
        ]
    )
    assert code == 0
    main_c = repo / "kernel" / "main.c"
    main_c.write_text(
        main_c.read_text(encoding="utf-8")
        + "\nint watch_incremental_probe(void)\n{\n    return 1;\n}\n",
        encoding="utf-8",
    )
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = run_cli(
            [
                "watch", "--repo", str(repo), "--compdb", str(compdb), "--db", str(db),
                "--jobs", "1", "--debounce", "0.1", "--json", "--once",
            ]
        )
    assert code == 0, err.getvalue()
    cycle = json.loads(out.getvalue())
    assert cycle["updated"] is True
    assert cycle["db_write"]["mode"] == "incremental"
    assert cycle["db_write"]["tu_count"] == 1
    assert cycle["incremental_rows"]["added"] > 0
    assert cycle["callback_refresh"]["mode"] == "skipped"
    assert cycle["cache_hits"] == 2 and cycle["cache_misses"] == 1
    assert "watch_incremental_probe" in _functions(db, "watch_incremental_probe")


def test_incremental_write_failure_rolls_back(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repo, compdb = _repo_with_extra_tu(tmp_path)
    db = tmp_path / "rollback.db"
    _index(repo, compdb, db)
    before = {table: _rows(db, table) for table in ("tus", "functions", "calls")}
    proc = repo / "kernel" / "proc.c"
    proc.write_text(proc.read_text(encoding="utf-8") + "\nint rollback_probe(void)\n{\n    return 0;\n}\n", encoding="utf-8")

    from ca.store import SqliteStore

    def boom(self, *args, **kwargs):
        raise RuntimeError("injected write failure")

    monkeypatch.setattr(SqliteStore, "replace_units", boom)
    result = _update(repo, compdb, db, ["kernel/proc.c"])
    assert result.exit_code == 1
    assert "incremental write failed" in result.error
    after = {table: _rows(db, table) for table in ("tus", "functions", "calls")}
    assert after == before
