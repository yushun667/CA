"""Entry point: ``analyze_function(unit, function, options=None)``.

Consumes the parse module's :class:`ca.core.interfaces.ParsedUnit` protocol and
produces one :class:`ca.core.schema.AnalysisResult` per function definition.
Production code only depends on the protocol methods ``clang_tu()`` and
``definition_cursor(function_id)``; a best-effort USR scan over the live TU is
used only when ``definition_cursor`` is absent or returns nothing, so the
analyzer can be exercised before the parse module is complete.

Determinism: every record bucket is ordered by ``(line, col, creation
ordinal)`` and IDs come from :mod:`ca.core.ids`. ``elapsed_ms`` is the only
intentionally non-reproducible field (wall-clock timing).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, fields
from typing import Any

from clang.cindex import CursorKind, TypeKind

from ca.analyze.astutil import (
    SourceCache,
    cursor_sloc,
    find_child_stmt,
    iter_descendants,
)
from ca.analyze.callbacks import resolve_call_targets
from ca.analyze.cfg import (
    Cfg,
    CfgBudgetExceeded,
    CfgBuilder,
)
from ca.analyze.dataflow import analyze_flows
from ca.analyze.facts import FactExtractor, PendingRecord, resource_target_key
from ca.analyze.resources import ResourceConfig
from ca.core import ids as core_ids
from ca.core.schema import (
    AnalysisResult,
    AssignmentRecord,
    BranchRecord,
    CfgBlockRecord,
    CfgEdgeRecord,
    FindingRecord,
    FunctionRecord,
    ResourceOpRecord,
    ReturnRecord,
    Sloc,
)

_EDGE_KIND_ORDER = {
    "true": 0,
    "false": 1,
    "switch_case": 2,
    "switch_default": 3,
    "back": 4,
    "unconditional": 5,
}

_FUNCTION_KINDS = (
    CursorKind.FUNCTION_DECL,
    CursorKind.CXX_METHOD,
    CursorKind.CONSTRUCTOR,
    CursorKind.DESTRUCTOR,
    CursorKind.CONVERSION_FUNCTION,
    CursorKind.FUNCTION_TEMPLATE,
)


@dataclass(frozen=True)
class AnalyzeOptions:
    """Tunables for one analysis run; accepts a mapping or an object too."""

    resource_config: Any = None
    max_condition_chars: int = 120
    max_expr_chars: int = 160
    max_assignment_chars: int = 300
    max_var_names: int = 16
    max_blocks: int = 20000
    max_call_targets: int = 64
    call_target_min_confidence: float = 0.4
    # Optional guard for pathological bodies (template/macro expansions from
    # OpenHarmony can be MBs of source). 0 means unlimited (default).
    max_function_bytes: int = 0

    @classmethod
    def from_value(cls, value: Any) -> "AnalyzeOptions":
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        known = {item.name for item in fields(cls)}
        if isinstance(value, dict):
            kwargs = {key: val for key, val in value.items() if key in known}
            return cls(**kwargs)
        kwargs = {}
        for name in known:
            if hasattr(value, name):
                kwargs[name] = getattr(value, name)
        return cls(**kwargs)


def analyze_function(
    unit: Any,
    function: FunctionRecord,
    options: Any = None,
) -> AnalysisResult:
    """Analyze one function definition and return its CFG/data-flow facts."""

    started = time.perf_counter()
    opts = AnalyzeOptions.from_value(options)
    result = AnalysisResult(function_id=function.id)
    cursor = _definition_cursor(unit, function)
    if cursor is None or not _is_definition(cursor):
        return result
    body = find_child_stmt(cursor)
    if body is None:
        return result
    if opts.max_function_bytes:
        from ca.analyze.astutil import extent_span

        if extent_span(body) > opts.max_function_bytes:
            return result

    cache = SourceCache(_repo_root(unit))
    builder = CfgBuilder(
        cache,
        max_condition_chars=opts.max_condition_chars,
        max_var_names=opts.max_var_names,
        max_blocks=opts.max_blocks,
    )
    try:
        cfg = builder.build(cursor)
    except CfgBudgetExceeded:
        return result
    config = ResourceConfig.from_options(opts.resource_config)
    extractor = FactExtractor(
        cache,
        config,
        cfg.block_map(),
        function_id=function.id,
        max_expr_chars=opts.max_expr_chars,
        max_assignment_chars=opts.max_assignment_chars,
        max_var_names=opts.max_var_names,
    )
    try:
        extractor.run(body)
    except RecursionError:  # pragma: no cover - pathological macro nesting
        pass

    blocks = _build_blocks(cfg, function.id)
    block_ids = [block.id for block in blocks]
    branches = _build_branches(cfg, function.id, block_ids)
    assignments = _finalize_pending(
        _dedupe_pending(extractor.assignments),
        lambda sloc, ordinal: core_ids.assignment_id(function.id, sloc, ordinal),
        block_ids,
    )
    returns = _finalize_pending(
        extractor.returns,
        lambda sloc, ordinal: None,
        block_ids,
        assign_ids=False,
    )
    implicit = _implicit_return(cfg, cursor, function)
    if implicit is not None:
        returns.append(implicit)
    returns = _finalize_returns(function.id, returns, block_ids)

    ordered_ops = sorted(
        extractor.resource_ops,
        key=lambda pending: (
            pending.record.sloc.line,
            pending.record.sloc.col,
            pending.record.api_name,
            pending.ordinal,
        ),
    )
    resource_ops: list[ResourceOpRecord] = []
    op_sites: list[PendingRecord] = []
    for position, pending in enumerate(ordered_ops):
        record = pending.record
        record.block_id = _safe_block_id(block_ids, pending.block_index)
        record.id = core_ids.resource_op_id(
            function.id, record.sloc, record.api_name, position
        )
        resource_ops.append(record)
        op_sites.append(pending)

    edges = _build_edges(cfg, function.id, block_ids)
    transferred: set[str] = set()
    for record in returns:
        if not record.expr:
            continue
        simple = _returned_simple_var(record.expr)
        if simple:
            transferred.add(resource_target_key(simple))
            transferred.add(resource_target_key("&" + simple))
    transferred |= _escaped_targets(assignments)
    transferred |= set(extractor.escaped_vars)
    transferred |= set(extractor.borrowed_vars)
    acquired_kinds = {
        op.resource_kind for op in resource_ops if op.op in ("acquire", "copy")
    }
    result_vars: dict[str, str] = {}
    for pending in ordered_ops:
        status_var = pending.meta.get("result_var")
        if status_var:
            result_vars[pending.record.id] = status_var
    if op_sites:
        findings = analyze_flows(
            function_id=function.id,
            blocks=blocks,
            edges=edges,
            ops=op_sites,
            branches=branches,
            transferred=transferred,
            local_targets=set(extractor.local_vars),
            acquired_kinds=acquired_kinds,
            result_vars=result_vars,
            function_name=function.name,
            param_names=_parameter_names(cursor) | set(extractor.param_vars),
            entry_index=cfg.entry_index,
            exit_index=cfg.exit_index,
        )
    else:
        findings = []
    findings.sort(key=lambda item: (item.kind, item.acquire_id, item.message))
    for finding in findings:
        finding.id = core_ids.finding_id(function.id, finding.kind, finding.acquire_id)

    call_targets = resolve_call_targets(
        unit,
        function,
        cursor,
        cache,
        min_confidence=opts.call_target_min_confidence,
        max_targets=opts.max_call_targets,
    )

    result = AnalysisResult(
        function_id=function.id,
        cfg_blocks=blocks,
        cfg_edges=edges,
        branches=branches,
        assignments=assignments,
        returns=returns,
        resource_ops=resource_ops,
        findings=findings,
        call_targets=call_targets,
        elapsed_ms=round((time.perf_counter() - started) * 1000.0, 3),
    )
    return result


def _escaped_targets(assignments: list[AssignmentRecord]) -> set[str]:
    """Variables stored into another object/global: ownership left the local.

    ``(*f0)->pipe = pi`` or ``*pte = PA2PTE(pagetable) | PTE_V`` means the
    allocated object now belongs to a caller/global, so a local leak would be a
    false positive.
    """

    escaped: set[str] = set()
    for record in assignments:
        if record.is_decl_init:
            continue
        lhs = record.lhs.strip()
        rhs = " ".join(record.rhs.split())
        for name in record.var_names:
            if not name or name == lhs:
                continue
            if name in rhs.split() or any(
                token == name
                for token in _identifier_tokens(rhs)
            ):
                escaped.add(name)
                escaped.add("&" + name)
    return escaped


def _identifier_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    current = ""
    for char in text:
        if char.isalnum() or char == "_":
            current += char
        else:
            if current:
                tokens.append(current)
            current = ""
    if current:
        tokens.append(current)
    return tokens


_CAST_RETURN = re.compile(r"^(?:\([^()]*\)\s*)*&?\s*([A-Za-z_]\w*)$")


def _returned_simple_var(expr: str) -> str:
    """Return the variable name when ``expr`` is just a var/cast-of-var.

    ``return (struct cmd *)cmd;`` transfers ownership; ``return p != 0;`` does
    not. This keeps ownership-transfer suppression from hiding real leaks.
    """

    compact = " ".join(expr.split())
    if not compact:
        return ""
    match = _CAST_RETURN.match(compact)
    return match.group(1) if match else ""


# --------------------------------------------------------------------------- #
# Unit/cursor adaptation
# --------------------------------------------------------------------------- #


def _repo_root(unit: Any) -> str:
    record = getattr(unit, "tu", None)
    root = getattr(record, "repo_root", "") if record is not None else ""
    return root if isinstance(root, str) else ""


def _definition_cursor(unit: Any, function: FunctionRecord) -> Any:
    method = getattr(unit, "definition_cursor", None)
    if callable(method):
        try:
            cursor = method(function.id)
        except Exception:  # pragma: no cover - parse implementation detail
            cursor = None
        if cursor is not None:
            return cursor
    clang_tu = _clang_tu(unit)
    if clang_tu is None:
        return None
    usr = getattr(function, "usr", "") or ""
    name = getattr(function, "name", "") or ""
    fallback: Any = None
    for cursor in iter_descendants(clang_tu.cursor):
        if cursor.kind not in _FUNCTION_KINDS:
            continue
        try:
            if not cursor.is_definition():
                continue
            cursor_usr = cursor.get_usr()
        except Exception:  # pragma: no cover
            continue
        if usr and cursor_usr == usr:
            return cursor
        if not usr and cursor.spelling == name and fallback is None:
            fallback = cursor
    return fallback


def _clang_tu(unit: Any) -> Any:
    method = getattr(unit, "clang_tu", None)
    if not callable(method):
        return None
    try:
        return method()
    except Exception:  # pragma: no cover - parse failure
        return None


def _parameter_names(function_cursor: Any) -> set[str]:
    names: set[str] = set()
    try:
        for child in function_cursor.get_children():
            if child.kind == CursorKind.PARM_DECL and child.spelling:
                names.add(child.spelling)
    except Exception:  # pragma: no cover - defensive
        return names
    return names


def _is_definition(cursor: Any) -> bool:
    try:
        return bool(cursor.is_definition())
    except Exception:  # pragma: no cover
        return False


# --------------------------------------------------------------------------- #
# Record construction
# --------------------------------------------------------------------------- #


def _safe_block_id(block_ids: list[str], index: int) -> str:
    if 0 <= index < len(block_ids):
        return block_ids[index]
    return ""


def _build_blocks(cfg: Cfg, function_id: str) -> list[CfgBlockRecord]:
    blocks: list[CfgBlockRecord] = []
    for block in cfg.blocks:
        sloc = block.sloc if block.sloc.is_valid() else Sloc("")
        blocks.append(
            CfgBlockRecord(
                id=core_ids.cfg_block_id(function_id, block.index),
                function_id=function_id,
                index=block.index,
                kind=block.kind,
                terminator=block.terminator or "none",
                sloc=sloc,
                statement_count=len(block.stmts),
            )
        )
    return blocks


def _build_branches(
    cfg: Cfg, function_id: str, block_ids: list[str]
) -> list[BranchRecord]:
    pending = sorted(
        enumerate(cfg.branches),
        key=lambda item: (item[1].sloc.line, item[1].sloc.col, item[0]),
    )
    records: list[BranchRecord] = []
    for position, (_, spec) in enumerate(pending):
        record = BranchRecord(
            id="",
            function_id=function_id,
            block_id=_safe_block_id(block_ids, spec.block_index),
            sloc=spec.sloc,
            kind=spec.kind,
            condition=spec.condition,
            var_names=spec.var_names,
            true_block=_safe_block_id(block_ids, spec.true_block_index)
            if spec.true_block_index is not None
            else "",
            false_block=_safe_block_id(block_ids, spec.false_block_index)
            if spec.false_block_index is not None
            else "",
            is_negated=spec.is_negated,
            case_value=spec.case_value,
        )
        record.id = core_ids.branch_id(function_id, record.sloc, position)
        records.append(record)
    return records


def _dedupe_pending(pending: list[PendingRecord]) -> list[PendingRecord]:
    unique: list[PendingRecord] = []
    seen: set[tuple[Any, ...]] = set()
    for item in pending:
        record = item.record
        key = (
            record.sloc.file,
            record.sloc.line,
            record.sloc.col,
            record.operator,
            record.lhs,
            record.rhs,
            record.is_decl_init,
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _finalize_pending(
    pending: list[PendingRecord],
    id_factory: Any,
    block_ids: list[str],
    *,
    assign_ids: bool = True,
) -> list[Any]:
    ordered = sorted(
        pending,
        key=lambda item: (
            item.record.sloc.line,
            item.record.sloc.col,
            item.ordinal,
        ),
    )
    records: list[Any] = []
    for ordinal, item in enumerate(ordered):
        record = item.record
        record.block_id = _safe_block_id(block_ids, item.block_index)
        if assign_ids:
            record.id = id_factory(record.sloc, ordinal)
        records.append(record)
    return records


def _expand_block_ids(
    records: list[Any], pending: list[PendingRecord], block_ids: list[str]
) -> list[Any]:  # pragma: no cover - retained for API stability
    return records


def _implicit_return(
    cfg: Cfg, cursor: Any, function: FunctionRecord
) -> ReturnRecord | None:
    if not cfg.has_implicit_return:
        return None
    if not _returns_value(cursor, function):
        return None
    sloc = cfg.implicit_return_sloc
    return ReturnRecord(
        id="",
        function_id=function.id,
        block_id="",
        sloc=sloc,
        expr="",
        var_names=(),
        is_implicit=True,
    )


def _finalize_returns(
    function_id: str, records: list[ReturnRecord], block_ids: list[str]
) -> list[ReturnRecord]:
    exit_id = block_ids[-1] if block_ids else ""
    for record in records:
        if not record.id:
            record.block_id = exit_id
    ordered = sorted(
        records,
        key=lambda item: (item.sloc.line, item.sloc.col, 1 if item.is_implicit else 0),
    )
    for ordinal, record in enumerate(ordered):
        record.id = core_ids.return_id(function_id, record.sloc, ordinal)
    return ordered


def _returns_value(cursor: Any, function: FunctionRecord) -> bool:
    spelling = (getattr(function, "return_type", "") or "").strip()
    if spelling:
        return spelling != "void"
    try:
        return cursor.result_type.kind != TypeKind.VOID
    except Exception:  # pragma: no cover
        return True


def _build_edges(
    cfg: Cfg, function_id: str, block_ids: list[str]
) -> list[CfgEdgeRecord]:
    records: list[CfgEdgeRecord] = []
    seen: set[str] = set()
    ordered = sorted(
        cfg.edges,
        key=lambda edge: (
            edge.src,
            _EDGE_KIND_ORDER.get(edge.kind, 99),
            edge.dst,
            edge.label,
        ),
    )
    for edge in ordered:
        src = _safe_block_id(block_ids, edge.src)
        dst = _safe_block_id(block_ids, edge.dst)
        if not src or not dst:
            continue
        edge_id = core_ids.cfg_edge_id(function_id, src, dst, edge.kind)
        if edge_id in seen:
            continue
        seen.add(edge_id)
        records.append(
            CfgEdgeRecord(
                id=edge_id,
                function_id=function_id,
                src=src,
                dst=dst,
                kind=edge.kind,
                label=edge.label,
            )
        )
    return records


__all__ = ["AnalyzeOptions", "analyze_function"]
