"""CFG and branch-condition tests for ca.analyze."""

from __future__ import annotations

try:  # pragma: no cover - import layout differs when tests is a package
    from test_analyze_common import unit_for_fixture
except ImportError:  # pragma: no cover - e.g. server deployment with tests/__init__.py
    from tests.test_analyze_common import unit_for_fixture

EXPECTED_BRANCH_KINDS = {
    "if",
    "else_if",
    "while",
    "do_while",
    "for",
    "switch",
    "case",
    "ternary",
    "logical_and",
    "logical_or",
}

EXPECTED_EDGE_KINDS = {
    "unconditional",
    "true",
    "false",
    "switch_case",
    "switch_default",
    "back",
}


def _classify():
    return unit_for_fixture("df_branches.c").analyze("classify")


def test_cfg_block_and_edge_shape():
    result = _classify()
    blocks = result.cfg_blocks
    edges = result.cfg_edges
    assert 35 <= len(blocks) <= 80
    assert len(edges) >= 55
    assert {block.kind for block in blocks} >= {
        "entry",
        "exit",
        "normal",
        "branch",
        "switch",
        "loop_header",
    }
    assert {block.terminator for block in blocks} >= {
        "none",
        "conditional",
        "return",
        "switch",
        "goto",
        "unconditional",
    }
    block_ids = {block.id for block in blocks}
    assert len(block_ids) == len(blocks)
    for index, block in enumerate(blocks):
        assert block.index == index
        assert block.function_id == result.function_id
        assert block.id.startswith("bb:")
        assert block.sloc.is_valid()
    edge_ids = {edge.id for edge in edges}
    assert len(edge_ids) == len(edges)
    for edge in edges:
        assert edge.id.startswith("edge:")
        assert edge.kind in EXPECTED_EDGE_KINDS
        assert edge.src in block_ids
        assert edge.dst in block_ids


def test_branch_constructs_and_conditions():
    result = _classify()
    kinds = {branch.kind for branch in result.branches}
    assert EXPECTED_BRANCH_KINDS <= kinds

    blocks = {block.id: block for block in result.cfg_blocks}
    for branch in result.branches:
        assert branch.function_id == result.function_id
        assert branch.id.startswith("branch:")
        assert branch.block_id in blocks
        assert "\n" not in branch.condition
        assert "  " not in branch.condition
        if branch.true_block:
            assert branch.true_block in blocks
        if branch.false_block:
            assert branch.false_block in blocks

    outer_if = next(
        branch
        for branch in result.branches
        if branch.kind == "if" and branch.condition == "x > 0 && y > 0"
    )
    assert outer_if.var_names == ("x", "y")
    assert outer_if.is_negated is False

    else_if = next(branch for branch in result.branches if branch.kind == "else_if")
    assert else_if.condition == "x < 0 || y < 0"
    assert else_if.var_names == ("x", "y")

    while_branch = next(branch for branch in result.branches if branch.kind == "while")
    assert while_branch.condition == "score > 0"
    assert while_branch.var_names == ("score",)
    for_branch = next(branch for branch in result.branches if branch.kind == "for")
    assert for_branch.condition == "i < 3"
    do_branch = next(branch for branch in result.branches if branch.kind == "do_while")
    assert do_branch.condition == "score < 5"

    switch_branch = next(branch for branch in result.branches if branch.kind == "switch")
    assert switch_branch.condition == "x"
    case_values = {
        branch.case_value for branch in result.branches if branch.kind == "case"
    }
    assert {"0", "1", "2", ""} <= case_values
    default_case = next(
        branch
        for branch in result.branches
        if branch.kind == "case" and branch.case_value == ""
    )
    assert default_case.condition == ""

    ternary = next(branch for branch in result.branches if branch.kind == "ternary")
    assert ternary.condition == "flag"
    assert ternary.var_names == ("flag",)
    assert ternary.true_block in blocks
    assert ternary.false_block in blocks

    negated = [
        branch
        for branch in result.branches
        if branch.kind == "if" and branch.condition == "!flag"
    ]
    assert negated and negated[0].is_negated is True


def test_short_circuit_splits_condition_blocks():
    result = _classify()
    blocks = {block.id: block for block in result.cfg_blocks}
    logical = next(
        branch for branch in result.branches if branch.kind == "logical_and"
    )
    assert logical.condition == "x > 0"
    true_edges = [
        edge
        for edge in result.cfg_edges
        if edge.src == logical.block_id and edge.kind == "true"
    ]
    assert true_edges, "short-circuit && must have a true edge to the next operand"
    next_operand = blocks[true_edges[0].dst]
    outgoing_kinds = {
        edge.kind for edge in result.cfg_edges if edge.src == next_operand.id
    }
    assert {"true", "false"} <= outgoing_kinds

    or_branch = next(
        branch for branch in result.branches if branch.kind == "logical_or"
    )
    or_false_edges = [
        edge
        for edge in result.cfg_edges
        if edge.src == or_branch.block_id and edge.kind == "false"
    ]
    assert or_false_edges


def test_loop_and_jump_edges():
    result = _classify()
    labels = {edge.label for edge in result.cfg_edges}
    assert {"break", "continue", "out"} <= labels
    assert any(edge.kind == "back" for edge in result.cfg_edges)
    assert any(edge.kind == "switch_case" for edge in result.cfg_edges)
    assert any(edge.kind == "switch_default" for edge in result.cfg_edges)


def test_implicit_return_record():
    unit = unit_for_fixture("df_branches.c")
    implicit = unit.analyze("implicit_path")
    implicit_returns = [item for item in implicit.returns if item.is_implicit]
    assert len(implicit_returns) == 1
    assert implicit_returns[0].expr == ""
    assert implicit_returns[0].var_names == ()
    explicit_returns = [item for item in implicit.returns if not item.is_implicit]
    assert len(explicit_returns) == 1
    assert explicit_returns[0].expr == "1"

    classify = unit.analyze("classify")
    assert [item for item in classify.returns if item.is_implicit] == []


def test_branch_records_sorted_by_position():
    result = _classify()
    positions = [(branch.sloc.line, branch.sloc.col) for branch in result.branches]
    assert positions == sorted(positions)
    ids = [branch.id for branch in result.branches]
    assert len(set(ids)) == len(ids)
