"""Incremental per-TU maintenance: ``remove_units`` / ``replace_units`` + FTS.

Builds a 3-TU database (functions, types, fields, calls, CFG, data-flow facts,
call targets, callback registrations + links, deep status, tu_includes), then
verifies cascade deletes, atomic replacement, FTS5 consistency, idempotency,
rollback, old-database compatibility and transaction joining.
"""

from __future__ import annotations

import sqlite3
import types

import pytest

from ca.core.ids import (
    assignment_id,
    branch_id,
    call_id,
    call_target_id,
    cfg_block_id,
    cfg_edge_id,
    field_id,
    finding_id,
    function_id,
    resource_op_id,
    return_id,
    tu_id,
    type_id,
)
from ca.core.schema import (
    AnalysisResult,
    AssignmentRecord,
    BranchRecord,
    CallRecord,
    CallTargetRecord,
    CfgBlockRecord,
    CfgEdgeRecord,
    CompilationUnit,
    FieldRecord,
    FindingRecord,
    FunctionRecord,
    ParseResult,
    ResourceOpRecord,
    ReturnRecord,
    Sloc,
    TypeRecord,
)
from ca.store import SqliteStore
from ca.store.incremental import UNIT_TABLES

REPO = "/repo"
TU_COUNT = 3


def _sloc(file: str, line: int) -> Sloc:
    return Sloc(file, line, 3, line, 24)


