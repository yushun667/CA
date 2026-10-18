"""Store-side C++ virtual dispatch tests (task-37).

Covers persistence of override/inheritance metadata, query-time cross-TU
``virtual_candidate`` edges merged into callers/callees, conservative behavior
(no override -> no candidate), and idempotent upgrade of pre-v7 databases.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

from ca.analyze import analyze_function
from ca.core import ids
from ca.core.schema import SCHEMA_VERSION, CompilationUnit
from ca.parse import ParseOptions, parse_compilation_unit
from ca.store import SqliteStore, writer
from ca.store import virtual_dispatch

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def make_unit(filename: str) -> CompilationUnit:
    file_path = (FIXTURES / filename).resolve()
    arguments = ("-std=c++17",)
    return CompilationUnit(
        id=ids.tu_id(str(file_path), str(FIXTURES), arguments),
        file=str(file_path),
        directory=str(FIXTURES),
        repo_root=str(FIXTURES),
        arguments=arguments,
        language="c++",
        source="test",
    )


@pytest.fixture()
def virtual_store():
    db = os.path.join(tempfile.mkdtemp(), "virtual.db")
    store = SqliteStore(db)
    store.open()
    store.begin()
    for name in ("ast_virtual_base.cpp", "ast_virtual_derived.cpp"):
        parsed = parse_compilation_unit(make_unit(name))
        assert parsed.parse_ok is True, parsed.error
        store.add_parse_result(parsed.parse_result())
        for function in parsed.functions:
            if function.is_definition:
                store.add_analysis_result(analyze_function(parsed, function))
    store.commit()
    store.finalize()
    yield store
    store.close()


def test_metadata_persisted(virtual_store) -> None:
    connection = virtual_store.connection
    assert connection.execute("SELECT COUNT(*) FROM virtual_overrides").fetchone()[0] >= 2
    assert connection.execute("SELECT COUNT(*) FROM type_bases").fetchone()[0] >= 2
    row = connection.execute(
        "SELECT is_virtual, is_pure_virtual, overrides_json FROM functions "
        "WHERE qualified_name = 'ast_virtual::Derived::value'"
    ).fetchone()
    assert row is not None and row["is_virtual"] == 1 and row["overrides_json"] != "[]"


def test_callees_merge_cross_tu_virtual_candidates(virtual_store) -> None:
    rows = virtual_store.query_callees("call_through_base", limit=50)
    candidates = [row for row in rows if row["kind"] == "virtual_candidate"]
    names = {row["callee_name"] for row in candidates}
    assert names == {"value"}
    confidences = sorted(row["confidence"] for row in candidates)
    assert confidences == [0.5, 0.6]
    derived = [row for row in candidates if row["confidence"] == 0.6]
    assert derived and derived[0]["callee_file"] == "ast_virtual_derived.cpp"
    # Direct virtual call edge is still present and kept as the direct source.
    direct = [row for row in rows if row["kind"] == "virtual" and row["source"] == "call"]
    assert direct


def test_callers_merge_virtual_candidates(virtual_store) -> None:
    rows = virtual_store.query_callers("ast_virtual::Derived::value", limit=50)
    candidates = [
        row for row in rows if row["kind"] == "virtual_candidate" and row["confidence"] == 0.6
    ]
    assert candidates
    assert candidates[0]["caller_name"] == "call_through_base"


def test_no_override_means_no_virtual_candidate(virtual_store) -> None:
    rows = virtual_store.query_callees("call_unrelated", limit=50)
    assert [row for row in rows if row["kind"] == "virtual_candidate"] == []


def test_old_database_upgrade_is_idempotent() -> None:
    db = os.path.join(tempfile.mkdtemp(), "old.db")
    connection = sqlite3.connect(db)
    connection.executescript(
        """
        CREATE TABLE functions (id TEXT PRIMARY KEY, tu_id TEXT, name TEXT,
            qualified_name TEXT, usr TEXT, kind TEXT, file TEXT, line INTEGER,
            col INTEGER, end_line INTEGER, end_col INTEGER, is_definition INTEGER,
            return_type TEXT, signature TEXT, display_name TEXT, storage_class TEXT,
            linkage TEXT, is_static INTEGER, is_inline INTEGER, is_variadic INTEGER,
            parent_id TEXT, params_json TEXT);
        CREATE TABLE types (id TEXT PRIMARY KEY, tu_id TEXT, name TEXT,
            qualified_name TEXT, usr TEXT, kind TEXT, file TEXT, line INTEGER,
            col INTEGER, end_line INTEGER, end_col INTEGER, is_definition INTEGER,
            size_bytes INTEGER, align_bytes INTEGER, underlying_type TEXT,
            parent_id TEXT, is_anonymous INTEGER);
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '');
        """
    )
    connection.commit()
    connection.close()

    for _ in range(2):  # idempotent across repeated opens
        store = SqliteStore(db)
        store.open()
        connection = store.connection
        function_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(functions)")
        }
        type_columns = {row[1] for row in connection.execute("PRAGMA table_info(types)")}
        assert {"is_virtual", "is_pure_virtual", "overrides_json"} <= function_columns
        assert "bases_json" in type_columns
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='virtual_overrides'"
        ).fetchone()
        assert connection.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0] == SCHEMA_VERSION
        store.close()


def test_candidate_queries_degrade_without_virtual_tables(virtual_store) -> None:
    connection = virtual_store.connection
    connection.execute("DROP TABLE virtual_overrides")
    connection.execute("DROP TABLE type_bases")
    # Cache was populated by earlier queries; reset so the capability check reruns.
    virtual_store._queries.reset_caches()
    rows = virtual_store.query_callees("call_through_base", limit=50)
    # Same-TU candidates persisted by analyze remain; query-time cross-TU pass
    # must not raise on an old/layout-limited database.
    assert rows
    assert all(row["kind"] in {"virtual", "virtual_candidate"} for row in rows)


def test_cross_tu_candidates_work_without_materialized_call_targets() -> None:
    """Regression: query-time override merge must actually run (binding/LIMIT).

    The buggy version passed one extra bind value while the SQL had no LIMIT
    placeholder; sqlite3 raised ProgrammingError which was swallowed by the
    broad ``except sqlite3.Error``, so cross-TU virtual candidates were always
    empty. This test builds a store from parse results only (no analyze pass, so
    ``call_targets`` contains no materialized virtual candidates) and requires
    ``forward_edges``/``reverse_edges`` plus query_callees/query_callers to
    return query-time rows with the expected confidences.
    """

    db = os.path.join(tempfile.mkdtemp(), "virtual_query_time.db")
    store = SqliteStore(db)
    store.open()
    store.begin()
    for name in ("ast_virtual_base.cpp", "ast_virtual_derived.cpp"):
        parsed = parse_compilation_unit(make_unit(name))
        assert parsed.parse_ok is True, parsed.error
        store.add_parse_result(parsed.parse_result())  # no add_analysis_result
    store.commit()
    store.finalize()
    try:
        connection = store.connection
        assert connection.execute("SELECT COUNT(*) FROM call_targets").fetchone()[0] == 0
        caller_id = connection.execute(
            "SELECT id FROM functions WHERE name = 'call_through_base'"
        ).fetchone()[0]
        static_id = connection.execute(
            "SELECT id FROM functions WHERE qualified_name = 'ast_virtual::Base::value'"
        ).fetchone()[0]
        derived_id = connection.execute(
            "SELECT id FROM functions WHERE qualified_name = 'ast_virtual::Derived::value'"
        ).fetchone()[0]

        forward = virtual_dispatch.forward_edges(connection, [caller_id], 50)
        assert forward, "forward virtual candidates must be non-empty (LIMIT binding bug)"
        assert sorted(round(row["confidence"], 2) for row in forward) == [0.5, 0.6]
        assert {row["callee_id"] for row in forward} == {
            derived_id,
            connection.execute(
                "SELECT id FROM functions WHERE qualified_name = 'ast_virtual::Leaf::value'"
            ).fetchone()[0],
        }
        assert all(row["source"] == "virtual_candidate" for row in forward)

        reverse = virtual_dispatch.reverse_edges(connection, [derived_id], 50)
        assert reverse, "reverse virtual candidates must be non-empty (LIMIT binding bug)"
        assert reverse[0]["callee_id"] == derived_id
        assert round(reverse[0]["confidence"], 2) == 0.6

        callees = [row for row in store.query_callees("call_through_base", limit=50)
                   if row["kind"] == "virtual_candidate"]
        assert {round(row["confidence"], 2) for row in callees} == {0.5, 0.6}
        assert all(row["source"] == "virtual_candidate" for row in callees)
        callers = [row for row in store.query_callers("ast_virtual::Derived::value", limit=50)
                   if row["kind"] == "virtual_candidate"]
        assert callers and callers[0]["caller_name"] == "call_through_base"
        assert callers[0]["source"] == "virtual_candidate"
        # The static target is still a direct call edge, never replaced.
        direct = [row for row in store.query_callees("call_through_base", limit=50)
                  if row["kind"] == "virtual" and row["source"] == "call"]
        assert direct
    finally:
        store.close()
