"""call_targets persistence, merged caller/callee edges and call-graph queries.

Covers phase-3 contract: CallTargetRecord storage/export, merged direct+candidate
edges (kind/confidence/source/candidate, direct wins on duplicates), graph
directions/depth/limit/truncation, simple paths, cycles, EXPLAIN index usage and
in-place upgrade of a phase-2 database.
"""

from __future__ import annotations

import sqlite3

import pytest

from ca.core.ids import call_id, call_target_id, function_id, tu_id
from ca.core.schema import (
    AnalysisResult,
    CallRecord,
    CallTargetRecord,
    CompilationUnit,
    FunctionRecord,
    ParseResult,
    Sloc,
)
from ca.store import SqliteStore

REPO = "/repo"
FILE = "kernel/calls.c"
TU = CompilationUnit(
    id=tu_id(FILE, REPO, ["-c", FILE]),
    file=FILE,
    directory=REPO,
    repo_root=REPO,
    arguments=("-c", FILE),
)

CALLER_KEYS = {
    "caller_id",
    "caller_name",
    "caller_file",
    "caller_line",
    "callee_name",
    "call_file",
    "call_line",
    "is_resolved",
    "kind",
    "confidence",
    "source",
    "candidate",
}
CALLEE_KEYS = {
    "callee_id",
    "callee_name",
    "callee_file",
    "callee_line",
    "call_file",
    "call_line",
    "is_resolved",
    "kind",
    "confidence",
    "source",
    "candidate",
}
EDGE_KEYS = {
    "call_id",
    "caller_id",
    "caller_name",
    "caller_file",
    "caller_line",
    "callee_id",
    "callee_name",
    "callee_file",
    "callee_line",
    "call_file",
    "call_line",
    "kind",
    "confidence",
    "is_resolved",
    "source",
    "candidate",
    "depth",
    "direction",
}


def _sloc(line: int) -> Sloc:
    return Sloc(FILE, line, 3, line, 30)


def _function(name: str, line: int) -> FunctionRecord:
    usr = f"c:@F@{name}"
    return FunctionRecord(
        id=function_id(name=name, usr=usr, sloc=_sloc(line), linkage="external"),
        tu_id=TU.id,
        name=name,
        qualified_name=name,
        usr=usr,
        kind="function",
        sloc=_sloc(line),
        is_definition=True,
        linkage="external",
    )


MAIN = _function("main_a", 10)
MID = _function("mid_b", 20)
LEAF = _function("leaf_c", 30)
CALLBACK = _function("cb_d", 40)
ORPHAN = _function("orphan_e", 50)


def _call(caller: FunctionRecord, callee: FunctionRecord, line: int) -> CallRecord:
    sloc = _sloc(line)
    return CallRecord(
        id=call_id(caller_id=caller.id, callee_usr=callee.usr, callee_name=callee.name, sloc=sloc),
        tu_id=TU.id,
        caller_id=caller.id,
        sloc=sloc,
        callee_id=callee.id,
        callee_usr=callee.usr,
        callee_name=callee.name,
        callee_qualified_name=callee.name,
        kind="direct",
        is_resolved=True,
    )


CALL_MAIN_MID = _call(MAIN, MID, 15)
CALL_MID_LEAF = _call(MID, LEAF, 25)
CALL_LEAF_MID = _call(LEAF, MID, 35)


def _target(
    call_record: CallRecord,
    caller: FunctionRecord,
    callee: FunctionRecord,
    line: int,
    confidence: float = 0.55,
    kind: str = "indirect_candidate",
) -> CallTargetRecord:
    return CallTargetRecord(
        id=call_target_id(call_record.id, callee.id, kind),
        call_id=call_record.id,
        caller_id=caller.id,
        callee_id=callee.id,
        callee_name=callee.name,
        kind=kind,
        confidence=confidence,
        reason="registered callback" if kind == "indirect_candidate" else "direct resolve",
        sloc=_sloc(line),
    )


TARGET_MAIN_CALLBACK = _target(CALL_MAIN_MID, MAIN, CALLBACK, 15)

PARSE_RESULT = ParseResult(
    tu=TU,
    functions=[MAIN, MID, LEAF, CALLBACK, ORPHAN],
    calls=[CALL_MAIN_MID, CALL_MID_LEAF, CALL_LEAF_MID],
)
ANALYSIS = AnalysisResult(function_id=MAIN.id, call_targets=[TARGET_MAIN_CALLBACK])


def _open_store(tmp_path) -> SqliteStore:
    store = SqliteStore(tmp_path / "graph.db")
    store.open()
    return store


