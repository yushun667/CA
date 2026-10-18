"""deep_status: layered-index status persistence, implicit pending and migration.

The base index (L0) must stay queryable without any deep rows; an L0 database
reports every function as implicit ``pending`` and only an explicit deep pass (or
the phase-3 -> phase-4 backfill) turns functions ``ready``.
"""

from __future__ import annotations

import pytest

from ca.core.ids import call_id, function_id, tu_id
from ca.core.schema import (
    AnalysisResult,
    CallRecord,
    CfgBlockRecord,
    CompilationUnit,
    FunctionRecord,
    ParseResult,
    Sloc,
)
from ca.core.schema import SCHEMA_VERSION
from ca.store import SqliteStore

REPO = "/repo"
FILE = "kernel/deep.c"
TU1 = CompilationUnit(
    id=tu_id(FILE, REPO, ["-c", FILE]),
    file=FILE,
    directory=REPO,
    repo_root=REPO,
    arguments=("-c", FILE),
)
TU2_FILE = "kernel/deep2.c"
TU2 = CompilationUnit(
    id=tu_id(TU2_FILE, REPO, ["-c", TU2_FILE]),
    file=TU2_FILE,
    directory=REPO,
    repo_root=REPO,
    arguments=("-c", TU2_FILE),
)


def _sloc(line: int, file: str = FILE) -> Sloc:
    return Sloc(file, line, 3, line, 20)


def _function(name: str, line: int, tu: CompilationUnit = TU1) -> FunctionRecord:
    usr = f"c:@F@{name}"
    return FunctionRecord(
        id=function_id(name=name, usr=usr, sloc=_sloc(line, tu.file), linkage="external"),
        tu_id=tu.id,
        name=name,
        qualified_name=name,
        usr=usr,
        kind="function",
        sloc=_sloc(line, tu.file),
        is_definition=True,
        linkage="external",
    )


MAIN = _function("main_a", 10)
MID = _function("mid_b", 20)
LEAF = _function("leaf_c", 30, TU2)

CALL = CallRecord(
    id=call_id(
        caller_id=MAIN.id,
        callee_usr=MID.usr,
        callee_name=MID.name,
        sloc=_sloc(12),
    ),
    tu_id=TU1.id,
    caller_id=MAIN.id,
    sloc=_sloc(12),
    callee_id=MID.id,
    callee_usr=MID.usr,
    callee_name=MID.name,
    callee_qualified_name=MID.name,
    is_resolved=True,
)

PARSE_RESULT = ParseResult(tu=TU1, functions=[MAIN, MID, LEAF], calls=[CALL])

CFG_BLOCK = CfgBlockRecord(
    id="bb:deep",
    function_id=MID.id,
    index=0,
    kind="entry",
    terminator="unconditional",
    sloc=_sloc(20),
)
ANALYSIS = AnalysisResult(function_id=MID.id, cfg_blocks=[CFG_BLOCK])


def _open(tmp_path, *, analyzed: bool = False) -> SqliteStore:
    store = SqliteStore(tmp_path / ("deep.db" if not analyzed else "deep-analyzed.db"))
    store.open()
    store.begin()
    store.add_parse_result(PARSE_RESULT)
    if analyzed:
        store.add_analysis_result(ANALYSIS)
    store.commit()
    store.finalize()
    return store


@pytest.fixture()
def l0(tmp_path):
    store = _open(tmp_path)
    yield store
    store.close()


@pytest.fixture()
def analyzed(tmp_path):
    store = _open(tmp_path, analyzed=True)
    yield store
    store.close()


def test_deep_status_table_and_version(l0) -> None:
    meta = dict(l0.connection.execute("SELECT key, value FROM meta"))
    assert meta["store_schema_version"] == "8"
    assert meta["schema_version"] == SCHEMA_VERSION
    assert l0.stats()["tables"]["deep_status"] == 0
    indexes = {
        row[0]
        for row in l0.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL"
        )
    }
    assert {"idx_deep_status_status", "idx_deep_status_tu_id"} <= indexes


