"""Store-level tests for header include dependencies (``tu_includes``).

The parser contract provides ``ParseResult.include_files`` (direct repo-local
includes) and ``include_edges`` ((parent, child), including header->header).
The store persists the transitive closure per TU so a changed deep header still
maps back to every dependent TU, and old databases gain the table idempotently.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ca.core.schema import CompilationUnit, ParseResult
from ca.store import SqliteStore


def _unit(file: str = "/repo/kernel/a.c", tu_id: str = "tu:include-test") -> CompilationUnit:
    return CompilationUnit(
        id=tu_id,
        file=file,
        directory=str(Path(file).parent),
        repo_root="/repo",
        arguments=["clang", "-c", file],
        language="c",
    )


def _parse(files: tuple[str, ...], edges: tuple[tuple[str, str], ...]) -> ParseResult:
    return ParseResult(tu=_unit(), include_files=files, include_edges=edges)


def test_include_closure_and_dependent_queries(tmp_path: Path) -> None:
    db = tmp_path / "includes.db"
    store = SqliteStore(db)
    store.open()
    store.begin()
    store.add_parse_result(
        _parse(
            ("include/a.h", "include/b.h"),
            (
                ("kernel/a.c", "include/a.h"),
                ("include/a.h", "include/deep.h"),
                ("include/b.h", "include/other.h"),
            ),
        )
    )
    store.commit()
    store.finalize()
    try:
        assert store.has_tu_includes() is True
        assert store.tu_includes("tu:include-test") == [
            "include/a.h",
            "include/b.h",
            "include/deep.h",
            "include/other.h",
        ]
        assert store.dependent_tu_ids(["include/deep.h"]) == ["tu:include-test"]
        assert store.dependent_tu_ids(["include/a.h", "include/missing.h"]) == [
            "tu:include-test"
        ]
        assert store.dependent_tu_ids([]) == []
        rows = store.dependent_tus(["include/other.h"])
        assert len(rows) == 1
        assert rows[0]["file"] == "/repo/kernel/a.c"
        assert store.stats()["tables"]["tu_includes"] == 4
    finally:
        store.close()


def test_empty_includes_write_nothing(tmp_path: Path) -> None:
    db = tmp_path / "empty.db"
    store = SqliteStore(db)
    store.open()
    store.begin()
    store.add_parse_result(ParseResult(tu=_unit()))
    store.commit()
    try:
        assert store.has_tu_includes() is False
        assert store.tu_includes("tu:include-test") == []
        assert store.dependent_tu_ids(["include/a.h"]) == []
    finally:
        store.close()


def test_old_database_upgrades_idempotently(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    store = SqliteStore(db)
    store.open()
    store.begin()
    store.add_parse_result(_parse(("include/a.h",), ()))
    store.commit()
    store.close()

    # Simulate a pre-include (store schema v5) database: no tu_includes table.
    connection = sqlite3.connect(db)
    connection.execute("DROP TABLE tu_includes")
    connection.execute(
        "UPDATE meta SET value = '5' WHERE key = 'store_schema_version'"
    )
    connection.commit()
    connection.close()

    upgraded = SqliteStore(db)
    upgraded.open()
    try:
        assert upgraded.has_tu_includes() is False  # empty table created
        upgraded.begin()
        upgraded.add_tu_includes("tu:include-test", ["include/new.h"])
        upgraded.commit()
        assert upgraded.dependent_tu_ids(["include/new.h"]) == ["tu:include-test"]
    finally:
        upgraded.close()
    # Opening again is idempotent (table/index already there).
    again = SqliteStore(db)
    again.open()
    try:
        assert again.has_tu_includes() is True
    finally:
        again.close()
