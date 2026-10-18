"""Function-level CFG construction over the live Clang AST.

Blocks are created in AST walk order and carry the statements that belong to
them, plus a coarse terminator. Edges are emitted from the structured
statements (if/else, loops, switch, goto, return) and from short-circuit
evaluation of ``&&``, ``||`` and ``?:`` operators.

The builder is deliberately statement-granular: an expression is never split
inside a block, but a boolean condition containing short-circuit operators is
split into one decision block per operand so ``true``/``false`` edges reflect
the language semantics. Assignment-level data-flow facts are extracted by
:mod:`ca.analyze.facts` and mapped back onto these blocks via source offsets.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from clang.cindex import CursorKind, TypeKind

from ca.analyze.astutil import (
    STATEMENT_KINDS,
    SourceCache,
    depth_zero_tokens,
    call_callee_name,
    collect_var_names,
    cursor_is_macro_expansion,
    cursor_path,
    statement_is_synthetic,
    cursor_sloc,
    find_child_stmt,
    first_child,
    logical_operator,
    normalize_text,
    token_list,
    unwrap,
)
from ca.core.schema import BranchRecord, Sloc

NORETURN_CALLEES = frozenset(
    {
        "panic",
        "__panic",
        "abort",
        "exit",
        "_exit",
        "_Exit",
        "__assert_fail",
        "__assert",
        "__stack_chk_fail",
        "__builtin_trap",
        "__builtin_unreachable",
        "vAssertCalled",
        "assert_failed",
    }
)

_EDGE_KIND_ORDER = {
    "true": 0,
    "false": 1,
    "switch_case": 2,
    "switch_default": 3,
    "back": 4,
    "unconditional": 5,
}


@dataclass
class CfgBlock:
    index: int
    kind: str
    terminator: str = ""
    sloc: Sloc = field(default_factory=lambda: Sloc(""))
    stmts: list[tuple[Any, int]] = field(default_factory=list)


@dataclass(frozen=True)
class CfgEdge:
    src: int
    dst: int
    kind: str = "unconditional"
    label: str = ""


@dataclass
class PendingBranch:
    """A branch record whose block references are still builder indices."""

    kind: str
    sloc: Sloc
    block_index: int
    condition: str = ""
    var_names: tuple[str, ...] = ()
    true_block_index: int | None = None
    false_block_index: int | None = None
    is_negated: bool = False
    case_value: str = ""


@dataclass
class Cfg:
    blocks: list[CfgBlock] = field(default_factory=list)
    edges: list[CfgEdge] = field(default_factory=list)
    branches: list[PendingBranch] = field(default_factory=list)
    entry_index: int = 0
    exit_index: int = 0
    implicit_return_sloc: Sloc = field(default_factory=lambda: Sloc(""))
    has_implicit_return: bool = False

    def block_map(self) -> "BlockMap":
        return BlockMap(self)


@dataclass
class _Target:
    dst: int | None = None


@dataclass
class _LoopCtx:
    break_target: _Target
    continue_target: _Target | None
    switch_block: int | None = None


class CfgBudgetExceeded(RuntimeError):
    """Raised when a function's CFG exceeds the configured block budget."""


