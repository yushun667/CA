"""Contract tests for the shared schema and stable ID rules (Lead-owned)."""

from __future__ import annotations

import json

from ca.core.ids import (
    call_id,
    cfg_block_id,
    digest,
    function_id,
    resource_op_id,
    sloc_key,
    tu_id,
    type_id,
)
from ca.core.schema import (
    SCHEMA_VERSION,
    AnalysisResult,
    BranchRecord,
    CallRecord,
    CompilationUnit,
    FunctionRecord,
    ParseResult,
    Sloc,
    iter_record_dicts,
)

SLOC = Sloc("kernel/proc.c", 10, 5, 10, 30)


def test_ids_are_stable_and_prefixed() -> None:
    assert digest("a", "b") == digest("a", "b")
    assert digest("a", "b") != digest("a", "c")

    external = function_id(name="sleep", usr="c:@F@sleep", sloc=SLOC, linkage="external")
    external_again = function_id(name="sleep", usr="c:@F@sleep", sloc=SLOC, linkage="external")
    internal = function_id(name="sleep", usr="c:@F@sleep", sloc=SLOC, linkage="internal")
    assert external == external_again
    assert external != internal
    assert external.startswith("fn:")
    assert type_id(name="proc", usr="c:@S@proc", sloc=SLOC, kind="struct").startswith("ty:")
    assert tu_id("a.c", "/repo", ["-c", "a.c"]).startswith("tu:")
    assert call_id(caller_id=external, callee_usr="c:@F@sleep", callee_name="sleep", sloc=SLOC).startswith("call:")
    assert cfg_block_id(external, 3).startswith("bb:")
    assert resource_op_id(external, SLOC, "acquire", 1).startswith("res:")
    assert sloc_key(SLOC) == "kernel/proc.c:10:5"


def test_record_stream_has_kinds_and_valid_json() -> None:
    tu = CompilationUnit(
        id=tu_id("kernel/proc.c", "/repo", ["-c", "kernel/proc.c"]),
        file="/repo/kernel/proc.c",
        directory="/repo",
        repo_root="/repo",
        arguments=("-c", "kernel/proc.c"),
    )
    function = FunctionRecord(
        id=function_id(name="sleep", usr="c:@F@sleep", sloc=SLOC, linkage="external"),
        tu_id=tu.id,
        name="sleep",
        qualified_name="sleep",
        usr="c:@F@sleep",
        kind="function",
        sloc=SLOC,
        is_definition=True,
    )
    call = CallRecord(
        id="call:x",
        tu_id=tu.id,
        caller_id=function.id,
        sloc=SLOC,
        callee_id="",
        callee_name="wakeup",
    )
    parse_result = ParseResult(tu=tu, functions=[function], calls=[call])
    analysis = AnalysisResult(
        function_id=function.id,
        branches=[
            BranchRecord(
                id="branch:x",
                function_id=function.id,
                block_id="bb:x",
                sloc=SLOC,
                kind="if",
                condition="lk != 0",
            )
        ],
    )

    records = list(iter_record_dicts(parse_result, [analysis]))
    kinds = [record["kind"] for record in records]
    assert kinds[0] == "parse_result"
    assert "function" in kinds
    assert "call" in kinds
    assert "branch" in kinds
    for record in records:
        assert record["schema"] == SCHEMA_VERSION
        json.dumps(record)  # must be JSON serializable

    # CompilationUnit is part of every parse result and must stay serializable.
    assert records[0]["tu"]["id"] == tu.id