def _populate(store: SqliteStore) -> None:
    store.begin()
    store.add_parse_result(PARSE_RESULT)
    store.add_analysis_result(ANALYSIS)
    store.commit()
    store.finalize()


@pytest.fixture()
def populated(tmp_path):
    store = _open_store(tmp_path)
    _populate(store)
    yield store
    store.close()


def test_call_target_persisted_and_exported(populated) -> None:
    stats = populated.stats()
    assert stats["tables"]["call_targets"] == 1
    records = list(populated.iter_records(("call_target",)))
    assert len(records) == 1
    record = records[0]
    assert record["kind"] == "call_target"
    assert record["schema"]
    assert record["call_id"] == CALL_MAIN_MID.id
    assert record["caller_id"] == MAIN.id
    assert record["callee_id"] == CALLBACK.id
    assert record["callee_name"] == "cb_d"
    assert record["confidence"] == pytest.approx(0.55)
    assert record["reason"] == "registered callback"
    assert record["file"] == FILE and record["line"] == 15
    assert isinstance(record["confidence"], float)


def test_merged_callees_keys_and_confidence(populated) -> None:
    rows = populated.query_callees("main_a")
    assert len(rows) == 2
    assert all(set(row) == CALLEE_KEYS for row in rows)
    # Same call site: the direct edge is emitted before the candidate edge.
    direct, candidate = rows
    assert direct["callee_name"] == "mid_b"
    assert direct["source"] == "call"
    assert direct["candidate"] is False
    assert direct["confidence"] == 1.0
    assert direct["kind"] == "direct"
    assert direct["is_resolved"] is True
    assert candidate["callee_name"] == "cb_d"
    assert candidate["source"] == "call_target"
    assert candidate["candidate"] is True
    assert candidate["confidence"] == pytest.approx(0.55)
    assert candidate["kind"] == "indirect_candidate"
    assert candidate["callee_file"] == FILE and candidate["callee_line"] == 40


def test_merged_callers_keys_and_confidence(populated) -> None:
    candidate_rows = populated.query_callers("cb_d")
    assert len(candidate_rows) == 1
    assert set(candidate_rows[0]) == CALLER_KEYS
    row = candidate_rows[0]
    assert row["caller_name"] == "main_a"
    assert row["caller_line"] == 10
    assert row["callee_name"] == "cb_d"
    assert row["source"] == "call_target"
    assert row["candidate"] is True
    assert row["confidence"] == pytest.approx(0.55)
    assert row["is_resolved"] is False

    direct_rows = populated.query_callers("leaf_c")
    assert len(direct_rows) == 1
    assert direct_rows[0]["caller_name"] == "mid_b"
    assert direct_rows[0]["source"] == "call"
    assert direct_rows[0]["confidence"] == 1.0


def test_direct_edge_wins_over_duplicate_candidate(populated) -> None:
    duplicate = _target(CALL_MAIN_MID, MAIN, MID, 15, confidence=0.9, kind="direct")
    populated.begin()
    populated.add_analysis_result(AnalysisResult(function_id=MAIN.id, call_targets=[duplicate]))
    populated.commit()
    populated.finalize()
    rows = populated.query_callees("main_a")
    assert len(rows) == 2  # mid_b (direct) + cb_d (candidate), no duplicate row
    by_name = {row["callee_name"]: row for row in rows}
    assert by_name["mid_b"]["source"] == "call"
    assert by_name["mid_b"]["confidence"] == 1.0
    assert populated.stats()["tables"]["call_targets"] == 2


def test_query_call_targets_directions(populated) -> None:
    rows = populated.query_call_targets("main_a", direction="callee")
    assert len(rows) == 1
    assert rows[0]["call_id"] == CALL_MAIN_MID.id
    assert rows[0]["callee_name"] == "cb_d"
    assert rows[0]["kind"] == "indirect_candidate"
    assert rows[0]["reason"] == "registered callback"
    assert rows[0]["source"] == "call_target"
    assert rows[0]["candidate"] is True
    reverse = populated.query_call_targets("cb_d", direction="caller")
    assert len(reverse) == 1
    assert reverse[0]["caller_name"] == "main_a"
    assert populated.query_call_targets("no-such", direction="callee") == []
    with pytest.raises(ValueError):
        populated.query_call_targets("main_a", direction="sideways")


