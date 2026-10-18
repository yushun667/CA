"""Unit/smoke tests for the native watch backends (task-48)."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import time
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))

from test_e2e_pipeline import run_cli, write_fixture_repo  # noqa: E402

from ca.cli import native_watch  # noqa: E402


def _index(repo: Path, db: Path) -> None:
    assert run_cli(
        ["index", "--repo", str(repo), "--db", str(db), "--jobs", "1", "--incremental"]
    ) == 0


def _watch_json(argv: list[str]) -> tuple[int, dict, str]:
    from ca.cli.main import main

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    text = out.getvalue().strip()
    return code, (json.loads(text) if text else {}), err.getvalue()


def test_event_filtering_skips_and_dedupe(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / "out").mkdir()
    (repo / "node_modules").mkdir()
    values = [
        str(repo / "src" / "a.c"),
        str(repo / "src" / "a.c"),
        str(repo / "src" / "b.hpp"),
        str(repo / "src" / "notes.txt"),
        str(repo / ".git" / "config.c"),
        str(repo / "out" / "gen.c"),
        str(repo / "node_modules" / "x.c"),
        "src/relative.cpp",
    ]
    selected = native_watch.filter_event_paths(repo, values)
    names = {Path(path).name for path in selected}
    assert names == {"a.c", "b.hpp", "relative.cpp"}
    assert len(selected) == 3  # duplicate a.c removed

    watcher = native_watch.NativeWatcher(repo)
    watcher._push(str(repo / "src" / "a.c"))
    watcher._push(str(repo / "src" / "a.c"))
    watcher._push(str(repo / ".git" / "x.c"))
    watcher._push(str(repo / "src" / "notes.txt"))
    waited = watcher.wait(0.05)
    assert [Path(path).name for path in waited] == ["a.c"]


def test_auto_falls_back_to_poll_with_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = write_fixture_repo(tmp_path / "repo")
    db = tmp_path / "watch.db"
    _index(repo, db)
    calls: list[bool] = []

    def unavailable(path, *, start=True):
        calls.append(start)
        return None

    monkeypatch.setattr(native_watch, "create_native_watcher", unavailable)
    monkeypatch.setattr(
        native_watch, "availability", lambda: (False, "mock native unavailable")
    )
    code, payload, err = _watch_json(
        [
            "watch", "--repo", str(repo), "--db", str(db), "--json", "--once",
            "--backend", "auto",
        ]
    )
    assert code == 0
    assert payload["backend"] == "poll"
    assert "mock native unavailable" in payload["fallback_reason"]
    assert "fallback=mock native unavailable" in err


def test_explicit_native_fails_when_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = write_fixture_repo(tmp_path / "repo")
    db = tmp_path / "watch.db"
    _index(repo, db)
    monkeypatch.setattr(
        native_watch,
        "create_native_watcher",
        lambda path, *, start=True: None,
    )
    monkeypatch.setattr(
        native_watch, "availability", lambda: (False, "mock native unavailable")
    )
    code, payload, err = _watch_json(
        [
            "watch", "--repo", str(repo), "--db", str(db), "--json", "--once",
            "--backend", "native",
        ]
    )
    assert code == 1
    assert payload == {}
    assert "mock native unavailable" in err


def test_invalid_backend_rejected(tmp_path: Path) -> None:
    repo = write_fixture_repo(tmp_path / "repo")
    db = tmp_path / "watch.db"
    _index(repo, db)
    code, _payload, err = _watch_json(
        [
            "watch", "--repo", str(repo), "--db", str(db), "--json", "--once",
            "--backend", "bogus",
        ]
    )
    assert code != 0
    assert "invalid choice" in err


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS FSEvents only")
def test_macos_fsevents_smoke(tmp_path: Path) -> None:
    watcher = native_watch.create_native_watcher(tmp_path)
    if watcher is None:
        pytest.skip("FSEvents backend unavailable")
    try:
        target = tmp_path / "native_probe.cpp"
        target.write_text("int probe;\n", encoding="utf-8")
        time.sleep(0.3)
        events = watcher.wait(2.0)
        assert any(Path(event).name == "native_probe.cpp" for event in events), events
    finally:
        watcher.stop()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux inotify only")
def test_linux_inotify_smoke(tmp_path: Path) -> None:
    watcher = native_watch.create_native_watcher(tmp_path)
    if watcher is None:
        pytest.skip("inotify backend unavailable")
    try:
        target = tmp_path / "native_probe.c"
        target.write_text("int probe;\n", encoding="utf-8")
        time.sleep(0.2)
        events = watcher.wait(2.0)
        assert any(Path(event).name == "native_probe.c" for event in events), events
        # New directories must be watched automatically.
        child = tmp_path / "generated"
        child.mkdir()
        time.sleep(0.2)
        nested = child / "nested.h"
        nested.write_text("int nested;\n", encoding="utf-8")
        time.sleep(0.2)
        assert any(Path(event).name == "nested.h" for event in watcher.wait(2.0))
    finally:
        watcher.stop()


def test_event_paths_drive_update_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Native event paths are passed directly to the incremental update runner."""

    from types import SimpleNamespace

    from ca.cli import watch as watch_module
    from ca.cli.watcher import snapshot

    repo = write_fixture_repo(tmp_path / "repo")
    db = tmp_path / "watch.db"
    _index(repo, db)
    config = watch_module.WatchConfig(
        repo=str(repo), db=str(db), json_output=False, backend="native"
    )
    previous = snapshot(repo)
    target = repo / "kernel" / "main.c"
    target.write_text(
        target.read_text(encoding="utf-8")
        + "\nint native_event_probe(void)\n{\n    return 9;\n}\n",
        encoding="utf-8",
    )
    captured: dict = {}

    def fake_update(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            ok=True,
            plan=SimpleNamespace(
                affected_count=1,
                header_change=False,
                dependency_aware=False,
                fallback_full=False,
            ),
            cache_stats={"hits": 1, "misses": 1},
            db_write={"mode": "incremental"},
            incremental_rows={"removed": 1, "added": 1},
            callback_refresh={"mode": "skipped"},
            db_size=1,
            elapsed_seconds=0.01,
            exit_code=0,
            error="",
        )

    result, manifest = watch_module.run_cycle(
        config,
        2,
        previous,
        update_runner=fake_update,
        event_paths=[str(target)],
    )
    assert result.changed == ["kernel/main.c"]
    assert captured["changed_files"] == ["kernel/main.c"]
    assert manifest is not None
