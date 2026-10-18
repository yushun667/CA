"""Indirect-call / callback candidate resolution tests for ca.analyze."""

from __future__ import annotations

from collections import defaultdict

from ca.core import ids as core_ids
from ca.core.schema import CompilationUnit
from ca.parse import parse_compilation_unit
try:  # pragma: no cover - import layout differs when tests is a package
    from test_analyze_common import DEFAULT_ARGS, REPO_ROOT, repo_rel, unit_for_fixture
except ImportError:  # pragma: no cover - e.g. server deployment with tests/__init__.py
    from tests.test_analyze_common import DEFAULT_ARGS, REPO_ROOT, repo_rel, unit_for_fixture

# Ground truth for tests/fixtures/df_callbacks.c: caller -> expected callees.
EXPECTED = {
    "call_global": {"handler_b"},
    "dispatch": {"handler_neg"},
    "store_field": {"handler_a"},
    "call_field": {"handler_a"},
    "address_of": {"cb_void"},
    "call_array": {"handler_a", "handler_b"},
    "call_designated": {"handler_a", "handler_b"},
    "call_factory": {"handler_a"},
}
NEGATIVE_FUNCTIONS = ("mismatched", "unmatched")


def _emitted(unit):
    mapping: dict[str, set[str]] = defaultdict(set)
    records = []
    for function in unit.functions:
        result = unit.analyze(function.name)
        for record in result.call_targets:
            mapping[function.name].add(record.callee_name)
            records.append((function.name, record))
    return mapping, records


def test_fixture_precision_and_recall():
    unit = unit_for_fixture("df_callbacks.c")
    emitted, records = _emitted(unit)
    expected_pairs = {
        (function, callee)
        for function, callees in EXPECTED.items()
        for callee in callees
    }
    emitted_pairs = {
        (function, callee)
        for function, callees in emitted.items()
        for callee in callees
    }
    true_positive = expected_pairs & emitted_pairs
    precision = len(true_positive) / len(emitted_pairs) if emitted_pairs else 0.0
    recall = len(true_positive) / len(expected_pairs) if expected_pairs else 0.0
    assert emitted_pairs == expected_pairs
    assert precision == 1.0
    assert recall == 1.0
    assert len(records) == 10


def test_candidate_record_shape_and_evidence():
    unit = unit_for_fixture("df_callbacks.c")
    function_ids = {function.id for function in unit.functions}
    _, records = _emitted(unit)
    ids = [record.id for _, record in records]
    assert len(set(ids)) == len(ids)
    for function_name, record in records:
        assert record.kind == "indirect_candidate"
        assert record.caller_id != ""
        assert record.callee_id in function_ids
        assert record.callee_name in EXPECTED[function_name]
        assert record.reason
        assert record.sloc.is_valid()
        assert record.id == core_ids.call_target_id(
            record.call_id, record.callee_id, record.kind
        )
        assert record.call_id.startswith("call:")
        assert record.reason
    # Candidate target sets are evidence-only: every record names its
    # registration/dispatch evidence and there is no scored ranking.
    assert all(record.reason for _, record in records)


def test_self_suffix_candidate_is_suppressed():
    result = unit_for_fixture("df_callback_self.c").analyze("target_self")
    assert result.call_targets == []
    # The registration site itself has no indirect call and stays empty too.
    setup = unit_for_fixture("df_callback_self.c").analyze("setup_self")
    assert setup.call_targets == []


def test_negative_cases_emit_no_candidates():
    unit = unit_for_fixture("df_callbacks.c")
    for function_name in NEGATIVE_FUNCTIONS:
        result = unit.analyze(function_name)
        assert result.call_targets == [], function_name


def test_records_are_deterministic():
    unit = unit_for_fixture("df_callbacks.c")
    for function_name in EXPECTED:
        first = unit.analyze(function_name)
        second = unit.analyze(function_name)
        assert [record.id for record in first.call_targets] == [
            record.id for record in second.call_targets
        ]
        assert [(record.id, record.reason) for record in first.call_targets] == [
            (record.id, record.reason) for record in second.call_targets
        ]
        positions = [
            (record.sloc.line, record.sloc.col) for record in first.call_targets
        ]
        assert positions == sorted(positions)


def test_call_ids_link_to_parsed_indirect_calls():
    fixture = REPO_ROOT / "tests" / "fixtures" / "df_callbacks.c"
    relative = repo_rel(str(fixture))
    unit_record = CompilationUnit(
        id=core_ids.tu_id(relative, str(fixture.parent), DEFAULT_ARGS),
        file=str(fixture),
        directory=str(fixture.parent),
        repo_root=str(REPO_ROOT),
        arguments=DEFAULT_ARGS,
        language="c",
        source="synthetic",
    )
    parsed = parse_compilation_unit(unit_record)
    assert parsed.parse_ok
    functions = {function.name: function for function in parsed.functions}
    indirect_ids = {
        call.id
        for call in parsed.calls
        if call.kind != "direct"
    }
    saw_candidate = False
    for name in ("call_field", "call_global", "dispatch"):
        function = functions[name]
        from ca.analyze import analyze_function

        result = analyze_function(parsed, function)
        for record in result.call_targets:
            saw_candidate = True
            assert record.call_id in indirect_ids
    assert saw_candidate
