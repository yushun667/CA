"""End-to-end tests for ``ca watch`` (polling incremental daemon).

The self-contained mini repository from ``test_e2e_pipeline`` is indexed with a
warm incremental cache; editing a source/header then running ``ca watch --once``
must refresh the database from the cache, report cycle metrics and invalidate
deep state. The poller is not started as a background daemon, so existing tests
are unaffected.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))

from test_e2e_pipeline import run_cli, write_fixture_repo  # noqa: E402


def _run_json(argv: list[str]) -> tuple[int, dict, str]:
    from ca.cli.main import main

    buffer = io.StringIO()
    errors = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(errors):
        code = main(argv)
    text = buffer.getvalue().strip()
    return code, (json.loads(text) if text else {}), errors.getvalue()


def _index(repo: Path, db: Path, *extra: str) -> None:
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
            "--incremental",
            *extra,
        ]
    )
    assert code == 0


def _watch(
    repo: Path, db: Path, *extra: str
) -> tuple[int, dict, str]:
    return _run_json(
        [
            "watch",
            "--repo",
            str(repo),
            "--db",
            str(db),
            "--compdb",
            str(repo / "compile_commands.json"),
            "--jobs",
            "1",
            "--debounce",
            "0.1",
            "--json",
            *extra,
        ]
    )


def _store_functions(db: Path, pattern: str) -> list[str]:
    from ca.store import SqliteStore

    store = SqliteStore(str(db))
    store.open()
    try:
        return [row["name"] for row in store.query_functions(pattern)]
    finally:
        store.close()


def test_baseline_then_source_change_is_incremental(tmp_path: Path) -> None:
    repo = write_fixture_repo(tmp_path / "repo")
    db = tmp_path / "watch.db"
    _index(repo, db)

    code, baseline, err = _watch(repo, db, "--once")
    assert code == 0, err
    assert baseline["baseline"] is True
    assert baseline["changed_count"] == 0
    assert (tmp_path / "watch.db.watch.json").is_file()

    proc_c = repo / "kernel" / "proc.c"
    proc_c.write_text(
        proc_c.read_text(encoding="utf-8")
        + "\nint watch_marker(void)\n{\n    return 42;\n}\n",
        encoding="utf-8",
    )
    code, cycle, err = _watch(repo, db, "--once")
    assert code == 0, err
    assert cycle["changed_count"] == 1
    assert cycle["updated"] is True
    assert cycle["affected_count"] == 1
    assert cycle["header_change"] is False
    assert cycle["cache_hits"] >= 1  # main.c unchanged
    assert cycle["deep_invalidated"] is True
    assert "watch_marker" in _store_functions(db, "watch_marker")

    # Unchanged tree: no update is triggered.
    code, second, err = _watch(repo, db, "--once")
    assert code == 0, err
    assert second["changed_count"] == 0
    assert second["updated"] is False


def test_header_change_forces_full_rebuild(tmp_path: Path) -> None:
    repo = write_fixture_repo(tmp_path / "repo")
    db = tmp_path / "header-watch.db"
    _index(repo, db)
    assert _watch(repo, db, "--once")[0] == 0

    header = repo / "include" / "os.h"
    header.write_text(
        header.read_text(encoding="utf-8") + "\nint watch_header_probe(int value);\n",
        encoding="utf-8",
    )
    code, cycle, err = _watch(repo, db, "--once")
    assert code == 0, err
    assert cycle["header_change"] is True
    assert cycle["affected_count"] == 2
    assert cycle["updated"] is True
    assert "watch_header_probe" in _store_functions(db, "watch_header_probe")


def test_new_source_missing_from_compdb_is_skipped(tmp_path: Path) -> None:
    repo = write_fixture_repo(tmp_path / "repo")
    db = tmp_path / "stale-watch.db"
    _index(repo, db)
    assert _watch(repo, db, "--once")[0] == 0

    (repo / "kernel" / "extra.c").write_text(
        "int extra_function(void)\n{\n    return 1;\n}\n", encoding="utf-8"
    )
    code, cycle, err = _watch(repo, db, "--once")
    assert code == 0, err
    assert cycle["skipped"] is True
    assert cycle["compdb_stale"] is True
    assert cycle["skip_reason"] == "compdb_stale"
    assert cycle["updated"] is False
    assert any("compile database" in message for message in cycle["errors"])
    assert "extra_function" not in _store_functions(db, "extra_function")


def test_base_layer_deep_status_is_invalidated(tmp_path: Path) -> None:
    from ca.cli import deep as deep_module
    from ca.store import SqliteStore

    repo = write_fixture_repo(tmp_path / "repo")
    db = tmp_path / "deep-watch.db"
    _index(repo, db, "--layer", "base")
    assert _watch(repo, db, "--once")[0] == 0
    code = run_cli(
        [
            "ensure-deep",
            "--db",
            str(db),
            "--repo",
            str(repo),
            "--compdb",
            str(repo / "compile_commands.json"),
            "--symbols",
            "proc_alloc",
        ]
    )
    assert code == 0
    store = SqliteStore(str(db))
    store.open()
    try:
        before = deep_module.deep_stats(str(db), store)
    finally:
        store.close()
    assert before["deep_ready"] >= 1

    proc_c = repo / "kernel" / "proc.c"
    proc_c.write_text(
        proc_c.read_text(encoding="utf-8") + "\nint deep_watch_marker(void)\n{\n    return 0;\n}\n",
        encoding="utf-8",
    )
    code, cycle, err = _watch(repo, db, "--once", "--layer", "base")
    assert code == 0, err
    assert cycle["updated"] is True
    store = SqliteStore(str(db))
    store.open()
    try:
        after = deep_module.deep_stats(str(db), store)
        assert after.get("mode") == "base"
        assert after["deep_ready"] == 0
    finally:
        store.close()


def test_lock_prevents_second_watcher(tmp_path: Path) -> None:
    from ca.cli.watch import _WatchLock

    repo = write_fixture_repo(tmp_path / "repo")
    db = tmp_path / "lock-watch.db"
    _index(repo, db)
    lock = _WatchLock(Path(str(db) + ".watch.lock"))
    lock.acquire()
    try:
        code, _payload, err = _watch(repo, db, "--once")
        assert code == 2
        assert "already running" in err
    finally:
        lock.release()
    assert _watch(repo, db, "--once")[0] == 0


def test_manual_changed_files_batch(tmp_path: Path) -> None:
    repo = write_fixture_repo(tmp_path / "repo")
    db = tmp_path / "manual-watch.db"
    _index(repo, db)
    proc_c = repo / "kernel" / "proc.c"
    proc_c.write_text(
        proc_c.read_text(encoding="utf-8") + "\nint manual_watch_marker(void)\n{\n    return 1;\n}\n",
        encoding="utf-8",
    )
    code, cycle, err = _watch(repo, db, "--changed-files", "kernel/proc.c")
    assert code == 0, err
    assert cycle["updated"] is True
    assert cycle["affected_count"] == 1
    assert "manual_watch_marker" in _store_functions(db, "manual_watch_marker")


def test_failed_update_does_not_advance_snapshot(tmp_path: Path) -> None:
    from ca.cli import watch as watch_module
    from ca.cli.watcher import diff_snapshots, snapshot

    repo = write_fixture_repo(tmp_path / "repo")
    db = tmp_path / "retry-watch.db"
    _index(repo, db)
    config = watch_module.WatchConfig(
        repo=str(repo),
        db=str(db),
        compdb=str(repo / "compile_commands.json"),
        debounce=0.05,
        jobs=1,
    )
    previous = snapshot(repo)
    proc_c = repo / "kernel" / "proc.c"
    proc_c.write_text(
        proc_c.read_text(encoding="utf-8") + "\nint retry_marker(void)\n{\n    return 2;\n}\n",
        encoding="utf-8",
    )

    def failing_runner(**kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            exit_code=1,
            ok=False,
            plan=SimpleNamespace(affected_count=1, header_change=False),
            cache_stats={},
            db_size=0,
            elapsed_seconds=0.01,
            error="simulated failure",
        )

    result, manifest = watch_module.run_cycle(
        config, 1, previous, update_runner=failing_runner
    )
    assert result.updated is False
    assert result.errors
    # Previous snapshot is kept, so the next cycle retries the same change set.
    assert manifest == previous
    changed = diff_snapshots(manifest, snapshot(repo))
    assert changed