class CfgBuilder:
    def __init__(
        self,
        cache: SourceCache,
        *,
        max_condition_chars: int = 120,
        max_var_names: int = 16,
        max_blocks: int = 20000,
    ) -> None:
        self.cache = cache
        self.max_condition_chars = max_condition_chars
        self.max_var_names = max_var_names
        self.max_blocks = max_blocks
        self.blocks: list[CfgBlock] = []
        self._edges: list[CfgEdge] = []
        self._edge_keys: set[tuple[int, int, str, str]] = set()
        self._branch_specs: list[tuple[str, Sloc, int, str, tuple[str, ...], _Target | None, _Target | None, bool, str]] = []
        self._pending_edges: list[tuple[int, str, _Target, str]] = []
        self._pending_label_edges: list[tuple[int, str]] = []
        self._labels: dict[str, int] = {}
        self._ctx: list[_LoopCtx] = []
        self._seq = 0
        self._exit_target = _Target()
        self._suppress_branches = 0

    # -- public API ------------------------------------------------------
    def build(self, function_cursor: Any) -> Cfg:
        body = find_child_stmt(function_cursor)
        if body is None:
            cfg = Cfg()
            entry = self._new_block("entry", cursor_sloc(self.cache, function_cursor))
            cfg.blocks = self.blocks
            cfg.entry_index = entry
            cfg.exit_index = entry
            return cfg

        entry = self._new_block("entry", cursor_sloc(self.cache, body))
        cur = self._build_compound(body, entry)
        exit_block = self._new_block("exit", _end_sloc(self.cache, body))
        if cur is not None:
            self._edge(cur, exit_block, "unconditional")
        self._exit_target.dst = exit_block
        self._resolve_pending()
        implicit = self._finalize_terminators(exit_block)
        cfg = Cfg(
            blocks=self.blocks,
            edges=self._sorted_edges(),
            branches=self._resolve_branches(),
            entry_index=entry,
            exit_index=exit_block,
            implicit_return_sloc=_end_sloc(self.cache, body),
            has_implicit_return=implicit,
        )
        return cfg

    # -- block/edge primitives ------------------------------------------
    def _new_block(self, kind: str, sloc: Sloc | None) -> int:
        if len(self.blocks) >= self.max_blocks:
            raise CfgBudgetExceeded(
                f"CFG block budget {self.max_blocks} exceeded"
            )
        block = CfgBlock(index=len(self.blocks), kind=kind, sloc=sloc or Sloc(""))
        self.blocks.append(block)
        return block.index

    def _append(self, block_index: int, cursor: Any) -> int:
        self._seq += 1
        self.blocks[block_index].stmts.append((cursor, self._seq))
        return self._seq

    def _edge(self, src: int, dst: int, kind: str, label: str = "") -> None:
        key = (src, dst, kind, label)
        if key in self._edge_keys:
            return
        self._edge_keys.add(key)
        self._edges.append(CfgEdge(src=src, dst=dst, kind=kind, label=label))

    def _edge_to(self, src: int, kind: str, target: _Target, label: str = "") -> None:
        self._pending_edges.append((src, kind, target, label))

    def _sorted_edges(self) -> list[CfgEdge]:
        return sorted(
            self._edges,
            key=lambda edge: (
                edge.src,
                _EDGE_KIND_ORDER.get(edge.kind, 99),
                edge.dst,
                edge.label,
            ),
        )

    def _resolve_pending(self) -> None:
        for src, kind, target, label in self._pending_edges:
            if target.dst is not None:
                self._edge(src, target.dst, kind, label)
        for src, label in self._pending_label_edges:
            dst = self._labels.get(label)
            if dst is not None:
                self._edge(src, dst, "unconditional", label)
        self._pending_edges.clear()
        self._pending_label_edges.clear()

    def _finalize_terminators(self, exit_index: int) -> bool:
        outgoing: dict[int, list[CfgEdge]] = {}
        for edge in self._edges:
            outgoing.setdefault(edge.src, []).append(edge)
        implicit = False
        for block in self.blocks:
            if block.index == exit_index:
                block.terminator = "none"
                continue
            edges = outgoing.get(block.index, [])
            if any(edge.dst == exit_index for edge in edges):
                if block.terminator != "return":
                    implicit = True
            if block.terminator:
                continue
            if not edges:
                block.terminator = "none"
            elif any(edge.kind in ("true", "false") for edge in edges):
                block.terminator = "conditional"
            else:
                block.terminator = "unconditional"
        self._fill_missing_slocs()
        return implicit

    def _fill_missing_slocs(self) -> None:
        for index, block in enumerate(self.blocks):
            if block.sloc.is_valid():
                continue
            for previous in range(index - 1, -1, -1):
                if self.blocks[previous].sloc.is_valid():
                    block.sloc = self.blocks[previous].sloc
                    break

    def _resolve_branches(self) -> list[PendingBranch]:
        resolved: list[PendingBranch] = []
        for (
            kind,
            sloc,
            block_index,
            condition,
            var_names,
            true_target,
            false_target,
            is_negated,
            case_value,
        ) in self._branch_specs:
            resolved.append(
                PendingBranch(
                    kind=kind,
                    sloc=sloc,
                    block_index=block_index,
                    condition=condition,
                    var_names=var_names,
                    true_block_index=true_target.dst if true_target else None,
                    false_block_index=false_target.dst if false_target else None,
                    is_negated=is_negated,
                    case_value=case_value,
                )
            )
        return resolved

    def _record_branch(
        self,
        kind: str,
        condition_cursor: Any,
        block_index: int,
        *,
        true_target: _Target | None = None,
        false_target: _Target | None = None,
        is_negated: bool | None = None,
        case_value: str = "",
        sloc: Sloc | None = None,
        suppress: bool = False,
    ) -> None:
        if self._suppress_branches or (
            condition_cursor is not None
            and cursor_is_macro_expansion(condition_cursor)
        ):
            # Macro-expanded conditions mix header macro bodies with the usage
            # site; keep the CFG edges but do not persist a bogus condition.
            return
        if condition_cursor is None:
            condition = ""
            var_names = ()
            if is_negated is None:
                is_negated = False
            condition_sloc = sloc or Sloc("")
        else:
            condition = self.cache.text_for(condition_cursor, self.max_condition_chars)
            var_names = collect_var_names(condition_cursor, self.max_var_names)
            if is_negated is None:
                is_negated = _is_negated(condition_cursor)
            condition_sloc = sloc or cursor_sloc(self.cache, condition_cursor)
        self._branch_specs.append(
            (
                kind,
                condition_sloc,
                block_index,
                condition,
                var_names,
                true_target,
                false_target,
                is_negated,
                case_value,
            )
        )

    def _emit_bool_guarded(
        self, stmt: Any, cond: Any, true_target: _Target, false_target: _Target
    ) -> int:
        """``_emit_bool`` that suppresses branch records for macro statements."""

        macro = cursor_is_macro_expansion(stmt)
        if macro:
            self._suppress_branches += 1
        try:
            return self._emit_bool(cond, true_target, false_target)
        finally:
            if macro:
                self._suppress_branches -= 1

    # -- statements ------------------------------------------------------
    def _build_compound(self, compound: Any, cur: int | None) -> int | None:
        for stmt in compound.get_children():
            cur = self._build_stmt(stmt, cur)
        return cur

    def _build_stmt(self, stmt: Any, cur: int | None) -> int | None:
        kind = stmt.kind
        if kind in (
            CursorKind.IF_STMT,
            CursorKind.WHILE_STMT,
            CursorKind.DO_STMT,
            CursorKind.FOR_STMT,
            CursorKind.SWITCH_STMT,
        ) and (cursor_is_macro_expansion(stmt) or statement_is_synthetic(stmt)):
            # Control flow invented by a macro body (for example
            # ``configASSERT`` or ``list_for_each_entry``) is not a
            # source-level fact; keep its branches opaque but still build the
            # real source statements that were passed as macro arguments
            # (loop bodies are ordinary COMPOUND_STMTs in the source).
            for child in stmt.get_children():
                if child.kind in STATEMENT_KINDS:
                    cur = self._build_stmt(child, cur)
            return cur
        if cur is None and kind not in (
            CursorKind.LABEL_STMT,
        ):
            # Unreachable code: keep it in a detached block so facts still map.
            cur = self._new_block("normal", cursor_sloc(self.cache, stmt))
        if kind == CursorKind.COMPOUND_STMT:
            return self._build_compound(stmt, cur)
        if kind == CursorKind.IF_STMT:
            _entry, join = self._build_if(stmt, cur, "if")
            return join
        if kind == CursorKind.WHILE_STMT:
            return self._build_while(stmt, cur)
        if kind == CursorKind.DO_STMT:
            return self._build_do(stmt, cur)
        if kind == CursorKind.FOR_STMT:
            return self._build_for(stmt, cur)
        if kind == CursorKind.SWITCH_STMT:
            return self._build_switch(stmt, cur)
        if kind == CursorKind.RETURN_STMT:
            self._append(cur, stmt)
            self.blocks[cur].terminator = "return"
            self._edge_to(cur, "unconditional", self._exit_target)
            return None
        if kind == CursorKind.BREAK_STMT:
            target = self._ctx[-1].break_target if self._ctx else None
            if target is not None:
                self._edge_to(cur, "unconditional", target, "break")
            self.blocks[cur].terminator = "goto"
            return None
        if kind == CursorKind.CONTINUE_STMT:
            target = self._continue_target()
            if target is not None:
                self._edge_to(cur, "back", target, "continue")
            self.blocks[cur].terminator = "goto"
            return None
        if kind == CursorKind.GOTO_STMT:
            self._pending_label_edges.append((cur, _goto_label(stmt)))
            self.blocks[cur].terminator = "goto"
            return None
        if kind == CursorKind.LABEL_STMT:
            label_block = self._new_block("normal", cursor_sloc(self.cache, stmt))
            if cur is not None:
                self._edge(cur, label_block, "unconditional")
            if stmt.spelling:
                self._labels[stmt.spelling] = label_block
            result: int | None = label_block
            for child in stmt.get_children():
                result = self._build_stmt(child, result)
            return result
        if kind in (CursorKind.CASE_STMT, CursorKind.DEFAULT_STMT):
            ctx = self._ctx[-1] if self._ctx else None
            if ctx is not None and ctx.switch_block is not None:
                result, _case_index = self._handle_case(
                    stmt, ctx.switch_block, cur, kind == CursorKind.DEFAULT_STMT
                )
                return result
            if cur is not None:
                self._append(cur, stmt)
            return cur
        # Declarations, expression statements, null statements: straight-line.
        if kind != CursorKind.NULL_STMT:
            self._append(cur, stmt)
            if _is_noreturn_statement(self.cache, stmt):
                # panic()/abort()/__builtin_unreachable() end the path: no
                # fall-through edge, so resource analysis cannot report leaks
                # on code that never returns to the caller.
                self.blocks[cur].terminator = "none"
                return None
        return cur

    def _continue_target(self) -> _Target | None:
        for ctx in reversed(self._ctx):
            if ctx.continue_target is not None:
                return ctx.continue_target
        return None

    # -- structured statements ------------------------------------------
    def _build_if(
        self, stmt: Any, cur: int | None, branch_kind: str
    ) -> tuple[int, int]:
        """Build an if/else-if; returns ``(decision_entry, join_block)``."""

        children = list(stmt.get_children())
        if not children:
            block = self._new_block("normal", cursor_sloc(self.cache, stmt))
            return block, block
        cond = children[0]
        then_stmt = children[1] if len(children) > 1 else None
        else_stmt = children[2] if len(children) > 2 else None
        suppress = _stmt_is_macro(stmt)
        true_target, false_target = _Target(), _Target()
        entry = self._emit_bool_guarded(stmt, cond, true_target, false_target)
        if cur is not None:
            self._edge(cur, entry, "unconditional")
        then_entry = self._new_block(
            "normal",
            cursor_sloc(self.cache, then_stmt) if then_stmt is not None else None,
        )
        true_target.dst = then_entry
        then_exit = (
            self._build_stmt(then_stmt, then_entry)
            if then_stmt is not None
            else then_entry
        )
        else_exit: int | None = None
        if else_stmt is None:
            join_index = self._new_block("normal", None)
            false_target.dst = join_index
        elif else_stmt.kind == CursorKind.IF_STMT:
            nested_entry, nested_join = self._build_if(else_stmt, None, "else_if")
            false_target.dst = nested_entry
            else_exit = nested_join
            join_index = self._new_block("normal", _end_sloc(self.cache, stmt))
        else:
            else_entry = self._new_block(
                "normal", cursor_sloc(self.cache, else_stmt)
            )
            false_target.dst = else_entry
            else_exit = self._build_stmt(else_stmt, else_entry)
            join_index = self._new_block("normal", _end_sloc(self.cache, stmt))
        self._record_branch(
            branch_kind,
            cond,
            entry,
            true_target=true_target,
            false_target=false_target,
            suppress=suppress,
        )
        if then_exit is not None:
            self._edge(then_exit, join_index, "unconditional")
        if else_exit is not None:
            self._edge(else_exit, join_index, "unconditional")
        return entry, join_index

    def _build_while(self, stmt: Any, cur: int | None) -> int | None:
        children = list(stmt.get_children())
        if not children:
            return cur
        cond = children[0]
        body = children[1] if len(children) > 1 else None
        body_target, exit_target = _Target(), _Target()
        constant_true = _is_constant_true(cond)
        suppress = _stmt_is_macro(stmt)
        header = self._emit_bool_guarded(stmt, cond, body_target, exit_target)
        self.blocks[header].kind = "loop_header"
        if cur is not None:
            self._edge(cur, header, "unconditional")
        body_entry = self._new_block("normal", cursor_sloc(self.cache, body) if body is not None else None)
        body_target.dst = body_entry
        ctx = _LoopCtx(_Target(), _Target())
        self._ctx.append(ctx)
        body_exit = self._build_stmt(body, body_entry) if body is not None else body_entry
        self._ctx.pop()
        if body_exit is not None:
            self._edge(body_exit, header, "back")
        exit_block = self._new_block("normal", _end_sloc(self.cache, stmt))
        if not constant_true:
            exit_target.dst = exit_block
        ctx.break_target.dst = exit_block
        ctx.continue_target.dst = header
        self._record_branch(
            "while",
            cond,
            header,
            true_target=body_target,
            false_target=exit_target,
            suppress=suppress,
        )
        return exit_block

    def _build_do(self, stmt: Any, cur: int | None) -> int | None:
        children = list(stmt.get_children())
        if not children:
            return cur
        body = children[0]
        cond = children[-1]
        body_entry = self._new_block("normal", cursor_sloc(self.cache, body))
        if cur is not None:
            self._edge(cur, body_entry, "unconditional")
        ctx = _LoopCtx(_Target(), _Target())
        self._ctx.append(ctx)
        body_exit = self._build_stmt(body, body_entry)
        self._ctx.pop()
        true_target = _Target(body_entry)
        exit_target = _Target()
        constant_true = _is_constant_true(cond)
        suppress = _stmt_is_macro(stmt)
        header = self._emit_bool_guarded(stmt, cond, true_target, exit_target)
        self.blocks[header].kind = "loop_header"
        ctx.continue_target.dst = header
        if body_exit is not None:
            self._edge(body_exit, header, "unconditional")
        exit_block = self._new_block("normal", _end_sloc(self.cache, stmt))
        if not constant_true:
            exit_target.dst = exit_block
        ctx.break_target.dst = exit_block
        self._record_branch(
            "do_while",
            cond,
            header,
            true_target=true_target,
            false_target=exit_target,
            suppress=suppress,
        )
        return exit_block

    def _build_for(self, stmt: Any, cur: int | None) -> int | None:
        init_stmt, cond, inc_stmt, body = _classify_for(self.cache, stmt)
        pre = cur
        if init_stmt is not None:
            init_block = self._new_block("normal", cursor_sloc(self.cache, init_stmt))
            if pre is not None:
                self._edge(pre, init_block, "unconditional")
            self._append(init_block, init_stmt)
            pre = init_block
        body_target, exit_target = _Target(), _Target()
        suppress = _stmt_is_macro(stmt)
        constant_true = cond is not None and _is_constant_true(cond)
        header: int | None = None
        if cond is not None:
            header = self._emit_bool_guarded(stmt, cond, body_target, exit_target)
            self.blocks[header].kind = "loop_header"
            if pre is not None:
                self._edge(pre, header, "unconditional")
        body_entry = self._new_block("normal", cursor_sloc(self.cache, body) if body is not None else None)
        body_target.dst = body_entry
        if cond is None and pre is not None:
            self._edge(pre, body_entry, "unconditional")
        ctx = _LoopCtx(_Target(), _Target())
        self._ctx.append(ctx)
        body_exit = self._build_stmt(body, body_entry) if body is not None else body_entry
        self._ctx.pop()
        loop_top = header if header is not None else body_entry
        if inc_stmt is not None:
            inc_block = self._new_block("normal", cursor_sloc(self.cache, inc_stmt))
            if body_exit is not None:
                self._edge(body_exit, inc_block, "unconditional")
            self._append(inc_block, inc_stmt)
            self._edge(inc_block, loop_top, "back")
            ctx.continue_target.dst = inc_block
        else:
            if body_exit is not None:
                self._edge(body_exit, loop_top, "back")
            ctx.continue_target.dst = loop_top
        exit_block = self._new_block("normal", _end_sloc(self.cache, stmt))
        if not constant_true:
            exit_target.dst = exit_block
        ctx.break_target.dst = exit_block
        self._record_branch(
            "for" if cond is not None else "loop",
            cond,
            header if header is not None else body_entry,
            true_target=body_target,
            false_target=exit_target,
            sloc=cursor_sloc(self.cache, stmt),
            suppress=suppress,
        )
        return exit_block

    def _build_switch(self, stmt: Any, cur: int | None) -> int | None:
        children = list(stmt.get_children())
        if not children:
            return cur
        cond = children[0]
        body = children[1] if len(children) > 1 else None
        switch_block = self._new_block("switch", cursor_sloc(self.cache, cond))
        self.blocks[switch_block].terminator = "switch"
        self._append(switch_block, cond)
        if cur is not None:
            self._edge(cur, switch_block, "unconditional")
        break_target = _Target()
        self._ctx.append(_LoopCtx(break_target, None, switch_block))
        body_cur: int | None = None
        first_case: int | None = None
        if body is not None:
            for child in body.get_children():
                if child.kind == CursorKind.CASE_STMT:
                    body_cur, case_index = self._handle_case(child, switch_block, body_cur, False)
                    if first_case is None:
                        first_case = case_index
                elif child.kind == CursorKind.DEFAULT_STMT:
                    body_cur, case_index = self._handle_case(child, switch_block, body_cur, True)
                    if first_case is None:
                        first_case = case_index
                else:
                    if body_cur is None:
                        body_cur = self._new_block("normal", cursor_sloc(self.cache, child))
                        self._edge(switch_block, body_cur, "unconditional", "dead")
                    body_cur = self._build_stmt(child, body_cur)
        self._ctx.pop()
        exit_block = self._new_block("normal", _end_sloc(self.cache, stmt))
        if body_cur is not None:
            self._edge(body_cur, exit_block, "unconditional")
        break_target.dst = exit_block
        self._record_branch(
            "switch",
            cond,
            switch_block,
            true_target=_Target(first_case if first_case is not None else exit_block),
            false_target=_Target(exit_block),
            suppress=_stmt_is_macro(stmt),
        )
        return exit_block

    def _handle_case(
        self,
        case_cursor: Any,
        switch_block: int,
        cur: int | None,
        is_default: bool,
    ) -> tuple[int | None, int]:
        children = list(case_cursor.get_children())
        value_cursor = None if is_default else (children[0] if children else None)
        rest = children if is_default else children[1:]
        label = normalize_text(self.cache.text_for(value_cursor)) if value_cursor is not None else ""
        case_block = self._new_block("normal", cursor_sloc(self.cache, case_cursor))
        self._edge(
            switch_block,
            case_block,
            "switch_default" if is_default else "switch_case",
            label,
        )
        if value_cursor is not None:
            self._append(switch_block, value_cursor)
        if cur is not None:
            self._edge(cur, case_block, "unconditional")
        self._record_branch(
            "case" if value_cursor is not None else "case",
            value_cursor,
            switch_block,
            true_target=_Target(case_block),
            case_value=label,
            sloc=cursor_sloc(self.cache, case_cursor),
            suppress=_stmt_is_macro(case_cursor),
        )
        body_cur: int | None = case_block
        for child in rest:
            body_cur = self._build_stmt(child, body_cur)
        return body_cur, case_block

    # -- boolean condition splitting ------------------------------------
    def _emit_bool(self, expr: Any, true_target: _Target, false_target: _Target) -> int:
        expr = unwrap(expr)
        operator = logical_operator(expr)
        if operator:
            children = list(expr.get_children())
            if len(children) == 2:
                left, right = children
                mid = _Target()
                if operator == "&&":
                    entry = self._emit_bool(left, mid, false_target)
                    mid.dst = self._emit_bool(right, true_target, false_target)
                    kind = "logical_and"
                else:
                    entry = self._emit_bool(left, true_target, mid)
                    mid.dst = self._emit_bool(right, true_target, false_target)
                    kind = "logical_or"
                self._record_branch(
                    kind,
                    left,
                    entry,
                    true_target=mid if operator == "&&" else true_target,
                    false_target=false_target if operator == "&&" else mid,
                )
                return entry
        if expr.kind == CursorKind.CONDITIONAL_OPERATOR:
            children = list(expr.get_children())
            if len(children) == 3:
                condition, when_true, when_false = children
                true_arm = self._emit_bool(when_true, true_target, false_target)
                false_arm = self._emit_bool(when_false, true_target, false_target)
                entry = self._emit_bool(
                    condition, _Target(true_arm), _Target(false_arm)
                )
                self._record_branch(
                    "ternary",
                    condition,
                    entry,
                    true_target=_Target(true_arm),
                    false_target=_Target(false_arm),
                )
                return entry
        block = self._new_block("branch", cursor_sloc(self.cache, expr))
        self._append(block, expr)
        block_data = self.blocks[block]
        block_data.terminator = "conditional"
        self._edge_to(block, "true", true_target)
        self._edge_to(block, "false", false_target)
        return block