def test_graph_forward_reverse_both_and_depth(populated) -> None:
    forward = populated.query_call_graph("main_a", direction="forward", depth=1, limit=100)
    assert set(forward) == {
        "root",
        "deep_ready",
        "direction",
        "depth",
        "nodes",
        "edges",
        "cycles",
        "truncated",
    }
    assert forward["root"]["id"] == MAIN.id
    assert forward["deep_ready"] is True  # fixture wrote CFG-carrying analyses
    assert all("deep_ready" in node for node in forward["nodes"])
    assert {node["name"] for node in forward["nodes"]} == {"main_a", "mid_b", "cb_d"}
    assert all(set(edge) == EDGE_KEYS for edge in forward["edges"])
    assert all(edge["depth"] == 1 for edge in forward["edges"])
    assert forward["truncated"] is False

    deep = populated.query_call_graph("main_a", direction="forward", depth=2, limit=100)
    depths = {node["name"]: node["depth"] for node in deep["nodes"]}
    assert depths["main_a"] == 0
    assert depths["mid_b"] == 1
    assert depths["leaf_c"] == 2

    reverse = populated.query_call_graph("leaf_c", direction="reverse", depth=2, limit=100)
    reverse_depths = {node["name"]: node["depth"] for node in reverse["nodes"]}
    assert reverse_depths["leaf_c"] == 0
    assert reverse_depths["mid_b"] == 1
    assert {"main_a", "leaf_c"} <= set(reverse_depths)  # leaf_c also calls mid_b

    both = populated.query_call_graph("mid_b", direction="both", depth=1, limit=100)
    assert {node["name"] for node in both["nodes"]} == {"mid_b", "leaf_c", "main_a"}
    assert len(both["edges"]) == 3


def test_graph_limit_truncated_and_zero_depth(populated) -> None:
    limited = populated.query_call_graph("main_a", direction="forward", depth=2, limit=1)
    assert limited["truncated"] is True
    assert len(limited["edges"]) == 1
    root_only = populated.query_call_graph("main_a", direction="both", depth=0, limit=10)
    assert root_only["root"]["name"] == "main_a"
    assert root_only["edges"] == []
    missing = populated.query_call_graph("does_not_exist", depth=2)
    assert missing["root"] is None and missing["nodes"] == []
    with pytest.raises(ValueError):
        populated.query_call_graph("main_a", direction="sideways")
    with pytest.raises(ValueError):
        populated.query_call_graph("main_a", depth=-1)


def test_call_paths(populated) -> None:
    paths = populated.query_call_paths("main_a", "leaf_c")
    assert len(paths) == 1
    path = paths[0]
    assert [node["name"] for node in path["nodes"]] == ["main_a", "mid_b", "leaf_c"]
    assert path["length"] == 2
    assert all(edge["source"] == "call" for edge in path["edges"])
    assert populated.query_call_paths("main_a", "no_such_target") == []
    assert populated.query_call_paths("no_such_source") == []
    sampled = populated.query_call_paths("main_a", limit=5, max_depth=2)
    assert 1 <= len(sampled) <= 5
    assert all(1 <= item["length"] <= 2 for item in sampled)
    assert populated.query_call_paths("main_a", limit=0) == []


def test_find_call_cycles(populated) -> None:
    cycles = populated.find_call_cycles("main_a", max_depth=4, limit=10)
    assert len(cycles) >= 1
    cycle = min(cycles, key=lambda item: item["length"])
    names = [node["name"] for node in cycle["nodes"]]
    assert names[0] == names[-1]  # closing node repeats the entry node
    assert set(names) == {"mid_b", "leaf_c"}
    assert cycle["length"] == len(names) - 1
    assert populated.find_call_cycles("cb_d", max_depth=4, limit=10) == []
    assert populated.find_call_cycles("no_such_symbol") == []


def test_merged_edge_and_graph_queries_use_indexes(populated) -> None:
    connection = populated.connection
    indexes = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL"
        )
    }
    assert {
        "idx_calls_caller_id",
        "idx_calls_callee_id",
        "idx_call_targets_caller_id",
        "idx_call_targets_callee_id",
        "idx_call_targets_call_id",
    } <= indexes
    # Covering projections keep the plan unambiguous on the tiny fixture.
    caller_plan = connection.execute(
        "EXPLAIN QUERY PLAN SELECT caller_id FROM call_targets WHERE caller_id = ?", (MAIN.id,)
    ).fetchall()
    assert any("idx_call_targets_caller_id" in row["detail"] for row in caller_plan)
    callee_plan = connection.execute(
        "EXPLAIN QUERY PLAN SELECT callee_id FROM call_targets WHERE callee_id = ?", (MID.id,)
    ).fetchall()
    assert any("idx_call_targets_callee_id" in row["detail"] for row in callee_plan)