def _make_units() -> list[dict]:
    units: list[dict] = []
    for index in range(TU_COUNT):
        file = f"kernel/tu_{index}.c"
        unit = CompilationUnit(
            id=tu_id(file, REPO, ["-c", file]),
            file=file,
            directory=REPO,
            repo_root=REPO,
            arguments=("-c", file),
        )
        def make_function(name: str, line: int) -> FunctionRecord:
            usr = f"c:@F@{name}"
            return FunctionRecord(
                id=function_id(name=name, usr=usr, sloc=_sloc(file, line), linkage="external"),
                tu_id=unit.id,
                name=name,
                qualified_name=name,
                usr=usr,
                kind="function",
                sloc=_sloc(file, line),
                is_definition=True,
                linkage="external",
            )

        alpha = make_function(f"tu{index}_alpha", 10)
        beta = make_function(f"tu{index}_beta", 20)
        type_record = TypeRecord(
            id=type_id(name=f"tu{index}_type", usr=f"c:@S@tu{index}_type", sloc=_sloc(file, 5), kind="struct"),
            tu_id=unit.id,
            name=f"tu{index}_type",
            qualified_name=f"struct tu{index}_type",
            usr=f"c:@S@tu{index}_type",
            kind="struct",
            sloc=_sloc(file, 5),
            is_definition=True,
        )
        field_record = FieldRecord(
            id=field_id(type_id_value=type_record.id, name="field", sloc=_sloc(file, 6), index=0),
            tu_id=unit.id,
            type_id=type_record.id,
            name="field",
            type_spelling="int",
            sloc=_sloc(file, 6),
        )
        units.append(
            {
                "index": index,
                "file": file,
                "tu": unit,
                "alpha": alpha,
                "beta": beta,
                "type": type_record,
                "field": field_record,
            }
        )
    # Calls: alpha_i -> alpha_(i+1), owned by TU i.
    for index, unit in enumerate(units):
        target = units[(index + 1) % TU_COUNT]["alpha"]
        call = CallRecord(
            id=call_id(
                caller_id=unit["alpha"].id,
                callee_usr=target.usr,
                callee_name=target.name,
                sloc=_sloc(unit["file"], 15),
            ),
            tu_id=unit["tu"].id,
            caller_id=unit["alpha"].id,
            sloc=_sloc(unit["file"], 15),
            callee_id=target.id,
            callee_usr=target.usr,
            callee_name=target.name,
            callee_qualified_name=target.name,
            kind="direct",
            is_resolved=True,
        )
        unit["call"] = call
        unit["target"] = target
    # Analyses + callbacks + includes for every TU.
    for index, unit in enumerate(units):
        file = unit["file"]
        alpha = unit["alpha"]
        entry = CfgBlockRecord(
            id=cfg_block_id(alpha.id, 0), function_id=alpha.id, index=0, kind="entry",
            terminator="unconditional", sloc=_sloc(file, 10),
        )
        body = CfgBlockRecord(
            id=cfg_block_id(alpha.id, 1), function_id=alpha.id, index=1, kind="normal",
            terminator="return", sloc=_sloc(file, 15),
        )
        call = unit["call"]
        call_target = CallTargetRecord(
            id=call_target_id(call.id, unit["target"].id, "indirect_candidate"),
            call_id=call.id,
            caller_id=alpha.id,
            callee_id=unit["target"].id,
            callee_name=unit["target"].name,
            kind="indirect_candidate",
            reason=f"registration g_ops_{index}.read",
            sloc=_sloc(file, 15),
        )
        finding = FindingRecord(
            id=finding_id(alpha.id, "leak", "res:test"),
            function_id=alpha.id,
            kind="leak",
            resource_kind="memory",
            message=f"leak in tu{index}",
        )
        analysis = AnalysisResult(
            function_id=alpha.id,
            cfg_blocks=[entry, body],
            cfg_edges=[
                CfgEdgeRecord(
                    id=cfg_edge_id(alpha.id, entry.id, body.id, "unconditional"),
                    function_id=alpha.id,
                    src=entry.id,
                    dst=body.id,
                )
            ],
            branches=[
                BranchRecord(
                    id=branch_id(alpha.id, _sloc(file, 14), 0),
                    function_id=alpha.id,
                    block_id=body.id,
                    sloc=_sloc(file, 14),
                    kind="if",
                    condition="x > 0",
                )
            ],
            assignments=[
                AssignmentRecord(
                    id=assignment_id(alpha.id, _sloc(file, 15), 0),
                    function_id=alpha.id,
                    block_id=body.id,
                    sloc=_sloc(file, 15),
                    lhs="x",
                    rhs="1",
                )
            ],
            returns=[
                ReturnRecord(
                    id=return_id(alpha.id, _sloc(file, 16), 0),
                    function_id=alpha.id,
                    block_id=body.id,
                    sloc=_sloc(file, 16),
                    expr="x",
                )
            ],
            resource_ops=[
                ResourceOpRecord(
                    id=resource_op_id(alpha.id, _sloc(file, 15), "acquire", 0),
                    function_id=alpha.id,
                    block_id=body.id,
                    sloc=_sloc(file, 15),
                    op="acquire",
                    resource_kind="memory",
                    api_name="acquire",
                )
            ],
            findings=[finding],
            call_targets=[call_target],
        )
        callbacks = [
            {
                "id": f"cbreg:tu{index}",
                "object_path": f"g_ops_{index}.read",
                "target_function_id": unit["beta"].id,
                "target_function_name": unit["beta"].name,
                "file": file,
                "line": 20,
                "tu_id": unit["tu"].id,
                "registration_kind": "struct_field",
                "evidence": [f"g_ops_{index}.read = {unit['beta'].name}"],
                "module": "kernel",
            }
        ]
        unit.update(
            {
                "parse": ParseResult(
                    tu=unit["tu"],
                    functions=[alpha, unit["beta"]],
                    types=[unit["type"]],
                    fields=[unit["field"]],
                    calls=[call],
                ),
                "analysis": analysis,
                "callbacks": callbacks,
                "include_paths": [f"kernel/header_{index}.h"],
                "call_target": call_target,
            }
        )
    return units


UNITS = _make_units()