def _goto_label(stmt: Any) -> str:
    for child in stmt.get_children():
        if child.spelling:
            return child.spelling
    if stmt.spelling:
        return stmt.spelling
    tokens = token_list(stmt)
    return tokens[-1].spelling if tokens else ""


def _is_noreturn_statement(cache: SourceCache, stmt: Any) -> bool:
    expr = unwrap(stmt)
    if expr is None or expr.kind != CursorKind.CALL_EXPR:
        return False
    return call_callee_name(expr, cache) in NORETURN_CALLEES


def _stmt_is_macro(stmt: Any) -> bool:
    return cursor_is_macro_expansion(stmt) or statement_is_synthetic(stmt)


def _is_constant_true(expr: Any) -> bool:
    """Detect ``while (1)`` / ``for (; 1 ;)`` style conditions.

    Their false edge is unreachable at run time, so emitting it would create
    bogus exit paths for resource analysis (``acquire(); while (1) {...}``).
    """

    expr = unwrap(expr)
    if expr is None:
        return False
    if expr.kind == CursorKind.INTEGER_LITERAL:
        tokens = token_list(expr)
        spelling = (tokens[-1].spelling if tokens else expr.spelling) or ""
        for suffix in ("ull", "llu", "ul", "lu", "u", "l"):
            if spelling.lower().endswith(suffix):
                spelling = spelling[: -len(suffix)]
                break
        try:
            return int(spelling, 0) != 0
        except ValueError:
            return False
    tokens = depth_zero_tokens(token_list(expr))
    return len(tokens) == 1 and tokens[0].spelling in ("true", "1")