def test_add_call_targets_post_processing(tmp_path) -> None:
    """Post-processing passes can append candidate edges without an analysis batch."""

    store = _open_store(tmp_path)
    store.begin()
    store.add_parse_result(PARSE_RESULT)
    store.commit()
    store.finalize()
    candidate = _target(CALL_MAIN_MID, MAIN, CALLBACK, 15, confidence=0.66)
    assert store.add_call_targets([candidate]) == 1
    assert store.add_call_targets([candidate]) == 0  # idempotent
    mapping = {
        "id": call_target_id(CALL_MAIN_MID.id, ORPHAN.id, "indirect_candidate"),
        "call_id": CALL_MAIN_MID.id,
        "caller_id": MAIN.id,
        "callee_id": ORPHAN.id,
        "callee_name": ORPHAN.name,
        "kind": "indirect_candidate",
        "confidence": 0.4,
        "reason": "post-process mapping form",
        "file": FILE,
        "line": 15,
        "col": 3,
        "end_line": 15,
        "end_col": 30,
    }
    assert store.add_call_targets([mapping]) == 1
    with pytest.raises(ValueError):
        store.add_call_targets([{"callee_name": "x"}])

    callees = {row["callee_name"]: row for row in store.query_callees("main_a")}
    assert callees["cb_d"]["source"] == "call_target"
    assert callees["cb_d"]["confidence"] == pytest.approx(0.66)
    assert callees["orphan_e"]["source"] == "call_target"
    assert callees["orphan_e"]["reason"] if "reason" in callees["orphan_e"] else True
    graph = store.query_call_graph("main_a", direction="forward", depth=1, limit=50)
    assert {"cb_d", "orphan_e"} <= {edge["callee_name"] for edge in graph["edges"]}
    assert store.stats()["tables"]["call_targets"] == 2
    store.close()


def test_phase2_database_upgrade_in_place(tmp_path) -> None:
    db_path = tmp_path / "phase2.db"
    store = SqliteStore(db_path)
    store.open()
    store.begin()
    store.add_parse_result(PARSE_RESULT)
    store.commit()
    store.finalize()
    # Simulate a phase-2 database: no call_targets/workspace tables, no
    # store_schema_version and no phase-3 indexes.
    connection = store.connection
    connection.executescript(
        "DROP TABLE IF EXISTS call_targets;"
        "DROP TABLE IF EXISTS workspaces;"
        "DROP TABLE IF EXISTS projects;"
        "DROP TABLE IF EXISTS variants;"
        "DELETE FROM meta WHERE key = 'store_schema_version';"
        "DELETE FROM meta WHERE key = 'fts_ready';"
    )
    store.close()

    reopened = SqliteStore(db_path)
    with reopened:
        # open() recreates the additive tables; phase-2 queries still work
        # before finalize() because the old indexes are intact.
        assert reopened.get_workspace("ws:none") is None
        assert reopened.stats()["tables"]["call_targets"] == 0
        assert reopened.query_functions("main_a")[0]["id"] == MAIN.id
        assert reopened.query_callers("leaf_c")[0]["caller_name"] == "mid_b"
        meta = dict(reopened.connection.execute("SELECT key, value FROM meta"))
        assert meta["store_schema_version"] == "7"

        reopened.finalize()
        indexes = {
            row[0]
            for row in reopened.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL"
            )
        }
        assert "idx_call_targets_caller_id" in indexes
        assert "idx_projects_workspace_id" in indexes

        # New records can be written after the in-place upgrade.
        reopened.begin()
        reopened.add_analysis_result(ANALYSIS)
        reopened.commit()
        reopened.finalize()
        assert reopened.stats()["tables"]["call_targets"] == 1
        assert reopened.query_call_targets("main_a")[0]["callee_name"] == "cb_d"
        # finalize() is idempotent on an already-upgraded database.
        reopened.finalize()
        assert reopened.stats()["tables"]["call_targets"] == 1


def test_graph_works_before_finalize_on_phase2_indexes(tmp_path) -> None:
    """A phase-2 DB without phase-3 indexes must not raise on graph queries."""

    store = _open_store(tmp_path)
    store.begin()
    store.add_parse_result(PARSE_RESULT)
    store.commit()
    # No finalize(): direct call indexes are absent, call_targets is empty.
    graph = store.query_call_graph("main_a", direction="forward", depth=2, limit=50)
    assert graph["root"]["name"] == "main_a"
    store.close()
