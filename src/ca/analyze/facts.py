"""Extract statement-level data-flow facts from a function body.

The extractor walks the AST once and records assignments (including
declaration initializers, compound assignments and ``++``/``--``), returns and
resource operations. It does not decide control flow; :mod:`ca.analyze.cfg`
owns the CFG and :class:`ca.analyze.cfg.BlockMap` maps every fact back to the
block that contains it.

Known limits: expressions inside macro expansions, comma-expression roots and
initializers hidden behind extra casts are approximated by their source text;
facts are attached to the enclosing basic block, not to an instruction index.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from clang.cindex import CursorKind

from ca.analyze.astutil import (
    STATEMENT_KINDS,
    SourceCache,
    assignment_split,
    call_arguments,
    call_callee_name,
    collect_var_names,
    cursor_is_macro_expansion,
    cursor_sloc,
    decl_init_split,
    incdec_operator,
    safe_text_for,
    statement_is_synthetic,
    unwrap,
)
from ca.analyze.cfg import BlockMap
from ca.analyze.resources import ResourceConfig
from ca.core.schema import AssignmentRecord, ResourceOpRecord, ReturnRecord


@dataclass
class PendingRecord:
    """A record plus the CFG position it should be attached to."""

    record: Any
    block_index: int
    seq: int
    ordinal: int
    meta: dict[str, Any] = field(default_factory=dict)


class FactExtractor:
    def __init__(
        self,
        cache: SourceCache,
        config: ResourceConfig,
        block_map: BlockMap,
        *,
        function_id: str,
        max_expr_chars: int = 160,
        max_assignment_chars: int = 300,
        max_var_names: int = 16,
    ) -> None:
        self.cache = cache
        self.config = config
        self.block_map = block_map
        self.function_id = function_id
        self.max_expr_chars = max_expr_chars
        self.max_assignment_chars = max_assignment_chars
        self.max_var_names = max_var_names
        self.assignments: list[PendingRecord] = []
        self.returns: list[PendingRecord] = []
        self.resource_ops: list[PendingRecord] = []
        self._ordinal = 0
        self.local_vars: set[str] = set()
        self.param_vars: set[str] = set()
        self.escaped_vars: set[str] = set()
        self.borrowed_vars: set[str] = set()

    # -- entry -----------------------------------------------------------
    def run(self, body: Any) -> None:
        if body is None:
            return
        self._collect_locals(body)
        for child in body.get_children():
            self._visit_stmt(child)

    def _collect_locals(self, node: Any) -> None:
        """Names declared inside the function body (parameters excluded)."""

        for child in node.get_children():
            if child.kind == CursorKind.VAR_DECL and child.spelling:
                self.local_vars.add(child.spelling)
            elif child.kind == CursorKind.PARM_DECL and child.spelling:
                self.param_vars.add(child.spelling)
            self._collect_locals(child)

    # -- positions -------------------------------------------------------
    def _locate(self, cursor: Any) -> tuple[int, int]:
        return self.block_map.locate(cursor)

    def _add(self, bucket: list[PendingRecord], record: Any, cursor: Any) -> PendingRecord:
        block_index, seq = self._locate(cursor)
        self._ordinal += 1
        pending = PendingRecord(record, block_index, seq, self._ordinal)
        bucket.append(pending)
        return pending

    # -- statements ------------------------------------------------------
    def _visit_stmt(self, cursor: Any) -> None:
        if cursor_is_macro_expansion(cursor) or statement_is_synthetic(cursor):
            # Macro-invented control flow is opaque, but real source statements
            # passed as macro arguments (loop bodies) still carry facts.
            for child in cursor.get_children():
                if child.kind in STATEMENT_KINDS or child.kind in _EXTRA_STMT_KINDS:
                    self._visit_stmt(child)
            return
        kind = cursor.kind
        if kind == CursorKind.COMPOUND_STMT:
            for child in cursor.get_children():
                self._visit_stmt(child)
            return
        if kind == CursorKind.DECL_STMT:
            for child in cursor.get_children():
                if child.kind == CursorKind.VAR_DECL:
                    self._handle_decl(child)
                else:
                    self._visit_expr(child)
            return
        if kind == CursorKind.RETURN_STMT:
            self._handle_return(cursor)
            return
        if kind in (
            CursorKind.IF_STMT,
            CursorKind.WHILE_STMT,
            CursorKind.DO_STMT,
            CursorKind.FOR_STMT,
            CursorKind.SWITCH_STMT,
            CursorKind.CASE_STMT,
            CursorKind.DEFAULT_STMT,
            CursorKind.LABEL_STMT,
        ):
            for child in cursor.get_children():
                if child.kind in STATEMENT_KINDS or child.kind in _EXTRA_STMT_KINDS:
                    self._visit_stmt(child)
                else:
                    self._visit_expr(child)
            return
        if kind in (CursorKind.BREAK_STMT, CursorKind.CONTINUE_STMT, CursorKind.GOTO_STMT):
            return
        if kind in _EXTRA_STMT_KINDS:
            for child in cursor.get_children():
                self._visit_stmt(child)
            return
        self._visit_expr(cursor)

    # -- declarations ----------------------------------------------------
    def _handle_decl(self, var_cursor: Any) -> None:
        split = decl_init_split(
            self.cache, var_cursor, self.max_assignment_chars
        )
        if split is None:
            return
        children = list(var_cursor.get_children())
        init_cursor = unwrap(children[-1]) if children else None
        if init_cursor is None:
            return
        rhs_text = safe_text_for(self.cache, init_cursor, self.max_assignment_chars)
        lhs_text = var_cursor.spelling
        var_names = self._merge_names(
            (var_cursor.spelling,) + collect_var_names(init_cursor, self.max_var_names)
        )
        record = AssignmentRecord(
            id="",
            function_id=self.function_id,
            block_id="",
            sloc=cursor_sloc(self.cache, var_cursor),
            lhs=lhs_text,
            rhs=rhs_text,
            var_names=var_names,
            is_decl_init=True,
            operator="=",
            type_spelling=var_cursor.type.spelling,
        )
        self._add(self.assignments, record, var_cursor)
        if cursor_is_macro_expansion(init_cursor):
            # ``p = allocate_mm()`` expands to a known allocator call: record
            # the acquire at the usage site even though the body is macro code.
            self._record_macro_acquire(init_cursor, var_cursor.spelling)
        else:
            self._visit_expr(
                init_cursor,
                skip_acquire_target=var_cursor.spelling,
                result_var=var_cursor.spelling,
            )

    # -- returns ---------------------------------------------------------
    def _handle_return(self, cursor: Any) -> None:
        children = list(cursor.get_children())
        expr_cursor = unwrap(children[0]) if children else None
        expr = (
            ""
            if expr_cursor is None
            else safe_text_for(self.cache, expr_cursor, self.max_expr_chars)
        )
        var_names = collect_var_names(cursor, self.max_var_names)
        record = ReturnRecord(
            id="",
            function_id=self.function_id,
            block_id="",
            sloc=cursor_sloc(self.cache, cursor),
            expr=expr,
            var_names=var_names,
            is_implicit=False,
        )
        self._add(self.returns, record, cursor)
        if all(not cursor_is_macro_expansion(child) for child in children):
            for child in children:
                self._visit_expr(child)

    # -- expressions -----------------------------------------------------
    def _visit_expr(
        self,
        cursor: Any,
        skip_acquire_target: str = "",
        result_var: str = "",
    ) -> None:
        cursor = unwrap(cursor)
        if cursor is None:
            return
        macro = cursor_is_macro_expansion(cursor)
        kind = cursor.kind
        if kind == CursorKind.CALL_EXPR:
            self._handle_call(
                cursor,
                target_override=skip_acquire_target,
                result_var=result_var,
            )
            arguments = call_arguments(cursor)
            api_name = call_callee_name(cursor, self.cache)
            if api_name in TRANSFER_CALLEES:
                for arg in arguments:
                    self.escaped_vars.update(
                        collect_var_names(arg, self.max_var_names)
                    )
                if macro:
                    # Macro-expanded transfer calls (RCU_INIT_POINTER, ...) can
                    # hide their arguments from get_children(); collect names
                    # from the whole expanded call instead.
                    self.escaped_vars.update(
                        collect_var_names(cursor, self.max_var_names)
                    )
            if not macro:
                for arg in arguments:
                    self._visit_expr(arg)
            return
        if macro:
            # Macro-body expressions are not source facts: their extents mix
            # header macro definitions with the usage site.
            return
        if kind in (
            CursorKind.BINARY_OPERATOR,
            CursorKind.COMPOUND_ASSIGNMENT_OPERATOR,
        ):
            split = assignment_split(
                self.cache, cursor, self.max_assignment_chars
            )
            if split is not None:
                self._handle_assignment(cursor, split)
                return
            for child in cursor.get_children():
                self._visit_expr(child)
            return
        if kind == CursorKind.UNARY_OPERATOR:
            operator = incdec_operator(cursor)
            children = list(cursor.get_children())
            if operator and children:
                operand = unwrap(children[0])
                text = safe_text_for(self.cache, operand, self.max_expr_chars)
                record = AssignmentRecord(
                    id="",
                    function_id=self.function_id,
                    block_id="",
                    sloc=cursor_sloc(self.cache, cursor),
                    lhs=text,
                    rhs="",
                    var_names=collect_var_names(operand, self.max_var_names),
                    is_decl_init=False,
                    operator=operator,
                    type_spelling=operand.type.spelling,
                )
                self._add(self.assignments, record, cursor)
                self._visit_expr(operand)
                return
            for child in children:
                self._visit_expr(child)
            return
        if kind == CursorKind.CONDITIONAL_OPERATOR:
            for child in cursor.get_children():
                self._visit_expr(child)
            return
        # Any other expression: recurse so nested calls/assignments are found.
        for child in cursor.get_children():
            self._visit_expr(child)

    def _handle_assignment(self, cursor: Any, split: tuple[str, str, str]) -> None:
        operator, lhs_text, rhs_text = split
        children = list(cursor.get_children())
        lhs_cursor = unwrap(children[0]) if children else None
        rhs_cursor = unwrap(children[-1]) if len(children) >= 2 else None
        var_names = self._merge_names(
            collect_var_names(cursor, self.max_var_names)
        )
        record = AssignmentRecord(
            id="",
            function_id=self.function_id,
            block_id="",
            sloc=cursor_sloc(self.cache, cursor),
            lhs=lhs_text,
            rhs=rhs_text,
            var_names=var_names,
            is_decl_init=False,
            operator=operator,
            type_spelling=lhs_cursor.type.spelling if lhs_cursor is not None else "",
        )
        self._add(self.assignments, record, cursor)
        target_text = (
            self._simple_target(lhs_cursor)
            or safe_text_for(self.cache, lhs_cursor, self.max_expr_chars)
            if lhs_cursor is not None
            else lhs_text
        )
        if lhs_cursor is not None and _rhs_is_borrowed(rhs_cursor):
            target_var = self._simple_target(lhs_cursor)
            if target_var:
                self.borrowed_vars.add(target_var.lstrip("&"))
        if rhs_cursor is not None:
            if cursor_is_macro_expansion(rhs_cursor):
                self._record_macro_acquire(rhs_cursor, target_text)
            else:
                self._visit_expr(
                    rhs_cursor,
                    skip_acquire_target=target_text,
                    result_var=target_text,
                )
        if lhs_cursor is not None:
            self._visit_expr(lhs_cursor)

    def _record_macro_acquire(self, cursor: Any, target: str) -> None:
        """Record an acquire call whose AST node comes from a macro expansion."""

        expr = unwrap(cursor)
        if expr is None or expr.kind != CursorKind.CALL_EXPR:
            return
        api_name = call_callee_name(expr, self.cache)
        entry = self.config.match(api_name)
        if entry is None or entry.op not in ("acquire", "copy"):
            return
        if entry.target == "none":
            return
        record = ResourceOpRecord(
            id="",
            function_id=self.function_id,
            block_id="",
            sloc=cursor_sloc(self.cache, cursor),
            op=entry.op,
            resource_kind=entry.resource_kind,
            api_name=api_name,
            target=target or "",
            target_vars=(target.strip(),) if target and target.strip() else (),
            call_id="",
            matcher=entry.matcher,
        )
        self._add(self.resource_ops, record, cursor)

    @staticmethod
    def _simple_target(cursor: Any) -> str:
        """Return ``p`` / ``&lk`` for cast/paren-wrapped simple targets."""

        current = unwrap(cursor)
        if current is None:
            return ""
        if current.kind == CursorKind.DECL_REF_EXPR and current.spelling:
            return current.spelling
        if current.kind == CursorKind.UNARY_OPERATOR:
            children = list(current.get_children())
            if children and any(
                token.spelling == "&" for token in current.get_tokens()
            ):
                inner = unwrap(children[0])
                if inner is not None and inner.kind == CursorKind.DECL_REF_EXPR:
                    return "&" + inner.spelling
        return ""

    def _handle_call(
        self,
        call_cursor: Any,
        target_override: str = "",
        result_var: str = "",
    ) -> None:
        api_name = call_callee_name(call_cursor, self.cache)
        entry = self.config.match(api_name)
        if entry is None:
            return
        args = call_arguments(call_cursor)
        target = ""
        target_vars: tuple[str, ...] = ()
        if entry.target == "none":
            target = ""
            target_vars = ()
        elif entry.target == "return":
            if target_override:
                target = target_override
                target_vars = (target.strip(),) if target.strip() else ()
        else:
            if entry.target.startswith("arg") and entry.target[3:].isdigit():
                index = int(entry.target[3:])
            else:
                index = 0
            if len(args) > index:
                target_cursor = args[index]
                simple = self._simple_target(target_cursor)
                if simple:
                    target = simple
                    target_vars = (simple.lstrip("&"),)
                else:
                    target = safe_text_for(
                        self.cache, target_cursor, self.max_expr_chars
                    )
                    target_vars = collect_var_names(
                        target_cursor, self.max_var_names
                    )
        record = ResourceOpRecord(
            id="",
            function_id=self.function_id,
            block_id="",
            sloc=cursor_sloc(self.cache, call_cursor),
            op=entry.op,
            resource_kind=entry.resource_kind,
            api_name=api_name,
            target=target,
            target_vars=target_vars,
            call_id="",
            matcher=entry.matcher,
        )
        pending = self._add(self.resource_ops, record, call_cursor)
        if (
            result_var
            and entry.target not in ("return", "none")
            and entry.op in ("acquire", "copy")
        ):
            # Kernel APIs that report failure through their return value
            # (request_irq, pci_enable_device, ...) usually put the handle in
            # an out-parameter; remember the status variable for path pruning.
            pending.meta["result_var"] = result_var

    # -- helpers ---------------------------------------------------------
    def _merge_names(self, names: tuple[str, ...]) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for name in names:
            if name and name not in seen:
                seen[name] = None
            if len(seen) >= self.max_var_names:
                break
        return tuple(seen)


TRANSFER_CALLEES = frozenset(
    {
        # container/list registration: the callee keeps the object
        "list_add",
        "list_add_tail",
        "list_add_rcu",
        "list_add_tail_rcu",
        "hlist_add_head",
        "hlist_add_head_rcu",
        # driver-model private data / xarray stores
        "dev_set_drvdata",
        "platform_set_drvdata",
        "pci_set_drvdata",
        "usb_set_intfdata",
        "xa_store",
        "idr_alloc",
        "idr_replace",
        "radix_tree_insert",
        "netif_napi_add",
        "register_netdevice",
        # installing into a table or handing to an async worker
        "fd_install",
        "stop_one_cpu_nowait",
        "stop_cpu",
        "call_rcu",
        "call_srcu",
        "queue_work",
        "queue_work_on",
        "schedule_work",
        "queue_delayed_work",
        "schedule_delayed_work",
        "irq_work_queue",
        "task_work_add",
        "RCU_INIT_POINTER",
        "rcu_assign_pointer",
        "rcu_assign_pointer_protected",
        # async I/O ownership: the bio is completed by the block layer
        "submit_bio",
        "submit_bio_wait",
        "submit_bio_noacct",
        "btrfs_submit_bio",
        "btrfs_submit_metadata_bio",
    }
)


def _rhs_is_borrowed(rhs_cursor: Any) -> bool:
    """True when the RHS loads a pointer out of another object.

    ``file = tmp->vm_file`` / ``p = arr[i]`` create borrowed references whose
    lifetime belongs to the containing structure, so per-function leak reports
    for a later ``get_file(file)`` are not actionable.
    """

    current = unwrap(rhs_cursor)
    if current is None:
        return False
    if current.kind in (
        CursorKind.MEMBER_REF_EXPR,
        CursorKind.ARRAY_SUBSCRIPT_EXPR,
    ):
        return True
    if current.kind == CursorKind.UNARY_OPERATOR:
        return any(
            token.spelling == "*" for token in current.get_tokens()
        )
    return False

_EXTRA_STMT_KINDS = frozenset(
    {
        CursorKind.CXX_TRY_STMT,
        CursorKind.CXX_CATCH_STMT,
        CursorKind.CXX_FOR_RANGE_STMT,
    }
)


def resource_target_key(target: str) -> str:
    """Stable comparison key for a resource target expression."""

    return "".join(target.split())


__all__ = ["FactExtractor", "PendingRecord", "resource_target_key"]