def _is_negated(expr: Any) -> bool:
    from ca.analyze.astutil import depth_zero_tokens, extent_span

    expr = unwrap(expr)
    if expr.kind != CursorKind.UNARY_OPERATOR:
        return False
    if extent_span(expr) > 4096:
        return False
    tokens = token_list(expr, 16)
    if len(tokens) >= 16:
        return False
    return any(token.spelling == "!" for token in depth_zero_tokens(tokens))


def _end_sloc(cache: SourceCache, cursor: Any) -> Sloc:
    try:
        path = cursor_path(cursor)
        extent = cursor.extent
        return Sloc(
            file=cache.repo_path(path),
            line=int(extent.end.line or 0),
            col=int(extent.end.column or 0),
            end_line=int(extent.end.line or 0),
            end_col=int(extent.end.column or 0),
        )
    except Exception:  # pragma: no cover - defensive
        return Sloc("")


def _classify_for(
    cache: SourceCache, stmt: Any
) -> tuple[Any | None, Any | None, Any | None, Any | None]:
    """Split a FOR_STMT into ``(init, condition, increment, body)``.

    Token positions are used because libclang omits absent clauses and the
    remaining children (for example ``for (;;)`` vs ``for (i = 0;; i++)``) are
    indistinguishable by cursor kind alone.
    """

    children = list(stmt.get_children())
    if not children:
        return None, None, None, None
    tokens = token_list(stmt)
    open_index = None
    for index, token in enumerate(tokens):
        if token.spelling == "(":
            open_index = index
            break
    semis: list[int] = []
    close_index = None
    if open_index is not None:
        depth = 0
        for index in range(open_index, len(tokens)):
            spelling = tokens[index].spelling
            if spelling == "(":
                depth += 1
            elif spelling == ")":
                depth -= 1
                if depth == 0:
                    close_index = index
                    break
            elif spelling == ";" and depth == 1:
                semis.append(index)
    if open_index is None or close_index is None or len(semis) < 2:
        body = children[-1]
        exprs = children[:-1]
        init_part = exprs[0] if exprs else None
        cond_part = exprs[1] if len(exprs) > 1 else None
        inc_part = exprs[2] if len(exprs) > 2 else None
        return init_part, cond_part, inc_part, body
    bounds = [
        tokens[semis[0]].extent.start.offset,
        tokens[semis[1]].extent.start.offset,
        tokens[close_index].extent.start.offset,
    ]
    init_part = cond_part = inc_part = body = None
    for child in children:
        child_tokens = token_list(child)
        if child_tokens:
            offset = child_tokens[0].extent.start.offset
        else:
            offset = child.extent.start.offset
        if offset < bounds[0]:
            init_part = child
        elif offset < bounds[1]:
            cond_part = child
        elif offset < bounds[2]:
            inc_part = child
        else:
            body = child
    return init_part, cond_part, inc_part, body


