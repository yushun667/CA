"""Resource data-flow finding tests for ca.analyze."""

from __future__ import annotations

import json

try:  # pragma: no cover - import layout differs when tests is a package
    from test_analyze_common import unit_for_fixture
except ImportError:  # pragma: no cover - e.g. server deployment with tests/__init__.py
    from tests.test_analyze_common import unit_for_fixture


def _analyze(function: str, options=None):
    return unit_for_fixture("df_resources.c").analyze(function, options)


def _findings_by_kind(result, kind: str):
    return [item for item in result.findings if item.kind == kind]


def test_balanced_functions_have_no_findings():
    for function in ("balanced_malloc", "lock_pair", "file_pair", "conditional_free"):
        result = _analyze(function)
        assert result.resource_ops, function
        assert result.findings == [], function


def test_leak_on_early_return_path():
    result = _analyze("leak_on_error")
    leaks = _findings_by_kind(result, "leak")
    assert len(leaks) == 1
    finding = leaks[0]
    assert finding.resource_kind == "memory"
    assert finding.severity == "warning"
    assert finding.acquire_id.startswith("res:")
    assert finding.release_ids == ()
    assert finding.path_blocks
    block_ids = {block.id for block in result.cfg_blocks}
    assert set(finding.path_blocks) <= block_ids
    assert "malloc" in finding.message
    assert any(item.startswith("matcher=default:malloc") for item in finding.evidence)
    assert finding.id.startswith("finding:")


def test_double_release():
    result = _analyze("double_free")
    doubles = _findings_by_kind(result, "double_release")
    assert len(doubles) == 1
    finding = doubles[0]
    assert finding.resource_kind == "memory"
    assert finding.release_ids and finding.release_ids[0].startswith("res:")
    assert "already released" in finding.message


def test_lock_and_file_leaks():
    lock = _analyze("lock_leak")
    leaks = _findings_by_kind(lock, "leak")
    assert len(leaks) == 1
    assert leaks[0].resource_kind == "lock"
    assert "spin_lock" in leaks[0].message

    file_result = _analyze("file_leak")
    file_leaks = _findings_by_kind(file_result, "leak")
    assert len(file_leaks) == 1
    assert file_leaks[0].resource_kind == "file"
    assert "fopen" in file_leaks[0].message


def test_release_without_matching_acquire():
    result = _analyze("unmatched_release")
    unreleased = _findings_by_kind(result, "unreleased")
    assert len(unreleased) == 1
    finding = unreleased[0]
    assert finding.severity == "info"
    assert finding.resource_kind == "memory"
    assert "no acquiring call" in finding.message


def test_custom_config_via_option_and_json(tmp_path):
    extend = {
        "extend": [
            {
                "api": "my_alloc",
                "op": "acquire",
                "resource_kind": "memory",
                "target": "return",
            },
            {
                "api": "my_free",
                "op": "release",
                "resource_kind": "memory",
                "target": "arg0",
            },
        ]
    }
    configured = _analyze("custom_leak", {"resource_config": extend})
    leaks = _findings_by_kind(configured, "leak")
    assert len(leaks) == 1
    assert leaks[0].resource_kind == "memory"
    assert "my_alloc" in leaks[0].message
    assert any(item.startswith("matcher=config:my_alloc") for item in leaks[0].evidence)

    config_path = tmp_path / "resource_config.json"
    config_path.write_text(json.dumps(extend), encoding="utf-8")
    from_path = _analyze("custom_alloc", {"resource_config": str(config_path)})
    assert [op.api_name for op in from_path.resource_ops] == ["my_alloc", "my_free"]
    assert from_path.findings == []

    # A full replacement table drops the built-in defaults.
    replace_path = tmp_path / "replace.json"
    replace_path.write_text(
        json.dumps({"entries": extend["extend"]}), encoding="utf-8"
    )
    replaced = _analyze("balanced_malloc", {"resource_config": str(replace_path)})
    assert replaced.resource_ops == []


def test_findings_are_deterministic_and_well_formed():
    result = _analyze("leak_on_error")
    again = _analyze("leak_on_error")
    assert [item.id for item in result.findings] == [item.id for item in again.findings]
    ids = [item.id for item in result.findings]
    assert len(set(ids)) == len(ids)
    for finding in result.findings:
        assert finding.function_id == result.function_id
        assert finding.kind in {
            "leak",
            "double_release",
            "balanced",
            "use_after_release",
            "unreleased",
        }
        assert finding.message