def _write_unit(store: SqliteStore, unit: dict) -> None:
    store.begin()
    store.add_parse_result(unit["parse"])
    store.add_analysis_result(unit["analysis"])
    store.add_callback_registrations(unit["callbacks"])
    store.link_callback_targets(
        [(unit["callbacks"][0]["id"], unit["call_target"].id)]
    )
    store.add_tu_includes(unit["tu"].id, unit["include_paths"])
    store.commit()


def _build_store(tmp_path, *, finalize: bool = True) -> SqliteStore:
    store = SqliteStore(tmp_path / "incremental.db")
    store.open()
    for unit in UNITS:
        _write_unit(store, unit)
    store.mark_deep_ready([unit["analysis"].function_id for unit in UNITS])
    if finalize:
        store.finalize()
    return store


@pytest.fixture()
def populated(tmp_path):
    store = _build_store(tmp_path)
    yield store
    store.close()


def _updated_unit(index: int) -> dict:
    """Version-2 TU: renamed functions/facts and refreshed callback/include."""

    unit = UNITS[index]
    file = unit["file"]
    name = f"tu{index}_alpha_v2"
    sloc = _sloc(file, 110)
    alpha = FunctionRecord(
        id=function_id(name=name, usr=f"c:@F@{name}", sloc=sloc, linkage="external"),
        tu_id=unit["tu"].id,
        name=name,
        qualified_name=name,
        usr=f"c:@F@{name}",
        kind="function",
        sloc=sloc,
        is_definition=True,
        linkage="external",
    )
    beta = FunctionRecord(
        id=function_id(name=f"tu{index}_beta_v2", usr=f"c:@F@tu{index}_beta_v2", sloc=_sloc(file, 120), linkage="external"),
        tu_id=unit["tu"].id,
        name=f"tu{index}_beta_v2",
        qualified_name=f"tu{index}_beta_v2",
        usr=f"c:@F@tu{index}_beta_v2",
        kind="function",
        sloc=_sloc(file, 120),
        is_definition=True,
        linkage="external",
    )
    call = CallRecord(
        id=call_id(caller_id=alpha.id, callee_usr=beta.usr, callee_name=beta.name, sloc=_sloc(file, 115)),
        tu_id=unit["tu"].id,
        caller_id=alpha.id,
        sloc=_sloc(file, 115),
        callee_id=beta.id,
        callee_usr=beta.usr,
        callee_name=beta.name,
        callee_qualified_name=beta.name,
        is_resolved=True,
    )
    target = CallTargetRecord(
        id=call_target_id(call.id, beta.id, "indirect_candidate"),
        call_id=call.id,
        caller_id=alpha.id,
        callee_id=beta.id,
        callee_name=beta.name,
        kind="indirect_candidate",
        reason="refreshed registration",
        sloc=_sloc(file, 115),
    )
    analysis = AnalysisResult(
        function_id=alpha.id,
        branches=[
            BranchRecord(
                id=branch_id(alpha.id, _sloc(file, 114), 0),
                function_id=alpha.id,
                block_id="bb:v2",
                sloc=_sloc(file, 114),
                kind="if",
                condition="y > 1",
            )
        ],
        call_targets=[target],
    )
    callbacks = [
        {
            "id": f"cbreg:tu{index}:v2",
            "object_path": f"g_ops_{index}_v2.read",
            "target_function_id": beta.id,
            "target_function_name": beta.name,
            "file": file,
            "line": 120,
            "tu_id": unit["tu"].id,
            "registration_kind": "struct_field",
            "evidence": [f"g_ops_{index}_v2.read = {beta.name}"],
            "module": "kernel",
        }
    ]
    parse = ParseResult(
        tu=unit["tu"],
        functions=[alpha, beta],
        calls=[call],
    )
    return {
        "index": index,
        "tu": unit["tu"],
        "file": file,
        "alpha": alpha,
        "beta": beta,
        "call": call,
        "analysis": analysis,
        "callbacks": callbacks,
        "include_paths": [f"kernel/header_{index}_v2.h"],
        "parse": parse,
        "call_target": target,
    }