# --------------------------------------------------------------------------- #
# Fact-to-block mapping
# --------------------------------------------------------------------------- #


class BlockMap:
    """Map a cursor's source offset back to the CFG block that contains it."""

    def __init__(self, cfg: Cfg) -> None:
        self._entries: dict[str, list[tuple[int, int, int, int]]] = {}
        for block in cfg.blocks:
            for cursor, seq in block.stmts:
                path = cursor_path(cursor)
                if not path:
                    continue
                try:
                    extent = cursor.extent
                    start, end = extent.start.offset, extent.end.offset
                except Exception:  # pragma: no cover
                    continue
                self._entries.setdefault(path, []).append(
                    (start, end, block.index, seq)
                )
        for entries in self._entries.values():
            entries.sort(key=lambda item: (item[0], -(item[1] - item[0]), item[2], item[3]))
        self._blocks = cfg.blocks
        self._entry_index = cfg.entry_index

    def locate(self, cursor: Any) -> tuple[int, int]:
        path = cursor_path(cursor)
        if path and cursor is not None:
            try:
                offset = cursor.extent.start.offset
            except Exception:  # pragma: no cover
                offset = -1
            if offset >= 0:
                for start, end, block_index, seq in self._entries.get(path, ()):
                    if start <= offset < end:
                        return block_index, seq
        # Fallback: nearest block at or before the cursor's source position.
        sloc = cursor_sloc(_NullCache(), cursor)
        best: tuple[int, int, int] | None = None
        for block in self._blocks:
            line, col = block.sloc.line, block.sloc.col
            if block.index == self._entry_index:
                continue
            if (line, col) <= (sloc.line, sloc.col) or sloc.line == 0:
                key = (line, col, block.index)
                if best is None or key > best:
                    best = key
        if best is not None:
            return best[2], -1
        return self._entry_index, -1


class _NullCache:
    """Sloc helper for :class:`BlockMap` fallback without a SourceCache."""

    def repo_path(self, path: str) -> str:
        return path


__all__ = [
    "BlockMap",
    "Cfg",
    "CfgBlock",
    "CfgBudgetExceeded",
    "CfgBuilder",
    "CfgEdge",
    "PendingBranch",
]
