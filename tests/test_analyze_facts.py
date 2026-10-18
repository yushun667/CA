"""Assignment / return / resource-operation fact tests for ca.analyze."""

from __future__ import annotations

try:  # pragma: no cover - import layout differs when tests is a package
    from test_analyze_common import unit_for_fixture
except ImportError:  # pragma: no cover - e.g. server deployment with tests/__init__.py
    from tests.test_analyze_common import unit_for_fixture


def _analyze(fixture: str, function: str):
    return unit_for_fixture(fixture).analyze(function)


def _block_ids(result):
    return {block.id for block in result.cfg_blocks}


def test_assignments_forms_and_fields():
    result = _analyze("df_branches.c", "classify")
    assignments = result.assignments
    assert 12 <= len(assignments) <= 25

    decl_init = next(
        item for item in assignments if item.is_decl_init and item.lhs == "score"
    )
    assert decl_init.rhs == "0"
    assert decl_init.operator == "="
    assert decl_init.type_spelling == "int"
    assert decl_init.var_names == ("score",)

    compound = next(item for item in assignments if item.operator == "+=")
    assert compound.lhs == "score"
    assert compound.rhs == "10"
    assert compound.is_decl_init is False

    operators = {item.operator for item in assignments}
    assert {"=", "+=", "++", "--"} <= operators
    for item in assignments:
        assert item.id.startswith("assign:")
        assert item.function_id == result.function_id
        assert item.block_id in _block_ids(result)
        assert item.sloc.is_valid()


def test_return_records():
    result = _analyze("df_branches.c", "classify")
    assert len(result.returns) == 1
    record = result.returns[0]
    assert record.expr == "score"
    assert record.var_names == ("score",)
    assert record.is_implicit is False
    assert record.id.startswith("ret:")
    assert record.block_id in _block_ids(result)
    assert (record.sloc.line, record.sloc.col) == (
        record.sloc.line,
        record.sloc.col,
    )


def test_resource_ops_balanced_malloc():
    result = _analyze("df_resources.c", "balanced_malloc")
    ops = result.resource_ops
    assert len(ops) == 2
    acquire, release = ops
    assert (acquire.op, acquire.resource_kind, acquire.api_name) == (
        "acquire",
        "memory",
        "malloc",
    )
    assert acquire.target == "p"
    assert acquire.target_vars == ("p",)
    assert acquire.matcher == "default:malloc"
    assert acquire.id.startswith("res:")
    assert (release.op, release.api_name) == ("release", "free")
    assert release.target == "p"
    assert release.matcher == "default:free"
    assert acquire.sloc.line < release.sloc.line
    for op in ops:
        assert op.function_id == result.function_id
        assert op.block_id in _block_ids(result)


def test_resource_ops_lock_and_file_targets():
    lock = _analyze("df_resources.c", "lock_leak")
    assert [op.api_name for op in lock.resource_ops] == [
        "spin_lock",
        "spin_unlock",
    ]
    assert lock.resource_ops[0].target == "lk"
    assert lock.resource_ops[1].target == "lk"
    assert lock.resource_ops[0].resource_kind == "lock"

    file_ops = _analyze("df_resources.c", "file_pair")
    assert [op.api_name for op in file_ops.resource_ops] == ["open", "close"]
    assert file_ops.resource_ops[0].target == "fd"
    assert file_ops.resource_ops[1].target == "fd"
    assert file_ops.resource_ops[1].resource_kind == "file"


def test_custom_config_table_changes_matching():
    unit = unit_for_fixture("df_resources.c")
    options = {
        "resource_config": {
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
    }
    default = unit.analyze("custom_alloc")
    assert default.resource_ops == []
    configured = unit.analyze("custom_alloc", options)
    assert [op.api_name for op in configured.resource_ops] == ["my_alloc", "my_free"]
    assert configured.resource_ops[0].matcher == "config:my_alloc"
    assert configured.resource_ops[0].op == "acquire"
    assert configured.resource_ops[1].matcher == "config:my_free"


def test_macro_expansion_facts_are_bounded():
    unit = unit_for_fixture("df_macros.c")
    result = unit.analyze("macro_flow")
    for assignment in result.assignments:
        assert len(assignment.lhs) <= 300
        assert len(assignment.rhs) <= 300
        assert "#define" not in assignment.lhs + assignment.rhs
        assert assignment.sloc.is_valid()
    for branch in result.branches:
        assert len(branch.condition) <= 120
        assert "#define" not in branch.condition
        assert branch.sloc.is_valid()
    assert result.returns
    assert all(len(record.expr) <= 160 for record in result.returns)


def test_macro_loop_body_facts_are_recovered():
    result = unit_for_fixture("df_macros.c").analyze("free_macro_loop")
    api_names = [op.api_name for op in result.resource_ops]
    assert "malloc" in api_names
    assert api_names.count("free") >= 2  # buf + the macro-loop body statement
    assert result.findings == []


def test_records_sorted_by_position():
    for fixture, function in (
        ("df_branches.c", "classify"),
        ("df_resources.c", "leak_on_error"),
    ):
        result = _analyze(fixture, function)
        for bucket in (result.assignments, result.returns, result.resource_ops):
            positions = [(item.sloc.line, item.sloc.col) for item in bucket]
            assert positions == sorted(positions)
            ids = [item.id for item in bucket]
            assert len(set(ids)) == len(ids)