def _row_ids(store: SqliteStore, pattern: str) -> set[str]:
    return {row["id"] for row in store.query_functions(pattern)}


def _exists(store: SqliteStore, function_id: str) -> bool:
    return (
        store.connection.execute(
            "SELECT 1 FROM functions WHERE id = ?", (function_id,)
        ).fetchone()
        is not None
    )


def _index_ok(store: SqliteStore) -> None:
    store.connection.execute("INSERT INTO functions_fts(functions_fts) VALUES('integrity-check')")


def test_remove_one_tu_cascades(populated) -> None:
    store = populated
    removed_unit = UNITS[1]
    counts = store.remove_units([removed_unit["tu"].id])
    assert set(UNIT_TABLES) <= set(counts)
    assert counts["tus"] == 1
    assert counts["functions"] == 2
    assert counts["types"] == 1
    assert counts["fields"] == 1
    assert counts["calls"] == 1  # the call owned by TU1
    assert counts["cfg_blocks"] == 2 and counts["cfg_edges"] == 1
    assert counts["branches"] == 1
    assert counts["assignments"] == 1
    assert counts["returns"] == 1
    assert counts["resource_ops"] == 1
    assert counts["findings"] == 1
    # Own candidate (callee TU2) + the candidate targeting TU1's alpha.
    assert counts["call_targets"] == 2
    assert counts["callback_registrations"] == 1
    # TU1's own link plus TU0's candidate link that targets TU1's alpha.
    assert counts["callback_target_links"] == 2
    assert counts["deep_status"] == 1
    assert counts["tu_includes"] == 1
    assert counts["functions_fts"] == 2

    # Remaining TUs are intact.
    tables = store.stats()["tables"]
    assert tables["tus"] == 2
    assert tables["functions"] == 4
    assert tables["calls"] == 2
    assert tables["callback_registrations"] == 2
    assert tables["deep_status"] == 2
    # FTS and query APIs reflect the deletion immediately (no finalize()).
    assert store.query_functions("tu1_alpha") == []
    assert store.query_functions("tu1_beta") == []
    assert [row["name"] for row in store.query_functions("tu0_alpha")] == ["tu0_alpha"]
    assert [row["name"] for row in store.query_functions("tu2_alpha")] == ["tu2_alpha"]
    assert store.query_branches("tu1_alpha") == []
    assert store.query_callbacks(path_prefix="g_ops_1.") == []
    assert store.query_callback_targets("cbreg:tu1") == []
    assert store.get_deep_status(["tu1_alpha"]) == {}
    assert store.tu_includes(removed_unit["tu"].id) == []
    _index_ok(store)

    # Idempotent: re-deleting the same TU (or an unknown one) is a no-op.
    again = store.remove_units([removed_unit["tu"].id])
    assert all(value == 0 for value in again.values())
    assert all(value == 0 for value in store.remove_units(["tu:missing"]).values())
    assert all(value == 0 for value in store.remove_units([]).values())


def test_remove_units_multi_and_graph(populated) -> None:
    store = populated
    counts = store.remove_units([UNITS[0]["tu"].id, UNITS[2]["tu"].id])
    assert counts["tus"] == 2 and counts["functions"] == 4
    assert store.stats()["tables"]["tus"] == 1
    # The surviving call from TU0 points at a deleted callee: the graph must
    # still answer without raising (edge kept, target falls back to name).
    graph = store.query_call_graph("tu0_alpha", direction="forward", depth=1, limit=10)
    assert graph["root"] is None  # tu0_alpha itself was removed
    # A surviving function's outgoing edge list is still queryable.
    assert isinstance(store.query_callees("tu1_alpha"), list)
    _index_ok(store)


