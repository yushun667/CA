"""End-to-end tests for header-dependency-aware incremental update/watch.

The parser contract records each TU's direct repo-local includes
(``ParseResult.include_files``) plus repo-local include edges
(``include_edges``, including header->header); the store persists their
transitive closure in ``tu_includes``.  ``ca update``/``ca watch`` use that map
to invalidate only dependent TUs instead of clearing the whole cache.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))

from test_e2e_pipeline import write_fixture_repo  # noqa: E402


def _run(argv: list[str]) -> tuple[int, str, str]:
    from ca.cli.main import main

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


def _repo_with_extra_tu(tmp_path: Path) -> tuple[Path, Path]:
    repo = write_fixture_repo(tmp_path / "repo")
    # header chain: include/base.h <- include/os.h <- kernel/*.c
    (repo / "include" / "base.h").write_text(
        "int base_header_helper(int value);\n", encoding="utf-8"
    )
    os_header = repo / "include" / "os.h"
    os_header.write_text(
        '#include "base.h"\n' + os_header.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    # A third TU that includes neither os.h nor base.h.
    (repo / "include" / "extra.h").write_text(
        "int extra_header_only(int value);\n", encoding="utf-8"
    )
    (repo / "kernel" / "extra.c").write_text(
        '#include "extra.h"\n\nint extra_tu_function(void)\n{\n    return 3;\n}\n',
        encoding="utf-8",
    )
    compdb = repo / "compile_commands.json"
    entries = json.loads(compdb.read_text(encoding="utf-8"))
    template = next(item for item in entries if item["file"].endswith("kernel/proc.c"))
    extra = dict(template)
    extra_file = str(repo / "kernel" / "extra.c")
    extra["file"] = extra_file
    for key in ("command", "arguments"):
        if key in extra:
            if key == "command":
                extra[key] = str(extra[key]).replace(
                    str(template["file"]), extra_file
                )
            else:
                extra[key] = [
                    str(part).replace(str(template["file"]), extra_file)
                    for part in extra[key]
                ]
    entries.append(extra)
    compdb.write_text(json.dumps(entries, indent=1), encoding="utf-8")
    return repo, compdb


def _index(repo: Path, compdb: Path, db: Path, *extra: str) -> None:
    code, _out, err = _run(
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
    assert code == 0, err


def _tu_includes(db: Path) -> dict[str, list[str]]:
    from ca.store import SqliteStore

    store = SqliteStore(db)
    store.open()
    try:
        result: dict[str, list[str]] = {}
        for unit in store.iter_records(kinds=("compilation_unit",)):
            result[str(unit["file"])] = store.tu_includes(str(unit["id"]))
        return result
    finally:
        store.close()


def _update(repo: Path, compdb: Path, db: Path, changed: str) -> tuple[int, str]:
    code, _out, err = _run(
        [
            "update",
            "--repo",
            str(repo),
            "--compdb",
            str(compdb),
            "--db",
            str(db),
            "--jobs",
            "1",
            "--changed-files",
            changed,
        ]
    )
    return code, err


def _functions(db: Path, pattern: str) -> list[dict]:
    from ca.store import SqliteStore

    store = SqliteStore(db)
    store.open()
    try:
        return store.query_functions(pattern, limit=10)
    finally:
        store.close()


def test_index_writes_direct_and_transitive_includes(tmp_path: Path) -> None:
    repo, compdb = _repo_with_extra_tu(tmp_path)
    db = tmp_path / "includes.db"
    _index(repo, compdb, db)
    includes = _tu_includes(db)
    proc_includes = next(value for key, value in includes.items() if key.endswith("kernel/proc.c"))
    extra_includes = next(value for key, value in includes.items() if key.endswith("kernel/extra.c"))
    # direct include + transitive header->header edge from include/os.h
    assert "include/base.h" in proc_includes
    assert "include/os.h" in proc_includes
    assert extra_includes == ["include/extra.h"]
    _code, out, err = _run(
        ["stats", "--db", str(db), "--json"]
    )
    assert _code == 0, err
    stats = json.loads(out)
    assert stats["tables"]["tu_includes"] >= 4
    # record_scope=main_file still records TU-level include facts.
    main_db = tmp_path / "includes-main-file.db"
    _index(repo, compdb, main_db, "--record-scope", "main_file")
    main_includes = _tu_includes(main_db)
    assert any(value for value in main_includes.values())


def test_header_change_invalidates_only_dependent_tus(tmp_path: Path) -> None:
    repo, compdb = _repo_with_extra_tu(tmp_path)
    db = tmp_path / "update.db"
    _index(repo, compdb, db)
    header = repo / "include" / "os.h"
    header.write_text(
        header.read_text(encoding="utf-8")
        + "\nint dependency_probe(int value);\n",
        encoding="utf-8",
    )
    code, err = _update(repo, compdb, db, "include/os.h")
    assert code == 0, err
    assert "include dependencies" in err
    assert "dependency-aware" in err
    assert "cache hits=1/3" in err  # extra.c stays cached
    assert "misses=2" in err
    assert _functions(db, "dependency_probe")
    assert _functions(db, "extra_tu_function")


def test_transitive_header_change_maps_back_to_tu(tmp_path: Path) -> None:
    repo, compdb = _repo_with_extra_tu(tmp_path)
    db = tmp_path / "transitive.db"
    _index(repo, compdb, db)
    base = repo / "include" / "base.h"
    base.write_text(
        base.read_text(encoding="utf-8") + "\nint transitive_probe(int value);\n",
        encoding="utf-8",
    )
    code, err = _update(repo, compdb, db, "include/base.h")
    assert code == 0, err
    assert "include dependencies" in err
    assert "misses=2" in err
    assert "cache hits=1/3" in err
    assert _functions(db, "transitive_probe")


def test_unrelated_header_change_is_noop(tmp_path: Path) -> None:
    repo, compdb = _repo_with_extra_tu(tmp_path)
    db = tmp_path / "unrelated.db"
    _index(repo, compdb, db)
    unrelated = repo / "include" / "unrelated.h"
    unrelated.write_text("int unrelated_probe(int value);\n", encoding="utf-8")
    code, err = _update(repo, compdb, db, "include/unrelated.h")
    assert code == 2
    assert "not included by any indexed TU" in err
    assert not _functions(db, "unrelated_probe")


def test_missing_include_data_falls_back_to_full_rebuild(tmp_path: Path) -> None:
    repo, compdb = _repo_with_extra_tu(tmp_path)
    db = tmp_path / "fallback.db"
    _index(repo, compdb, db)
    # Simulate a pre-include index: no dependency rows survive.
    connection = sqlite3.connect(db)
    connection.execute("DELETE FROM tu_includes")
    connection.commit()
    connection.close()
    header = repo / "include" / "os.h"
    header.write_text(
        header.read_text(encoding="utf-8") + "\nint fallback_probe(int value);\n",
        encoding="utf-8",
    )
    code, err = _update(repo, compdb, db, "include/os.h")
    assert code == 0, err
    assert "no include dependency information" in err
    assert "cache hits=0/3" in err
    assert _functions(db, "fallback_probe")


def test_watch_uses_dependency_map(tmp_path: Path) -> None:
    repo, compdb = _repo_with_extra_tu(tmp_path)
    db = tmp_path / "watch.db"
    _index(repo, compdb, db)
    _run(["watch", "--repo", str(repo), "--compdb", str(compdb), "--db", str(db), "--jobs", "1", "--debounce", "0.1", "--json", "--once"])
    header = repo / "include" / "os.h"
    header.write_text(
        header.read_text(encoding="utf-8") + "\nint watch_dep_probe(int value);\n",
        encoding="utf-8",
    )
    code, out, err = _run(
        [
            "watch", "--repo", str(repo), "--compdb", str(compdb), "--db", str(db),
            "--jobs", "1", "--debounce", "0.1", "--json", "--once",
        ]
    )
    assert code == 0, err
    cycle = json.loads(out)
    assert cycle["header_change"] is True
    assert cycle["dependency_aware"] is True
    assert cycle["fallback_full"] is False
    assert cycle["affected_count"] == 2
    assert cycle["cache_hits"] == 1
    assert cycle["cache_misses"] == 2
    assert _functions(db, "watch_dep_probe")
    # Unrelated header: watch reports no affected TUs and does not update.
    (repo / "include" / "other.h").write_text("int other_probe(int value);\n", encoding="utf-8")
    code, out, err = _run(
        [
            "watch", "--repo", str(repo), "--compdb", str(compdb), "--db", str(db),
            "--jobs", "1", "--debounce", "0.1", "--json", "--once",
        ]
    )
    assert code == 0, err
    cycle = json.loads(out)
    assert cycle["affected_count"] == 0
    assert cycle["updated"] is False