def test_implicit_pending_and_stats(l0) -> None:
    counts = l0.count_deep_status()
    assert counts == {"pending": 3, "ready": 0, "failed": 0, "total": 3}
    stats = l0.deep_stats()
    assert stats == {
        "total_functions": 3,
        "deep_ready": 0,
        "pending": 3,
        "failed": 0,
        "coverage": 0.0,
    }
    statuses = l0.get_deep_status([MAIN.id])
    assert set(statuses) == {MAIN.id}
    record = statuses[MAIN.id]
    assert record["status"] == "pending"
    assert record["deep_ready"] is False
    assert record["name"] == "main_a"
    assert record["file"] == FILE
    assert record["line"] == 10
    assert record["tu_id"] == TU1.id
    assert record["facts"] == {}
    assert {item["function_id"] for item in l0.list_deep_pending()} == {
        MAIN.id,
        MID.id,
        LEAF.id,
    }
    assert l0.list_deep_ready() == []


def test_mark_ready_round_trip(l0) -> None:
    items = [
        {
            "function_id": MAIN.id,
            "tu_id": TU1.id,
            "tool_version": "0.3.0",
            "source_hash": "abc123",
            "facts": {"branches": 4, "assignments": 7},
        },
        {"function_id": MID.id},
    ]
    assert l0.mark_deep_ready(items) == 2
    statuses = l0.get_deep_status([MAIN.id, MID.id])
    ready = statuses[MAIN.id]
    assert ready["status"] == "ready"
    assert ready["deep_ready"] is True
    assert ready["tool_version"] == "0.3.0"
    assert ready["source_hash"] == "abc123"
    assert ready["facts"] == {"branches": 4, "assignments": 7}
    assert ready["analyzed_at"]  # default timestamp filled in
    assert statuses[MID.id]["status"] == "ready"
    assert statuses[MID.id]["facts"] == {}

    assert l0.count_deep_status()["ready"] == 2
    ready_rows = l0.list_deep_ready()
    assert [row["function_id"] for row in ready_rows] == [MAIN.id, MID.id]
    pending = l0.list_deep_pending()
    assert [row["function_id"] for row in pending] == [LEAF.id]
    stats = l0.deep_stats()
    assert stats["coverage"] == pytest.approx(2 / 3, abs=1e-4)


def test_mark_ready_accepts_single_id_and_names(l0) -> None:
    assert l0.mark_deep_ready(MAIN.id) == 1
    assert l0.get_deep_status(MAIN.id)[MAIN.id]["deep_ready"] is True
    # Names resolve through the symbol search; ids and names can be mixed.
    statuses = l0.get_deep_status([MAIN.id, "mid_b", "no_such_symbol"])
    assert set(statuses) == {MAIN.id, MID.id}
    assert statuses[MID.id]["status"] == "pending"
    assert statuses[MID.id]["name"] == "mid_b"


def test_mark_failed_and_recovery(l0) -> None:
    assert l0.mark_deep_failed([{"function_id": MAIN.id, "tu_id": TU1.id}], error="timeout") == 1
    record = l0.get_deep_status([MAIN.id])[MAIN.id]
    assert record["status"] == "failed"
    assert record["deep_ready"] is False
    assert record["error"] == "timeout"
    assert l0.count_deep_status()["failed"] == 1
    pending_ids = {row["function_id"] for row in l0.list_deep_pending()}
    assert MAIN.id in pending_ids  # failed functions are still pending work

    # A later successful pass clears the error.
    assert l0.mark_deep_ready([MAIN.id]) == 1
    record = l0.get_deep_status([MAIN.id])[MAIN.id]
    assert record["status"] == "ready"
    assert record["error"] == ""
    assert l0.count_deep_status()["failed"] == 0


def test_mark_pending_resets(l0) -> None:
    l0.mark_deep_ready([{"function_id": MAIN.id, "facts": {"branches": 1}}])
    assert l0.mark_deep_pending([MAIN.id]) == 1
    record = l0.get_deep_status([MAIN.id])[MAIN.id]
    assert record["status"] == "pending"
    assert record["deep_ready"] is False
    assert record["analyzed_at"] == ""
    assert record["facts"] == {}
    assert l0.mark_deep_ready([]) == 0
    assert l0.mark_deep_failed([]) == 0
    assert l0.mark_deep_pending([]) == 0