def test_replace_unit_updates_fts_and_facts(populated) -> None:
    store = populated
    updated = _updated_unit(1)
    result = store.replace_unit(
        updated["parse"],
        [updated["analysis"]],
        callbacks=updated["callbacks"],
        include_paths=updated["include_paths"],
    )
    assert result["tu_ids"] == [updated["tu"].id]
    assert result["removed"]["functions"] == 2
    assert result["added"]["functions"] == 2
    assert result["added"]["branches"] == 1
    assert result["added"]["call_targets"] == 1
    assert result["added"]["callback_registrations"] == 1
    assert result["fts_deleted"] == 2
    assert result["fts_inserted"] == 2
    assert result["deep_ready"] == 1

    # Old ids gone, new symbols searchable through the FTS path immediately.
    old_alpha, old_beta = UNITS[1]["alpha"], UNITS[1]["beta"]
    assert not _exists(store, old_alpha.id)
    assert not _exists(store, old_beta.id)
    assert updated["alpha"].id in _row_ids(store, "tu1_alpha_v2")
    assert updated["beta"].id in _row_ids(store, "tu1_beta_v2")
    assert store.query_functions("tu1_")[0]["id"] in {
        updated["alpha"].id,
        updated["beta"].id,
    }
    stats = store.stats()["tables"]
    assert stats["functions"] == 6
    assert stats["calls"] == 3
    assert stats["branches"] == 3
    detail = store.query_function_detail("tu1_alpha_v2")
    assert detail["function"]["id"] == updated["alpha"].id
    assert [branch["condition"] for branch in detail["branches"]] == ["y > 1"]
    status = store.get_deep_status(["tu1_alpha_v2"])[updated["alpha"].id]
    assert status["deep_ready"] is True
    assert [row["object_path"] for row in store.query_callbacks(path_prefix="g_ops_1_v2.")] == [
        "g_ops_1_v2.read"
    ]
    assert store.query_callbacks(path_prefix="g_ops_1.") == []
    assert store.tu_includes(updated["tu"].id) == ["kernel/header_1_v2.h"]
    assert len(store.query_call_targets("tu1_alpha_v2", direction="callee")) == 1
    _index_ok(store)

    # Re-running the same replacement is idempotent: same final state.
    before = store.stats()["tables"]
    again = store.replace_unit(
        updated["parse"],
        [updated["analysis"]],
        callbacks=updated["callbacks"],
        include_paths=updated["include_paths"],
    )
    assert again["added"]["functions"] == 2
    assert store.stats()["tables"] == before
    assert updated["alpha"].id in _row_ids(store, "tu1_alpha_v2")
    _index_ok(store)


def test_replace_units_batch_all_three(populated) -> None:
    store = populated
    updated = [_updated_unit(index) for index in range(TU_COUNT)]
    result = store.replace_units(
        [
            {
                "parse": unit["parse"],
                "analyses": [unit["analysis"]],
                "callbacks": unit["callbacks"],
                "include_paths": unit["include_paths"],
            }
            for unit in updated
        ]
    )
    assert result["tu_ids"] == [unit["tu"].id for unit in updated]
    assert result["removed"]["functions"] == 6
    assert result["added"]["functions"] == 6
    assert result["fts_deleted"] == 6 and result["fts_inserted"] == 6
    assert result["deep_ready"] == 3
    for index in range(TU_COUNT):
        assert updated[index]["alpha"].id in _row_ids(store, f"tu{index}_alpha_v2")
        assert not _exists(store, UNITS[index]["alpha"].id)
    assert store.stats()["tables"]["functions"] == 6
    _index_ok(store)


def test_replace_rolls_back_on_failure(populated) -> None:
    store = populated
    before_tables = dict(store.stats()["tables"])
    before_row = store.query_functions("tu1_alpha")
    invalid = types.SimpleNamespace(
        tu=UNITS[1]["tu"],
        functions=[object()],  # triggers AttributeError inside the writer
        types=[],
        fields=[],
        calls=[],
    )
    with pytest.raises(AttributeError):
        store.replace_unit(invalid, [])
    assert store.stats()["tables"] == before_tables
    assert store.query_functions("tu1_alpha") == before_row
    assert store.query_branches("tu1_alpha")
    assert store.query_callbacks(path_prefix="g_ops_1.")
    _index_ok(store)


