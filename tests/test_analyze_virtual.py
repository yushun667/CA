"""Virtual dispatch candidate tests (task-37, analyze side).

Same-TU resolution: ``analyze_function`` turns virtual call sites plus
``FunctionRecord.overrides`` metadata into ``virtual_candidate`` CallTargetRecords
with confidence/reason. No override metadata means no candidate edge, and the
direct call semantics are untouched.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ca.analyze import analyze_function
from ca.core import ids
from ca.core.schema import CompilationUnit, FunctionRecord
from ca.parse import ParseOptions, parse_compilation_unit

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


def function_by_name(parsed, name: str) -> FunctionRecord:
    for function in parsed.functions:
        if function.qualified_name == name or function.name == name:
            return function
    raise AssertionError(f"function not found: {name}")


@pytest.fixture(scope="module")
def derived_unit():
    parsed = parse_compilation_unit(make_unit("ast_virtual_derived.cpp"))
    assert parsed.parse_ok is True, parsed.error
    return parsed


def test_virtual_call_produces_override_candidates(derived_unit) -> None:
    caller = function_by_name(derived_unit, "call_through_base")
    result = analyze_function(derived_unit, caller)
    candidates = [target for target in result.call_targets if target.kind == "virtual_candidate"]
    names = {target.callee_name for target in candidates}
    assert names == {"value"}
    # Derived::value and Leaf::value are both possible dynamic targets of
    # Base::value; they are a target set with no scored ranking.
    derived_value = function_by_name(derived_unit, "ast_virtual::Derived::value")
    leaf_value = function_by_name(derived_unit, "ast_virtual::Leaf::value")
    callee_ids = {target.callee_id for target in candidates}
    assert callee_ids == {derived_value.id, leaf_value.id}
    for target in candidates:
        assert target.caller_id == caller.id
        assert target.call_id
        assert "virtual override" in target.reason
        assert target.id.startswith("ct:")
    # Deepest candidate is Leaf (override of Derived::value); depth is evidence.
    leaf_candidate = next(t for t in candidates if t.callee_id == leaf_value.id)
    assert "override depth 2" in leaf_candidate.reason
    derived_candidate = next(t for t in candidates if t.callee_id == derived_value.id)
    assert "override depth 1" in derived_candidate.reason


def test_virtual_call_without_override_has_no_candidates(derived_unit) -> None:
    caller = function_by_name(derived_unit, "call_unrelated")
    result = analyze_function(derived_unit, caller)
    assert [target for target in result.call_targets if target.kind == "virtual_candidate"] == []


def test_candidates_are_deterministic(derived_unit) -> None:
    caller = function_by_name(derived_unit, "call_through_base")
    first = analyze_function(derived_unit, caller)
    second = analyze_function(derived_unit, caller)
    assert [(t.id, t.callee_id, t.reason) for t in first.call_targets] == [
        (t.id, t.callee_id, t.reason) for t in second.call_targets
    ]


def test_candidates_survive_main_file_scope_with_header_declarations() -> None:
    parsed = parse_compilation_unit(
        make_unit("ast_virtual_derived.cpp"),
        ParseOptions(record_scope="main_file", emit_header_declarations=True),
    )
    caller = function_by_name(parsed, "call_through_base")
    result = analyze_function(parsed, caller)
    candidates = [target for target in result.call_targets if target.kind == "virtual_candidate"]
    assert {t.callee_name for t in candidates} == {"value"}
    assert len(candidates) == 2


def test_max_targets_caps_candidate_set(derived_unit) -> None:
    from ca.analyze import AnalyzeOptions

    caller = function_by_name(derived_unit, "call_through_base")
    result = analyze_function(derived_unit, caller, AnalyzeOptions(max_call_targets=1))
    candidates = [target for target in result.call_targets if target.kind == "virtual_candidate"]
    assert len(candidates) == 1
    # The retained member is deterministic (not confidence-ranked).
    again = analyze_function(derived_unit, caller, AnalyzeOptions(max_call_targets=1))
    assert [target.id for target in candidates] == [
        target.id for target in again.call_targets if target.kind == "virtual_candidate"
    ]