def test_invalidate_deep(l0) -> None:
    l0.mark_deep_ready([MAIN.id, MID.id, LEAF.id])
    assert l0.invalidate_deep(function_ids=[MAIN.id]) == 1
    assert l0.get_deep_status([MAIN.id])[MAIN.id]["status"] == "pending"
    # Byte-level: invalidation by TU covers all functions in that TU.
    assert l0.invalidate_deep(tu_ids=[TU2.id]) == 1
    assert l0.count_deep_status()["ready"] == 1
    with pytest.raises(ValueError):
        l0.invalidate_deep()
    assert l0.invalidate_deep(function_ids=["fn:missing"]) == 0


def test_tu_filtered_lists(l0) -> None:
    l0.mark_deep_ready([LEAF.id])
    pending_tu1 = l0.list_deep_pending(tu_ids=[TU1.id])
    assert {row["function_id"] for row in pending_tu1} == {MAIN.id, MID.id}
    ready_tu2 = l0.list_deep_ready(tu_ids=[TU2.id])
    assert [row["function_id"] for row in ready_tu2] == [LEAF.id]
    assert len(l0.list_deep_pending(limit=1)) == 1
    assert l0.list_deep_pending(limit=0) == []


def test_deep_ready_in_detail_and_graph(l0) -> None:
    detail = l0.query_function_detail("main_a")
    assert detail["deep_ready"] is False
    missing = l0.query_function_detail("no_such_function")
    assert missing["deep_ready"] is False

    graph = l0.query_call_graph("main_a", direction="forward", depth=1, limit=10)
    assert graph["deep_ready"] is False
    assert all(node["deep_ready"] is False for node in graph["nodes"])

    l0.mark_deep_ready([MID.id])
    detail = l0.query_function_detail("mid_b")
    assert detail["deep_ready"] is True
    graph = l0.query_call_graph("main_a", direction="forward", depth=1, limit=10)
    by_name = {node["name"]: node for node in graph["nodes"]}
    assert by_name["mid_b"]["deep_ready"] is True
    assert by_name["main_a"]["deep_ready"] is False
    assert graph["deep_ready"] is False


def test_base_queries_do_not_require_deep_table(l0) -> None:
    l0.connection.execute("DROP TABLE deep_status")
    l0.connection.commit()
    # L0 queries must keep working without the deep table (deep_ready=False).
    assert [row["name"] for row in l0.query_functions("main_a")] == ["main_a"]
    assert l0.query_callers("mid_b")[0]["caller_name"] == "main_a"
    assert l0.query_function_detail("main_a")["deep_ready"] is False
    assert l0.query_call_graph("main_a", depth=1)["deep_ready"] is False


def test_phase3_database_backfills_ready(tmp_path) -> None:
    store = _open(tmp_path, analyzed=True)
    # The analyzed function is backfilled as ready during finalize().
    assert store.stats()["tables"]["deep_status"] == 1
    assert store.get_deep_status([MID.id])[MID.id]["status"] == "ready"
    assert store.get_deep_status([MID.id])[MID.id]["tool_version"] == "legacy"
    assert store.get_deep_status([MAIN.id])[MAIN.id]["status"] == "pending"

    # Simulate a phase-3 database: no table, no indexes, no store_schema_version.
    store.connection.executescript(
        "DROP TABLE deep_status; DELETE FROM meta WHERE key = 'store_schema_version';"
    )
    store.close()

    reopened = SqliteStore(store.db_path)
    with reopened:
        assert reopened.stats()["tables"]["deep_status"] == 0
        assert reopened.query_function_detail("mid_b")["deep_ready"] is False
        reopened.finalize()
        assert reopened.stats()["tables"]["deep_status"] == 1
        assert reopened.get_deep_status([MID.id])[MID.id]["deep_ready"] is True
        # finalize() is idempotent: no duplicate/overwritten rows.
        reopened.finalize()
        assert reopened.count_deep_status()["ready"] == 1

        # Explicit new status wins over the legacy backfill on later finalizes.
        reopened.mark_deep_failed([MID.id], error="reanalyze")
        reopened.finalize()
        assert reopened.get_deep_status([MID.id])[MID.id]["status"] == "failed"
