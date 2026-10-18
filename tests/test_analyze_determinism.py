"""Determinism and protocol-adaptation tests for ca.analyze."""

from __future__ import annotations

from ca.analyze import AnalyzeOptions, analyze_function
from ca.analyze.function_analyzer import _definition_cursor
from ca.core.schema import record_to_dict
try:  # pragma: no cover - import layout differs when tests is a package
    from test_analyze_common import FakeParsedUnit, unit_for_fixture
except ImportError:  # pragma: no cover - e.g. server deployment with tests/__init__.py
    from tests.test_analyze_common import FakeParsedUnit, unit_for_fixture

_BUCKETS = (
    "cfg_blocks",
    "cfg_edges",
    "branches",
    "assignments",
    "returns",
    "resource_ops",
    "findings",
    "call_targets",
)


def _bucket_dicts(result):
    return {
        name: [record_to_dict(record) for record in getattr(result, name)]
        for name in _BUCKETS
    }


def _all_functions():
    cases = []
    for fixture in ("df_branches.c", "df_resources.c", "df_macros.c", "df_callbacks.c"):
        unit = unit_for_fixture(fixture)
        for function in unit.functions:
            cases.append((fixture, function.name))
    return cases


def test_two_runs_are_field_identical():
    unit = unit_for_fixture("df_branches.c")
    resource_unit = unit_for_fixture("df_resources.c")
    for fixture_unit, function in (
        (unit, "classify"),
        (unit, "implicit_path"),
        (resource_unit, "leak_on_error"),
        (resource_unit, "double_free"),
        (resource_unit, "lock_leak"),
    ):
        first = fixture_unit.analyze(function)
        second = fixture_unit.analyze(function)
        assert first.function_id == second.function_id
        assert _bucket_dicts(first) == _bucket_dicts(second)
        assert first.elapsed_ms >= 0.0
        assert second.elapsed_ms >= 0.0


def test_all_fixture_functions_are_deterministic_and_sorted():
    for fixture, function in _all_functions():
        unit = unit_for_fixture(fixture)
        first = unit.analyze(function)
        second = unit.analyze(function)
        assert first.function_id == second.function_id
        assert _bucket_dicts(first) == _bucket_dicts(second)
        for bucket_name in ("branches", "assignments", "returns", "resource_ops"):
            records = getattr(first, bucket_name)
            positions = [(record.sloc.line, record.sloc.col) for record in records]
            assert positions == sorted(positions), (fixture, function, bucket_name)
            ids = [record.id for record in records]
            assert len(set(ids)) == len(ids)
        block_ids = {block.id for block in first.cfg_blocks}
        for block in first.cfg_blocks:
            assert block.id.startswith("bb:")
            assert block.function_id == first.function_id
        for edge in first.cfg_edges:
            assert edge.src in block_ids and edge.dst in block_ids


def test_options_forms_are_equivalent():
    unit = unit_for_fixture("df_resources.c")
    function = unit.function("leak_on_error")
    default = analyze_function(unit, function)
    empty_mapping = analyze_function(unit, function, {})
    options_object = analyze_function(unit, function, AnalyzeOptions())
    assert _bucket_dicts(default) == _bucket_dicts(empty_mapping)
    assert _bucket_dicts(default) == _bucket_dicts(options_object)


class _ProtocolMinimalUnit:
    """ParsedUnit-shaped object without the optional definition_cursor helper."""

    def __init__(self, unit: FakeParsedUnit) -> None:
        self.tu = unit.tu
        self.functions = unit.functions
        self.types = ()
        self.fields = ()
        self.calls = ()
        self.diagnostics = ()
        self.parse_ok = True
        self.elapsed_ms = 0.0
        self._unit = unit

    def clang_tu(self):
        return self._unit.clang_tu()

    def parse_result(self):  # pragma: no cover - not used by analyze
        raise NotImplementedError


def test_usr_fallback_when_protocol_method_missing():
    unit = unit_for_fixture("df_branches.c")
    minimal = _ProtocolMinimalUnit(unit)
    for name in ("classify", "implicit_path"):
        function = unit.function(name)
        expected = analyze_function(unit, function)
        actual = analyze_function(minimal, function)
        assert actual.function_id == expected.function_id
        assert _bucket_dicts(actual) == _bucket_dicts(expected)


def test_missing_cursor_returns_empty_result():
    unit = unit_for_fixture("df_branches.c")
    function = unit.function("classify")
    missing = analyze_function(_NoTuUnit(), function)
    assert missing.function_id == function.id
    for bucket_name in _BUCKETS:
        assert getattr(missing, bucket_name) == []


class _NoTuUnit:
    tu = None

    def clang_tu(self):
        return None


def test_definition_cursor_helper_uses_protocol():
    unit = unit_for_fixture("df_branches.c")
    function = unit.function("classify")
    cursor = _definition_cursor(unit, function)
    assert cursor is not None
    assert cursor.is_definition()
