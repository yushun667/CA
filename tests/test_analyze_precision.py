"""Precision tests: guarded pairing, loop termination and noreturn paths."""

from __future__ import annotations

try:  # pragma: no cover - import layout differs when tests is a package
    from test_analyze_common import unit_for_fixture
except ImportError:  # pragma: no cover - e.g. server deployment with tests/__init__.py
    from tests.test_analyze_common import unit_for_fixture


def _leaks(result):
    return [item for item in result.findings if item.kind == "leak"]


def test_guarded_acquire_release_pair_is_not_a_leak():
    result = unit_for_fixture("df_guards.c").analyze("guarded_pair")
    assert [op.api_name for op in result.resource_ops] == ["acquire", "release"]
    assert _leaks(result) == []
    assert [item for item in result.findings if item.kind == "unreleased"] == []


def test_infinite_loops_do_not_fall_through_to_exit():
    unit = unit_for_fixture("df_guards.c")
    for name in ("loop_with_break", "while_with_break", "spin_no_exit"):
        result = unit.analyze(name)
        assert [item for item in result.findings if item.kind == "leak"] == [], name
        assert [
            item for item in result.findings if item.kind == "unreleased"
        ] == [], name


def test_noreturn_panic_path_is_not_a_leak():
    result = unit_for_fixture("df_guards.c").analyze("panic_path")
    assert [op.api_name for op in result.resource_ops] == ["malloc", "free"]
    assert _leaks(result) == []


def test_genuine_conditional_leak_is_still_reported():
    result = unit_for_fixture("df_guards.c").analyze("still_leaks")
    leaks = _leaks(result)
    assert len(leaks) == 1
    assert leaks[0].resource_kind == "lock"
    assert "acquire" in leaks[0].message


def test_unreleased_only_reported_for_local_targets():
    unit = unit_for_fixture("df_resources.c")
    local_release = unit.analyze("unmatched_release")
    assert len([item for item in local_release.findings if item.kind == "unreleased"]) == 1

    param_release = unit_for_fixture("df_guards.c").analyze("release_param")
    assert param_release.resource_ops
    assert [item for item in param_release.findings if item.kind == "unreleased"] == []
