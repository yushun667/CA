"""Round-trip tests for ``ca.store``: write -> finalize -> query/export.

Covers the CLI contract (dict key sets), dedupe, definition-over-declaration
preference, unresolved-callee placeholders, limits, escaping/parameter binding,
stats and streaming export. Uses ``tmp_path`` only; no network.
"""

from __future__ import annotations

import dataclasses
import sqlite3

import pytest

from ca.core.ids import (
    assignment_id,
    branch_id,
    call_id,
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
from ca.core.interfaces import RecordStore
from ca.core.schema import (
    SCHEMA_VERSION,
    AnalysisResult,
    AssignmentRecord,
    BranchRecord,
    CallRecord,
    CfgBlockRecord,
    CfgEdgeRecord,
    CompilationUnit,
    FieldRecord,
    FindingRecord,
    FunctionRecord,
    ParamRecord,
    ParseResult,
    ResourceOpRecord,
    ReturnRecord,
    Sloc,
    TypeRecord,
)
from ca.store import SqliteStore

REPO = "/repo"
PROC_FILE = "kernel/proc.c"

FUNCTION_KEYS = {
    "id",
    "name",
    "qualified_name",
    "kind",
    "file",
    "line",
    "is_definition",
    "is_static",
    "linkage",
}
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
BRANCH_KEYS = {
    "id",
    "function_id",
    "block_id",
    "file",
    "line",
    "col",
    "end_line",
    "end_col",
    "kind",
    "condition",
    "var_names",
    "true_block",
    "false_block",
    "is_negated",
    "case_value",
}
ASSIGNMENT_KEYS = {
    "id",
    "function_id",
    "block_id",
    "file",
    "line",
    "col",
    "end_line",
    "end_col",
    "lhs",
    "rhs",
    "var_names",
    "is_decl_init",
    "operator",
    "type_spelling",
}
RETURN_KEYS = {
    "id",
    "function_id",
    "block_id",
    "file",
    "line",
    "col",
    "end_line",
    "end_col",
    "expr",
    "var_names",
    "is_implicit",
}
RESOURCE_OP_KEYS = {
    "id",
    "function_id",
    "block_id",
    "file",
    "line",
    "col",
    "end_line",
    "end_col",
    "op",
    "resource_kind",
    "api_name",
    "target",
    "target_vars",
    "call_id",
    "matcher",
}
FINDING_KEYS = {
    "id",
    "function_id",
    "function_name",
    "kind",
    "resource_kind",
    "message",
    "severity",
    "file",
    "line",
}


def _sloc(line: int, file: str = PROC_FILE, col: int = 5, end_line: int | None = None) -> Sloc:
    return Sloc(file, line, col, end_line if end_line is not None else line, col + 20)


def _tu() -> CompilationUnit:
    return CompilationUnit(
        id=tu_id(PROC_FILE, REPO, ["-c", PROC_FILE]),
        file=PROC_FILE,
        directory=REPO,
        repo_root=REPO,
        arguments=("-c", PROC_FILE),
    )


TU = _tu()


def _function(
    name: str,
    line: int,
    *,
    usr: str | None = None,
    file: str = PROC_FILE,
    is_definition: bool = True,
    params: tuple[ParamRecord, ...] = (),
) -> FunctionRecord:
    usr_value = f"c:@F@{name}" if usr is None else usr
    sloc = _sloc(line, file)
    return FunctionRecord(
        id=function_id(name=name, usr=usr_value, sloc=sloc, linkage="external"),
        tu_id=TU.id,
        name=name,
        qualified_name=name,
        usr=usr_value,
        kind="function",
        sloc=sloc,
        is_definition=is_definition,
        return_type="int",
        signature=f"int {name}(void)",
        display_name=name,
        linkage="external",
        params=params,
    )


SYS_SLEEP = _function(
    "sys_sleep",
    10,
    params=(ParamRecord("seconds", "unsigned int", 0, _sloc(10, col=22), "0"),),
)
WAKEUP = _function("wakeup", 40)
# A callee that only appears in call rows: finalize() must add a placeholder.
KMALLOC = _function("kmalloc", 1, file="kernel/mm.c")

PROC = TypeRecord(
    id=type_id(name="proc", usr="c:@S@proc", sloc=_sloc(3), kind="struct"),
    tu_id=TU.id,
    name="proc",
    qualified_name="struct proc",
    usr="c:@S@proc",
    kind="struct",
    sloc=_sloc(3),
    is_definition=True,
    size_bytes=32,
    align_bytes=8,
)
PROC_PID = FieldRecord(
    id=field_id(type_id_value=PROC.id, name="pid", sloc=_sloc(4), index=0),
    tu_id=TU.id,
    type_id=PROC.id,
    name="pid",
    type_spelling="int",
    sloc=_sloc(4),
    offset_bits=0,
)


def _call(caller: FunctionRecord, call_line: int, callee: FunctionRecord | None = None, **overrides):
    callee_name = overrides.pop("callee_name", callee.name if callee else "")
    callee_usr = overrides.pop("callee_usr", callee.usr if callee else "")
    callee_id_value = overrides.pop("callee_id", callee.id if callee else "")
    sloc = overrides.pop("sloc", _sloc(call_line, col=9))
    return CallRecord(
        id=call_id(caller_id=caller.id, callee_usr=callee_usr, callee_name=callee_name, sloc=sloc),
        tu_id=TU.id,
        caller_id=caller.id,
        sloc=sloc,
        callee_id=callee_id_value,
        callee_usr=callee_usr,
        callee_name=callee_name,
        callee_qualified_name=callee_name,
        **overrides,
    )


CALL_SLEEP_WAKEUP = _call(SYS_SLEEP, 15, WAKEUP, is_resolved=True, arg_count=1)
CALL_WAKEUP_KMALLOC = _call(
    WAKEUP,
    45,
    callee_id=KMALLOC.id,
    callee_name=KMALLOC.name,
    callee_usr=KMALLOC.usr,
    is_resolved=False,
    arg_count=2,
)
CALL_WAKEUP_PRINTK = _call(
    WAKEUP, 46, callee_name="printk", is_resolved=False, kind="unknown", arg_count=1
)
CALL_SLEEP_PRINTK = _call(
    SYS_SLEEP, 16, callee_name="printk", is_resolved=False, kind="unknown", arg_count=2
)

PARSE_RESULT = ParseResult(
    tu=TU,
    functions=[SYS_SLEEP, WAKEUP],
    types=[PROC],
    fields=[PROC_PID],
    calls=[CALL_SLEEP_WAKEUP, CALL_WAKEUP_KMALLOC, CALL_WAKEUP_PRINTK, CALL_SLEEP_PRINTK],
)

ENTRY = CfgBlockRecord(
    id=cfg_block_id(WAKEUP.id, 0),
    function_id=WAKEUP.id,
    index=0,
    kind="entry",
    terminator="unconditional",
    sloc=_sloc(40),
)
BODY = CfgBlockRecord(
    id=cfg_block_id(WAKEUP.id, 1),
    function_id=WAKEUP.id,
    index=1,
    kind="normal",
    terminator="return",
    sloc=_sloc(47),
    statement_count=2,
)
EDGE = CfgEdgeRecord(
    id=cfg_edge_id(WAKEUP.id, ENTRY.id, BODY.id, "unconditional"),
    function_id=WAKEUP.id,
    src=ENTRY.id,
    dst=BODY.id,
)
BRANCH = BranchRecord(
    id=branch_id(WAKEUP.id, _sloc(43), 0),
    function_id=WAKEUP.id,
    block_id=BODY.id,
    sloc=_sloc(43),
    kind="if",
    condition="flags & 1",
    var_names=("flags",),
    true_block=BODY.id,
    false_block=ENTRY.id,
)
ASSIGNMENT = AssignmentRecord(
    id=assignment_id(WAKEUP.id, _sloc(44), 0),
    function_id=WAKEUP.id,
    block_id=BODY.id,
    sloc=_sloc(44),
    lhs="x",
    rhs="kmalloc(16, 0)",
    var_names=("x",),
    is_decl_init=True,
    operator="=",
    type_spelling="void *",
)
RETURN = ReturnRecord(
    id=return_id(WAKEUP.id, _sloc(47), 0),
    function_id=WAKEUP.id,
    block_id=BODY.id,
    sloc=_sloc(47),
    expr="x",
    var_names=("x",),
)
RESOURCE = ResourceOpRecord(
    id=resource_op_id(WAKEUP.id, _sloc(44), "kmalloc", 0),
    function_id=WAKEUP.id,
    block_id=BODY.id,
    sloc=_sloc(44),
    op="acquire",
    resource_kind="memory",
    api_name="kmalloc",
    target="x",
    target_vars=("x",),
    call_id=CALL_WAKEUP_KMALLOC.id,
    matcher="kfree",
)
FINDING = FindingRecord(
    id=finding_id(WAKEUP.id, "leak", RESOURCE.id),
    function_id=WAKEUP.id,
    kind="leak",
    resource_kind="memory",
    message="x leaked",
    severity="warning",
    acquire_id=RESOURCE.id,
    path_blocks=(ENTRY.id, BODY.id),
    evidence=("void *x = kmalloc(16, 0);",),
)

ANALYSIS = AnalysisResult(
    function_id=WAKEUP.id,
    cfg_blocks=[ENTRY, BODY],
    cfg_edges=[EDGE],
    branches=[BRANCH],
    assignments=[ASSIGNMENT],
    returns=[RETURN],
    resource_ops=[RESOURCE],
    findings=[FINDING],
)

# tus 1, functions 3 (2 real + 1 placeholder), types 1, fields 1, calls 4,
# cfg_blocks 2, cfg_edges 1, branches 1, assignments 1, returns 1,
# resource_ops 1, findings 1  ->  18 exported rows.
EXPECTED_TABLE_COUNTS = {
    "meta": 3,
    "tus": 1,
    "functions": 3,
    "types": 1,
    "fields": 1,
    "calls": 4,
    "cfg_blocks": 2,
    "cfg_edges": 1,
    "branches": 1,
    "assignments": 1,
    "returns": 1,
    "resource_ops": 1,
    "findings": 1,
    "call_targets": 0,
    "tu_includes": 0,
    "deep_status": 1,
    "callback_registrations": 0,
    "callback_target_links": 0,
    "workspaces": 0,
    "projects": 0,
    "variants": 0,
    "jobs": 0,
    "job_events": 0,
}


@pytest.fixture()
def populated(tmp_path):
    store = SqliteStore(tmp_path / "ca.db")
    with store:
        store.begin()
        store.add_parse_result(PARSE_RESULT)
        store.add_analysis_result(ANALYSIS)
        store.commit()
        store.finalize()
        yield store


def test_implements_record_store_contract(populated) -> None:
    assert isinstance(populated, RecordStore)


def test_query_functions_exact_pattern_limit(populated) -> None:
    rows = populated.query_functions("wakeup")
    assert len(rows) == 1
    assert set(rows[0]) == FUNCTION_KEYS
    row = rows[0]
    assert row["id"] == WAKEUP.id
    assert row["name"] == "wakeup"
    assert row["qualified_name"] == "wakeup"
    assert row["kind"] == "function"
    assert row["file"] == PROC_FILE
    assert row["line"] == 40
    assert row["is_definition"] is True
    assert row["is_static"] is False
    assert row["linkage"] == "external"

    # Substring fallback finds definitions whose name contains the pattern.
    assert [row["name"] for row in populated.query_functions("wake")] == ["wakeup"]
    assert [row["name"] for row in populated.query_functions("sys_sleep")] == ["sys_sleep"]
    assert populated.query_functions("no-such-function") == []
    assert populated.query_functions("wakeup", limit=0) == []
    assert len(populated.query_functions("e", limit=1)) == 1
    # Values are bound, never interpolated: LIKE wildcards are escaped too.
    assert populated.query_functions("'; DROP TABLE functions; --") == []
    assert populated.query_functions("%") == []
    assert populated.stats()["tables"]["functions"] == 3


def test_query_callers_resolved_placeholder_and_unresolved(populated) -> None:
    rows = populated.query_callers("wakeup")
    assert len(rows) == 1
    assert set(rows[0]) == CALLER_KEYS
    row = rows[0]
    assert row["caller_id"] == SYS_SLEEP.id
    assert row["caller_name"] == "sys_sleep"
    assert row["caller_file"] == PROC_FILE
    assert row["caller_line"] == 10
    assert row["callee_name"] == "wakeup"
    assert row["call_file"] == PROC_FILE
    assert row["call_line"] == 15
    assert row["is_resolved"] is True
    assert row["kind"] == "direct"
    assert row["confidence"] == 1.0
    assert row["source"] == "call"
    assert row["candidate"] is False

    # The callee has no function row, so finalize() created a placeholder; the
    # call row itself keeps the redundant callee name.
    placeholder = populated.query_functions("kmalloc")
    assert len(placeholder) == 1
    assert placeholder[0]["id"] == KMALLOC.id
    assert placeholder[0]["is_definition"] is False
    kmalloc_callers = populated.query_callers("kmalloc")
    assert len(kmalloc_callers) == 1
    assert kmalloc_callers[0]["caller_name"] == "wakeup"
    assert kmalloc_callers[0]["call_line"] == 45
    assert kmalloc_callers[0]["callee_name"] == "kmalloc"
    assert kmalloc_callers[0]["is_resolved"] is False

    # Calls with no callee id at all are still attributed via callee_name.
    printk_callers = populated.query_callers("printk")
    assert [row["call_line"] for row in printk_callers] == [16, 46]
    assert [row["caller_name"] for row in printk_callers] == ["sys_sleep", "wakeup"]
    assert len(populated.query_callers("printk", limit=1)) == 1
    assert populated.query_callers("no-such-caller") == []


def test_query_callees_resolved_placeholder_and_unresolved(populated) -> None:
    rows = populated.query_callees("sys_sleep")
    assert len(rows) == 2
    assert set(rows[0]) == CALLEE_KEYS
    resolved = rows[0]
    assert resolved["callee_id"] == WAKEUP.id
    assert resolved["callee_name"] == "wakeup"
    assert resolved["callee_file"] == PROC_FILE
    assert resolved["callee_line"] == 40
    assert resolved["call_file"] == PROC_FILE
    assert resolved["call_line"] == 15
    assert resolved["is_resolved"] is True
    assert resolved["source"] == "call"
    assert resolved["confidence"] == 1.0

    wakeup_callees = populated.query_callees("wakeup")
    by_line = {row["call_line"]: row for row in wakeup_callees}
    assert set(by_line) == {45, 46}
    # Placeholder join: callee row exists (empty location), name/usr from call.
    assert by_line[45]["callee_id"] == KMALLOC.id
    assert by_line[45]["callee_name"] == "kmalloc"
    assert by_line[45]["callee_file"] == ""
    assert by_line[45]["callee_line"] == 0
    assert by_line[45]["is_resolved"] is False
    # Unresolved call: nothing to join, redundant name still returned.
    assert by_line[46]["callee_id"] == ""
    assert by_line[46]["callee_name"] == "printk"
    assert by_line[46]["is_resolved"] is False
    assert populated.query_callees("no-such-caller") == []


def test_query_function_detail(populated) -> None:
    detail = populated.query_function_detail("wakeup")
    assert set(detail) == {
        "function",
        "deep_ready",
        "branches",
        "assignments",
        "returns",
        "resource_ops",
        "findings",
    }
    assert detail["deep_ready"] is True
    function = detail["function"]
    assert function["id"] == WAKEUP.id
    assert function["is_definition"] is True
    assert function["file"] == PROC_FILE and function["line"] == 40
    assert function["params"] == []
    assert set(detail["branches"][0]) == BRANCH_KEYS
    assert detail["branches"][0]["condition"] == "flags & 1"
    assert detail["branches"][0]["var_names"] == ["flags"]
    assert detail["branches"][0]["kind"] == "if"
    assert detail["assignments"][0]["lhs"] == "x"
    assert detail["assignments"][0]["rhs"] == "kmalloc(16, 0)"
    assert detail["returns"][0]["expr"] == "x"
    assert detail["resource_ops"][0]["api_name"] == "kmalloc"
    assert detail["resource_ops"][0]["op"] == "acquire"
    assert len(detail["findings"]) == 1
    assert detail["findings"][0]["message"] == "x leaked"
    assert detail["findings"][0]["file"] == PROC_FILE
    assert detail["findings"][0]["line"] == 40

    sleep_detail = populated.query_function_detail("sys_sleep")
    assert sleep_detail["function"]["params"][0]["name"] == "seconds"
    assert sleep_detail["function"]["params"][0]["type_spelling"] == "unsigned int"

    missing = populated.query_function_detail("no-such-function")
    assert missing["function"] is None
    assert missing["deep_ready"] is False
    assert missing["branches"] == missing["assignments"] == []
    assert missing["returns"] == missing["resource_ops"] == missing["findings"] == []


def test_fact_queries(populated) -> None:
    branches = populated.query_branches("wakeup")
    assert len(branches) == 1 and set(branches[0]) == BRANCH_KEYS
    assert branches[0]["id"] == BRANCH.id
    assignments = populated.query_assignments("wakeup")
    assert len(assignments) == 1 and set(assignments[0]) == ASSIGNMENT_KEYS
    assert assignments[0]["is_decl_init"] is True
    returns = populated.query_returns("wakeup")
    assert len(returns) == 1 and set(returns[0]) == RETURN_KEYS
    assert returns[0]["is_implicit"] is False
    resource_ops = populated.query_resource_ops("wakeup")
    assert len(resource_ops) == 1 and set(resource_ops[0]) == RESOURCE_OP_KEYS
    assert resource_ops[0]["target_vars"] == ["x"]
    assert resource_ops[0]["call_id"] == CALL_WAKEUP_KMALLOC.id

    assert populated.query_branches("no-such-function") == []
    assert populated.query_assignments("no-such-function") == []
    assert populated.query_returns("no-such-function") == []
    assert populated.query_resource_ops("no-such-function") == []


def test_query_findings(populated) -> None:
    rows = populated.query_findings()
    assert len(rows) == 1
    assert set(rows[0]) == FINDING_KEYS
    row = rows[0]
    assert row["kind"] == "leak"
    assert row["function_name"] == "wakeup"
    assert row["message"] == "x leaked"
    assert row["file"] == PROC_FILE
    assert row["line"] == 40
    assert len(populated.query_findings(kind="leak")) == 1
    assert populated.query_findings(kind="double_release") == []
    assert populated.query_findings(limit=0) == []


def test_stats(populated) -> None:
    stats = populated.stats()
    assert set(stats) == {"tables", "db_bytes", "schema_version", "sqlite_version"}
    assert stats["tables"] == EXPECTED_TABLE_COUNTS
    assert stats["db_bytes"] > 0
    assert stats["schema_version"] == SCHEMA_VERSION
    assert stats["sqlite_version"] == sqlite3.sqlite_version


def test_iter_records_export(populated) -> None:
    records = list(populated.iter_records())
    assert len(records) == 18
    assert all(record["schema"] == SCHEMA_VERSION for record in records)
    kinds = [record["kind"] for record in records]
    assert kinds.count("compilation_unit") == 1
    assert kinds.count("function") == 3
    assert kinds.count("call") == 4
    assert kinds.count("branch") == 1

    functions = [record for record in records if record["kind"] == "function"]
    by_name = {record["name"]: record for record in functions}
    assert by_name["sys_sleep"]["params"][0]["name"] == "seconds"
    assert by_name["wakeup"]["is_definition"] is True
    calls = [record for record in records if record["kind"] == "call"]
    assert all(isinstance(record["is_resolved"], bool) for record in calls)
    assert all(record["callee_name"] for record in calls)

    selected = list(populated.iter_records(("function", "call")))
    assert [record["kind"] for record in selected] == ["function"] * 3 + ["call"] * 4
    assert len(list(populated.iter_records(("branch",), limit=1))) == 1
    assert list(populated.iter_records(limit=0)) == []
    with pytest.raises(ValueError):
        list(populated.iter_records(("bogus",)))

    stream = populated.iter_records()
    assert next(stream)["kind"] == "compilation_unit"
    stream.close()


def test_definition_preference_and_dedupe(tmp_path) -> None:
    store = SqliteStore(tmp_path / "dedupe.db")
    with store:
        store.begin()
        store.add_parse_result(ParseResult(tu=TU, functions=[SYS_SLEEP]))
        store.commit()
        declaration = dataclasses.replace(
            SYS_SLEEP, is_definition=False, sloc=_sloc(5), signature="int sys_sleep(unsigned int);"
        )
        store.begin()
        store.add_parse_result(
            ParseResult(tu=TU, functions=[declaration, SYS_SLEEP, declaration, SYS_SLEEP])
        )
        store.commit()
        store.finalize()
        # Re-ingesting an identical batch and finalizing again is idempotent.
        store.begin()
        store.add_parse_result(PARSE_RESULT)
        store.add_analysis_result(ANALYSIS)
        store.commit()
        store.finalize()

        detail = store.query_function_detail("sys_sleep")
        assert detail["function"]["is_definition"] is True
        assert detail["function"]["line"] == 10
        assert store.stats()["tables"]["functions"] == 3
        assert store.stats()["tables"]["calls"] == 4


def test_finalize_builds_indexes_and_queries_use_them(populated) -> None:
    connection = populated.connection
    function_plan = connection.execute(
        "EXPLAIN QUERY PLAN SELECT id FROM functions WHERE name = ?", ("wakeup",)
    ).fetchall()
    assert any("USING INDEX" in row["detail"] for row in function_plan)
    # Covering projections make the index choice unambiguous even on the tiny
    # fixture; at 1M rows the same indexes serve the caller/callee joins.
    caller_plan = connection.execute(
        "EXPLAIN QUERY PLAN SELECT callee_id FROM calls WHERE callee_id = ?", (WAKEUP.id,)
    ).fetchall()
    assert any("idx_calls_callee_id" in row["detail"] for row in caller_plan)
    branch_plan = connection.execute(
        "EXPLAIN QUERY PLAN SELECT function_id FROM branches WHERE function_id = ?", (WAKEUP.id,)
    ).fetchall()
    assert any("idx_branches_function_id" in row["detail"] for row in branch_plan)


def test_lifecycle_errors_and_idempotence(tmp_path) -> None:
    store = SqliteStore(tmp_path / "empty.db")
    with pytest.raises(RuntimeError):
        store.stats()
    with pytest.raises(RuntimeError):
        store.begin()
    store.open()
    store.open()  # idempotent
    assert store.closed is False
    store.commit()  # no-op without transaction
    store.finalize()
    assert store.stats()["tables"]["functions"] == 0
    store.close()
    store.close()  # idempotent
    assert store.closed is True