def test_remove_rolls_back_on_failure_on_absent_tables(tmp_path) -> None:
    """Incremental helpers also work against old databases missing new tables."""

    store = _build_store(tmp_path)
    try:
        connection = store.connection
        # Simulate a pre-v3 database: no call_targets/deep_status/callback tables.
        connection.executescript(
            "DROP TABLE IF EXISTS callback_target_links;"
            "DROP TABLE IF EXISTS callback_registrations;"
            "DROP TABLE IF EXISTS deep_status;"
            "DROP TABLE IF EXISTS call_targets;"
        )
        counts = store.remove_units([UNITS[1]["tu"].id])
        assert counts["tus"] == 1 and counts["functions"] == 2
        assert counts["call_targets"] == 0
        assert counts["callback_registrations"] == 0
        assert counts["deep_status"] == 0
        assert not _exists(store, UNITS[1]["alpha"].id)
        # Replacement still works; FTS is maintained for surviving tables.
        updated = _updated_unit(1)
        result = store.replace_unit(updated["parse"], [updated["analysis"]])
        assert result["added"]["functions"] == 2
        assert updated["alpha"].id in _row_ids(store, "tu1_alpha_v2")
        _index_ok(store)
    finally:
        store.close()


def test_fts_absent_falls_back(tmp_path) -> None:
    store = _build_store(tmp_path, finalize=False)
    try:
        assert "functions_fts" not in {
            row[0]
            for row in store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        updated = _updated_unit(0)
        result = store.replace_unit(updated["parse"], [updated["analysis"]])
        assert result["fts_deleted"] == 0 and result["fts_inserted"] == 0
        # LIKE fallback keeps queries correct without an FTS index.
        assert updated["alpha"].id in _row_ids(store, "tu0_alpha_v2")
        assert not _exists(store, UNITS[0]["alpha"].id)
        # finalize() later builds the index from the current content (no stale).
        store.finalize()
        assert updated["alpha"].id in _row_ids(store, "tu0_alpha_v2")
        # finalize() may only recreate the old id as a placeholder for calls
        # from untouched TUs; it must never be a definition row again.
        placeholder = store.connection.execute(
            "SELECT is_definition FROM functions WHERE id = ?", (UNITS[0]["alpha"].id,)
        ).fetchone()
        assert placeholder is None or int(placeholder[0]) == 0
        _index_ok(store)
    finally:
        store.close()


def test_replace_joins_outer_transaction(tmp_path) -> None:
    store = _build_store(tmp_path)
    try:
        updated = _updated_unit(2)
        store.begin()
        result = store.replace_unit(updated["parse"], [updated["analysis"]])
        assert result["added"]["functions"] == 2
        assert store.query_functions("tu2_alpha_v2")  # visible inside the txn
        store.commit()
        assert updated["alpha"].id in _row_ids(store, "tu2_alpha_v2")
        assert not _exists(store, UNITS[2]["alpha"].id)
    finally:
        store.close()


def test_schema_unchanged_and_empty_units(tmp_path) -> None:
    store = SqliteStore(tmp_path / "empty.db")
    store.open()
    try:
        meta = dict(store.connection.execute("SELECT key, value FROM meta"))
        assert meta["store_schema_version"] == "8"
        zeros = store.remove_units([])
        assert all(value == 0 for value in zeros.values())
        empty = store.replace_units([])
        assert empty["tu_ids"] == []
        assert empty["added"] == {}
        assert empty["fts_deleted"] == 0
        with pytest.raises(ValueError):
            store.replace_units([(UNITS[0]["parse"], []), (UNITS[0]["parse"], [])])
    finally:
        store.close()
