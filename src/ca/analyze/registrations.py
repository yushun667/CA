"""Cross-TU callback / function-pointer registration facts and resolution.

The intra-TU resolver in :mod:`ca.analyze.callbacks` only sees one translation
unit. Real OS code (xv6 ``devsw``, OpenHarmony listener/ops tables, kernel
``file_operations``-style tables) registers callbacks in one TU and invokes
them from another. This module extracts *registration facts* per TU and
resolves indirect call sites against facts collected from all TUs.

Pipeline:

1. :func:`extract_registrations` -- per TU, walk global initializers and the
   function bodies of main-file functions, producing
   :class:`RegistrationFact` rows. Only registrations into *global* storage are
   recorded: assignments to local test structs are intentionally ignored, which
   removes the "same field name, different instance" false-positive family.
2. :func:`extract_indirect_call_sites` -- per TU, produce neutral indirect call
   site rows (callee path, function-pointer signature, caller identity).
3. :func:`resolve_cross_tu_targets` -- match call sites against facts:
   qualifier-aware (base symbol + field chain must match; a dynamic array index
   only matches constant-index registrations of the same base), signature
   filtered, with auditable evidence (candidate target set, no confidence). Also handles callbacks passed as
   arguments across TUs (``param:<callee>:<index>``) and function pointers
   returned by getters (``return:<getter>``).
4. :func:`analyze_cross_tu` -- convenience orchestration over a list of parsed
   units; returns facts, call sites, ``CallTargetRecord`` candidates and
   ``(registration_id, call_target_id)`` links for storage.

The record fields deliberately mirror the storage contract
(``callback_registrations`` table) so a store can insert
``dataclasses.asdict(fact)`` without a conversion layer. No core contract is
changed: candidate edges are ordinary :class:`ca.core.schema.CallTargetRecord`
rows with ``kind="indirect_candidate"``.
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from clang.cindex import CursorKind

from ca.analyze.astutil import (
    SourceCache,
    assignment_split,
    cursor_sloc,
    find_child_stmt,
    normalize_text,
    token_list,
    unwrap,
)
from ca.analyze.callbacks import (
    _FuncRef,
    _Signature,
    _definition_cursor,
    _function_ref_from_expr,
    _is_function_pointer_storage,
    _key,
    _parse_calls_by_position,
    _signature_from_type,
)
from ca.core import ids as core_ids
from ca.core.schema import CallTargetRecord, FunctionRecord, Sloc

_FUNCTION_KINDS = frozenset(
    {
        CursorKind.FUNCTION_DECL,
        CursorKind.FUNCTION_TEMPLATE,
        CursorKind.CXX_METHOD,
        CursorKind.CONSTRUCTOR,
        CursorKind.DESTRUCTOR,
        CursorKind.CONVERSION_FUNCTION,
    }
)
_ARRAY_TYPE_KINDS = tuple(
    kind
    for kind in (
        getattr(CursorKind, "CONSTANTARRAY", None),
    )
    if kind is not None
)
_REGISTRATION_CALL_PATTERN = re.compile(
    r"(register|set_|_set$|install|hook|bind|attach|subscribe|unsubscribe|"
    r"add_.*callback|callback.*add|register_callback|set_callback)",
    re.IGNORECASE,
)
_MAX_FACTS = 20000
_MAX_SITES = 20000
_MAX_CANDIDATES = 16
_MAX_TARGETS = 256

# Registration kinds accepted by the storage contract.
KIND_GLOBAL_ASSIGN = "global_assign"
KIND_STRUCT_FIELD = "struct_field"
KIND_ARRAY_ELEMENT = "array_element"
KIND_DESIGNATED = "designated_initializer"
KIND_REGISTRATION_CALL = "registration_call"
KIND_ARGUMENT_PASS = "argument_pass"
KIND_RETURN_POINTER = "return_pointer"


@dataclass(frozen=True)
class RegistrationFact:
    """One function reference stored into a global callback location.

    Field names match the store's ``callback_registrations`` columns; ``id`` is
    the stable id the store would compute, so link records can reference it
    before/after persistence.
    """

    object_path: str
    target_function_name: str
    target_function_id: str = ""
    target_usr: str = ""
    object_field: str = ""
    registration_kind: str = KIND_GLOBAL_ASSIGN
    file: str = ""
    line: int = 0
    col: int = 0
    end_line: int = 0
    end_col: int = 0
    tu_id: str = ""
    evidence: list[str] = field(default_factory=list)
    module: str = ""
    directory: str = ""
    registrar_function_id: str = ""
    id: str = ""


@dataclass(frozen=True)
class IndirectCallSite:
    """A neutral indirect call site used by the cross-TU resolution pass."""

    call_id: str
    caller_id: str
    caller_name: str
    caller_usr: str
    callee_text: str
    callee_kind: str  # path | param | call
    signature: _Signature
    file: str = ""
    line: int = 0
    col: int = 0
    param_index: int = -1
    getter_usr: str = ""
    getter_name: str = ""


@dataclass
class CrossTuResult:
    registrations: list[RegistrationFact] = field(default_factory=list)
    call_sites: list[IndirectCallSite] = field(default_factory=list)
    call_targets: list[CallTargetRecord] = field(default_factory=list)
    links: list[tuple[str, str]] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Access-path parsing (qualifier aware)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _AccessPath:
    base: str
    qualifiers: tuple[str, ...]
    fields: tuple[str, ...]
    text: str

    def canonical(self) -> str:
        parts = [self.base]
        for qualifier in self.qualifiers:
            parts.append(f"[{_key(qualifier)}]")
        for field_name in self.fields:
            parts.append(f".{field_name}")
        return "".join(parts)


def parse_access_path(text: str) -> _AccessPath:
    """Split ``devsw[f->major].read`` into base / qualifiers / fields."""

    compact = _key(text)
    # Strip parentheses and leading dereference/address-of operators.
    while compact.startswith("(") and compact.endswith(")"):
        compact = compact[1:-1]
    while compact and compact[0] in "*&":
        compact = compact[1:]
    if compact.startswith("(") and ")" in compact:
        compact = compact[1 : compact.index(")")] + compact[compact.index(")") + 1 :]
    base = ""
    index = 0
    while index < len(compact) and (compact[index].isalnum() or compact[index] == "_"):
        base += compact[index]
        index += 1
    qualifiers: list[str] = []
    fields: list[str] = []
    while index < len(compact):
        char = compact[index]
        if compact.startswith("->", index):
            index += 2
            name, index = _read_identifier(compact, index)
            if name:
                fields.append(name)
        elif char == ".":
            index += 1
            name, index = _read_identifier(compact, index)
            if name:
                fields.append(name)
        elif char == "[":
            depth = 0
            start = index + 1
            while index < len(compact):
                if compact[index] == "[":
                    depth += 1
                elif compact[index] == "]":
                    depth -= 1
                    if depth == 0:
                        break
                index += 1
            qualifiers.append(compact[start:index])
            index += 1
        else:
            index += 1
    return _AccessPath(tuple() if not base else base, tuple(qualifiers), tuple(fields), compact)


def _read_identifier(text: str, index: int) -> tuple[str, int]:
    start = index
    while index < len(text) and (text[index].isalnum() or text[index] == "_"):
        index += 1
    return text[start:index], index


def _is_constant_qualifier(qualifier: str) -> bool:
    """True for enum/macro-style constants (``CONSOLE``, ``SYS_fork``, ``0``).

    Single-lowercase identifiers are treated as variables (dynamic index): a
    call through ``table[i]`` must still match ``table[CONSOLE]`` facts, while
    ``table[CONSOLE]`` vs ``table[NON_ADV_ID]`` stays a qualifier mismatch.
    """

    compact = _key(qualifier)
    if not compact:
        return True
    if re.fullmatch(r"[-+]?\d+(x[0-9a-fA-F]+)?[uUlL]*", compact):
        return True
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", compact):
        return False
    # ``CONSOLE``, ``SYS_fork``, ``PNG_FILTER_VALUE_AVG`` are enum/macro
    # constants; lowercase-first names (``i``, ``num``) are variables.
    return compact[0].isupper()


# --------------------------------------------------------------------------- #
# Per-TU extraction
# --------------------------------------------------------------------------- #


class _Extractor:
    def __init__(
        self,
        unit: Any,
        cache: SourceCache,
        *,
        max_facts: int = _MAX_FACTS,
        max_sites: int = _MAX_SITES,
    ) -> None:
        self.unit = unit
        self.cache = cache
        self.max_facts = max_facts
        self.max_sites = max_sites
        self.tu_id = str(getattr(getattr(unit, "tu", None), "id", ""))
        self.repo_root = str(getattr(getattr(unit, "tu", None), "repo_root", "") or "")
        self.main_file = _main_file(cache, unit)
        self.globals: dict[str, Any] = {}
        self.alias: dict[str, _FuncRef] = {}
        self.facts: list[RegistrationFact] = []
        self.sites: list[IndirectCallSite] = []
        self.functions: list[FunctionRecord] = list(getattr(unit, "functions", ()) or ())
        self.functions_by_id = {f.id: f for f in self.functions}
        self._seq = 0

    # -- public ----------------------------------------------------------
    def run(self) -> tuple[list[RegistrationFact], list[IndirectCallSite]]:
        self._collect_globals()
        self._scan_global_initializers()
        for function in self.functions:
            if not getattr(function, "is_definition", False):
                continue
            if self.main_file and function.sloc.file != self.main_file:
                continue
            cursor = _definition_cursor(self.unit, function)
            if cursor is None:
                continue
            body = find_child_stmt(cursor)
            if body is None:
                continue
            param_index = _param_index(cursor)
            self._scan_function_body(function, body, param_index)
            self._scan_indirect_sites(function, cursor, param_index)
        return self.facts, self.sites

    # -- global symbols --------------------------------------------------
    def _collect_globals(self) -> None:
        tu = _clang_tu(self.unit)
        if tu is None:
            return
        try:
            for cursor in tu.cursor.get_children():
                if cursor.kind == CursorKind.VAR_DECL and cursor.spelling:
                    self.globals.setdefault(cursor.spelling, cursor)
        except Exception:  # pragma: no cover - defensive
            pass

    def _scan_global_initializers(self) -> None:
        tu = _clang_tu(self.unit)
        if tu is None:
            return
        try:
            children = list(tu.cursor.get_children())
        except Exception:  # pragma: no cover - defensive
            return
        for cursor in children:
            if cursor.kind != CursorKind.VAR_DECL or not cursor.spelling:
                continue
            if not _is_function_pointer_storage(cursor.type):
                continue
            for child in cursor.get_children():
                self._scan_initializer(
                    cursor.spelling, child, cursor, KIND_GLOBAL_ASSIGN
                )

    def _scan_initializer(
        self, var_name: str, node: Any, cursor: Any, kind: str
    ) -> None:
        current = unwrap(node)
        if current is None:
            return
        if current.kind == CursorKind.INIT_LIST_EXPR:
            for position, child in enumerate(current.get_children()):
                field_name, index = _initializer_designator(self.cache, child)
                if index is not None:
                    object_path = f"{var_name}[{index}]"
                    child_kind = KIND_DESIGNATED
                elif field_name:
                    object_path = f"{var_name}.{field_name}"
                    child_kind = KIND_STRUCT_FIELD
                else:
                    object_path = f"{var_name}[{position}]"
                    child_kind = KIND_ARRAY_ELEMENT
                self._record_initializer_refs(
                    child, var_name, object_path, cursor, child_kind
                )
            return
        self._record_initializer_refs(
            node,
            var_name,
            var_name,
            cursor,
            KIND_GLOBAL_ASSIGN if kind == KIND_GLOBAL_ASSIGN else kind,
        )

    def _record_initializer_refs(
        self, node: Any, var_name: str, object_path: str, cursor: Any, kind: str
    ) -> None:
        for ref in _target_refs(node, kind):
            self._add_fact(
                object_path=object_path,
                ref=ref,
                kind=kind,
                cursor=cursor,
                evidence=f"global initializer {object_path}",
            )

    # -- function bodies -------------------------------------------------
    def _scan_function_body(
        self, function: FunctionRecord, node: Any, param_index: dict[str, int]
    ) -> None:
        kind = node.kind
        if kind in (CursorKind.BINARY_OPERATOR, CursorKind.COMPOUND_ASSIGNMENT_OPERATOR):
            self._scan_assignment(function, node, param_index)
        elif kind == CursorKind.CALL_EXPR:
            self._scan_call(function, node, param_index)
        elif kind == CursorKind.RETURN_STMT:
            refs: list[_FuncRef] = []
            for child in node.get_children():
                refs.extend(_target_refs(child, KIND_RETURN_POINTER))
            if refs:
                key = function.usr or function.id or function.name
                for ref in refs:
                    self._add_fact(
                        object_path=f"return:{key}",
                        ref=ref,
                        kind=KIND_RETURN_POINTER,
                        cursor=node,
                        evidence=f"returned from {function.name}",
                        registrar_function_id=function.id,
                    )
        for child in node.get_children():
            if child.kind in _FUNCTION_KINDS:
                continue
            self._scan_function_body(function, child, param_index)

    def _scan_assignment(
        self, function: FunctionRecord, node: Any, param_index: dict[str, int]
    ) -> None:
        children = list(node.get_children())
        if len(children) < 2:
            return
        lhs = unwrap(children[0])
        if lhs is None:
            return
        split = assignment_split(self.cache, node)
        if split is None or split[0] != "=":
            return
        lhs_text = normalize_text(self.cache.text_for(lhs, 160))
        path = parse_access_path(lhs_text)
        if not path.base:
            return
        rhs = unwrap(children[-1])
        refs = _target_refs(rhs, KIND_GLOBAL_ASSIGN)
        if path.base in self.globals:
            if not refs:
                return
            if path.qualifiers:
                kind = KIND_ARRAY_ELEMENT
            elif path.fields:
                kind = KIND_STRUCT_FIELD
            else:
                kind = KIND_GLOBAL_ASSIGN
            object_path = path.canonical()
            for ref in refs:
                self._add_fact(
                    object_path=object_path,
                    ref=ref,
                    kind=kind,
                    cursor=node,
                    evidence=f"assigned to {object_path}",
                    registrar_function_id=function.id,
                )
            return
        # Local alias chain: ``fp = handler`` inside a function, later stored
        # into a global through the alias.
        if len(path.fields) == 0 and len(path.qualifiers) == 0:
            for ref in refs:
                self.alias.setdefault(path.base, ref)

    def _scan_call(
        self, function: FunctionRecord, node: Any, param_index: dict[str, int]
    ) -> None:
        children = list(node.get_children())
        if not children:
            return
        callee = _function_ref_from_expr(children[0])
        if callee is None:
            return
        name = callee.name
        arguments = [unwrap(child) for child in children[1:]]
        # Callback argument passing across TUs: param:<callee>:<arg index>.
        for index, argument in enumerate(arguments):
            refs = _target_refs(argument, KIND_ARGUMENT_PASS)
            if not refs:
                continue
            key = callee.usr or name
            for ref in refs:
                self._add_fact(
                    object_path=f"param:{key}:{index}",
                    ref=ref,
                    kind=KIND_ARGUMENT_PASS,
                    cursor=node,
                    evidence=(
                        f"passed as argument {index} to {name} at "
                        f"{_location(self.cache, node)}"
                    ),
                    registrar_function_id=function.id,
                )
        if not _REGISTRATION_CALL_PATTERN.search(name):
            return
        # Registration-call facts for storage/audit (not used for path
        # matching unless the argument is a global object path).
        refs_from_args: list[tuple[_FuncRef, str]] = []
        global_argument = ""
        for index, argument in enumerate(arguments):
            for ref in _target_refs(argument, KIND_REGISTRATION_CALL):
                refs_from_args.append((ref, f"arg{index}"))
            if argument is not None and argument.kind == CursorKind.UNARY_OPERATOR:
                tokens = token_list(argument)
                if tokens and tokens[0].spelling == "&":
                    text = normalize_text(self.cache.text_for(argument, 160))
                    path = parse_access_path(text)
                    if path.base and path.base in self.globals:
                        global_argument = path.canonical()
        for ref, position in refs_from_args[:8]:
            object_path = (
                f"{global_argument}:registered_by:{name}"
                if global_argument
                else f"call:{name}:{position}"
            )
            self._add_fact(
                object_path=object_path,
                ref=ref,
                kind=KIND_REGISTRATION_CALL,
                cursor=node,
                evidence=(
                    f"registration call {name}(...) at "
                    f"{_location(self.cache, node)}"
                ),
                registrar_function_id=function.id,
            )

    # -- indirect call sites --------------------------------------------
    def _scan_indirect_sites(
        self, function: FunctionRecord, cursor: Any, param_index: dict[str, int]
    ) -> None:
        body = find_child_stmt(cursor)
        if body is None:
            return
        parse_calls = _parse_calls_by_position(self.unit, function)

        def visit(node: Any) -> None:
            if node.kind == CursorKind.CALL_EXPR:
                children = list(node.get_children())
                if children:
                    callee = unwrap(children[0])
                    if callee is not None and self._site_for_call(
                        function, cursor, node, callee, param_index, parse_calls
                    ):
                        pass
            for child in node.get_children():
                if child.kind in _FUNCTION_KINDS:
                    continue
                visit(child)

        visit(body)

    def _site_for_call(
        self,
        function: FunctionRecord,
        cursor: Any,
        call: Any,
        callee: Any,
        param_index: dict[str, int],
        parse_calls: Mapping[tuple[int, int], Any],
    ) -> bool:
        if _function_ref_from_expr(callee) is not None:
            return False
        signature = _signature_from_type(callee.type)
        if signature is None:
            return False
        callee_text = normalize_text(self.cache.text_for(callee, 160))
        if not callee_text:
            return False
        kind = "path"
        index = -1
        getter_usr = ""
        getter_name = ""
        simple = parse_access_path(callee_text)
        if (
            not simple.fields
            and not simple.qualifiers
            and simple.base in param_index
        ):
            kind = "param"
            index = param_index[simple.base]
        elif callee.kind == CursorKind.CALL_EXPR:
            inner = list(callee.get_children())
            if inner:
                getter = _function_ref_from_expr(inner[0])
                if getter is not None:
                    kind = "call"
                    getter_usr = getter.usr
                    getter_name = getter.name
        sloc = cursor_sloc(self.cache, call)
        call_id = _call_id_for_site(
            self.unit, self.cache, function, call, sloc, parse_calls
        )
        if len(self.sites) >= self.max_sites:
            return False
        self.sites.append(
            IndirectCallSite(
                call_id=call_id,
                caller_id=function.id,
                caller_name=function.name,
                caller_usr=function.usr,
                callee_text=callee_text,
                callee_kind=kind,
                signature=signature,
                file=sloc.file,
                line=sloc.line,
                col=sloc.col,
                param_index=index,
                getter_usr=getter_usr,
                getter_name=getter_name,
            )
        )
        return True

    # -- fact helper -----------------------------------------------------
    def _add_fact(
        self,
        *,
        object_path: str,
        ref: _FuncRef,
        kind: str,
        cursor: Any,
        evidence: str,
        registrar_function_id: str = "",
    ) -> None:
        if len(self.facts) >= self.max_facts:
            return
        sloc = cursor_sloc(self.cache, cursor)
        function_name = _function_name_for_ref(self.unit, ref)
        function_id = _function_id_for_ref(self.unit, ref)
        file = sloc.file
        fact = RegistrationFact(
            object_path=object_path,
            target_function_name=ref.name,
            target_function_id=function_id,
            target_usr=ref.usr,
            object_field=_field_of(object_path),
            registration_kind=kind,
            file=file,
            line=sloc.line,
            col=sloc.col,
            end_line=sloc.end_line,
            end_col=sloc.end_col,
            tu_id=self.tu_id,
            evidence=[evidence],
            module=_module_of(file),
            directory=os.path.dirname(file),
            registrar_function_id=registrar_function_id,
            id=_fact_id(self.tu_id, object_path, function_id, function_name, file, sloc.line, kind),
        )
        self.facts.append(fact)


# --------------------------------------------------------------------------- #
# Public extraction entry points
# --------------------------------------------------------------------------- #


def extract_registrations(
    unit: Any, *, cache: SourceCache | None = None
) -> tuple[list[RegistrationFact], list[IndirectCallSite]]:
    """Extract registration facts and indirect call sites for one parsed TU."""

    if cache is None:
        repo_root = str(getattr(getattr(unit, "tu", None), "repo_root", "") or "")
        cache = SourceCache(repo_root)
    extractor = _Extractor(unit, cache)
    return extractor.run()


def extract_indirect_call_sites(
    unit: Any, *, cache: SourceCache | None = None
) -> list[IndirectCallSite]:
    facts, sites = extract_registrations(unit, cache=cache)
    return sites


# --------------------------------------------------------------------------- #
# Cross-TU resolution
# --------------------------------------------------------------------------- #


def resolve_cross_tu_targets(
    call_sites: Sequence[IndirectCallSite],
    registrations: Sequence[RegistrationFact],
    functions: Mapping[str, FunctionRecord],
    *,
    signatures: Mapping[str, _Signature] | None = None,
    existing_edges: Iterable[tuple[str, str]] = (),
    max_candidates_per_call: int = _MAX_CANDIDATES,
    max_targets: int = _MAX_TARGETS,
    dedupe: bool = True,
) -> tuple[list[CallTargetRecord], list[tuple[str, str]]]:
    """Resolve indirect call sites against cross-TU registration facts.

    Returns ``(call_targets, links)`` where links are
    ``(registration_id, call_target_id)`` pairs for the store's
    ``callback_target_links`` table.

    Every candidate whose signature matches is retained (bounded per call site
    and overall); there is no confidence score, threshold or ranking. ``dedupe``
    collapses the same logical edge emitted from header-inline functions
    instantiated in several TUs: the key is
    ``(caller_usr or caller_id, callee_usr or callee_id, call file, line)``.
    The retained representative is deterministic, duplicate evidence is merged
    into its ``reason`` and every matching registration id links to the
    retained ``call_target_id``.
    """

    signatures = signatures or {}
    seen_edges = set(existing_edges)
    by_path: dict[tuple[str, tuple[str, ...]], list[RegistrationFact]] = defaultdict(list)
    by_param: dict[str, list[RegistrationFact]] = defaultdict(list)
    by_return: dict[str, list[RegistrationFact]] = defaultdict(list)
    for fact in registrations:
        path = parse_access_path(fact.object_path)
        by_path[(path.base, path.fields)].append(fact)
        if fact.registration_kind == KIND_ARGUMENT_PASS:
            by_param[fact.object_path].append(fact)
        elif fact.registration_kind == KIND_RETURN_POINTER:
            by_return[fact.object_path].append(fact)

    entries: list[tuple[IndirectCallSite, FunctionRecord, str, RegistrationFact]] = []
    for site in sorted(call_sites, key=lambda item: (item.file, item.line, item.col, item.call_id)):
        candidates = _match_site(site, by_path, by_param, by_return)
        filtered: list[tuple[FunctionRecord, str, RegistrationFact]] = []
        for fact, reason in candidates:
            function = functions.get(fact.target_function_id)
            if function is None:
                continue
            if not _signature_matches(site.signature, signatures.get(function.id)):
                continue
            filtered.append((function, reason, fact))
        filtered.sort(key=lambda item: item[0].id)
        if len(filtered) > max_candidates_per_call:
            filtered = filtered[:max_candidates_per_call]
        for function, reason, fact in filtered:
            edge_key = (site.call_id, function.id)
            if edge_key in seen_edges:
                continue
            seen_edges.add(edge_key)
            entries.append((site, function, reason, fact))

    groups: dict[tuple[str, str, str, int], list[tuple[IndirectCallSite, FunctionRecord, str, RegistrationFact]]] = {}
    order: list[tuple[str, str, str, int]] = []
    for entry in entries:
        site, function, _reason, _fact = entry
        if dedupe:
            key = (
                site.caller_usr or site.caller_id,
                function.usr or function.id,
                site.file,
                site.line,
            )
        else:
            key = (site.file, site.line, site.call_id, function.id)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(entry)

    records: list[CallTargetRecord] = []
    links: list[tuple[str, str]] = []
    seen_links: set[tuple[str, str]] = set()
    for key in order:
        bucket = groups[key]
        bucket.sort(key=lambda item: (item[0].call_id, item[1].id))
        site, function, reason, fact = bucket[0]
        merged_reasons: list[str] = []
        for other in bucket[1:]:
            other_reason = other[2]
            if other_reason and other_reason not in merged_reasons and other_reason != reason:
                merged_reasons.append(other_reason)
        merged_reason = reason
        if merged_reasons:
            suffix = " ; also ".join(merged_reasons[:3])
            if len(merged_reasons) > 3:
                suffix += f" (+{len(merged_reasons) - 3} more)"
            merged_reason = f"{reason} ; also {suffix}"
        target_id = core_ids.call_target_id(
            site.call_id, function.id, "indirect_candidate"
        )
        records.append(
            CallTargetRecord(
                id=target_id,
                call_id=site.call_id,
                caller_id=site.caller_id,
                callee_id=function.id,
                callee_name=function.name,
                kind="indirect_candidate",
                reason=merged_reason,
                sloc=Sloc(
                    file=site.file,
                    line=site.line,
                    col=site.col,
                ),
            )
        )
        for other in bucket:
            other_fact = other[3]
            if other_fact.id and (other_fact.id, target_id) not in seen_links:
                seen_links.add((other_fact.id, target_id))
                links.append((other_fact.id, target_id))
        if len(records) >= max_targets:
            break
    records.sort(
        key=lambda record: (
            record.sloc.line,
            record.sloc.col,
            record.call_id,
            record.callee_id,
        )
    )
    return records, links


def _match_site(
    site: IndirectCallSite,
    by_path: Mapping[tuple[str, tuple[str, ...]], list[RegistrationFact]],
    by_param: Mapping[str, list[RegistrationFact]],
    by_return: Mapping[str, list[RegistrationFact]],
) -> list[tuple[RegistrationFact, str]]:
    facts: list[tuple[RegistrationFact, str]] = []
    if site.callee_kind == "param":
        keys = [f"param:{site.caller_usr}:{site.param_index}"]
        if site.caller_name:
            keys.append(f"param:{site.caller_name}:{site.param_index}")
        matched = []
        for key in keys:
            matched = by_param.get(key, [])
            if matched:
                break
        for fact in matched:
            facts.append(
                (
                    fact,
                    f"cross-TU argument pass: {fact.target_function_name} passed to "
                    f"{site.caller_name} parameter {site.param_index} at "
                    f"{fact.file}:{fact.line} ({fact.registration_kind})",
                )
            )
    elif site.callee_kind == "call":
        keys = []
        if site.getter_usr:
            keys.append(f"return:{site.getter_usr}")
        if site.getter_name:
            keys.append(f"return:{site.getter_name}")
        matched = []
        for key in keys:
            matched = by_return.get(key, [])
            if matched:
                break
        for fact in matched:
            facts.append(
                (
                    fact,
                    f"cross-TU returned callback: {fact.target_function_name} returned by "
                    f"{site.getter_name or site.getter_usr} at {fact.file}:{fact.line}",
                )
            )
    else:
        call_path = parse_access_path(site.callee_text)
        if not call_path.base:
            return []
        for fact in _path_matches(call_path, by_path):
            facts.append(
                (
                    fact,
                    f"cross-TU registration: {fact.object_path} = "
                    f"{fact.target_function_name} at "
                    f"{fact.file}:{fact.line} "
                    f"({fact.registration_kind}, "
                    f"qualifier={'dynamic' if _has_dynamic_qualifier(call_path) else 'exact'})",
                )
            )
    return facts


def _path_matches(
    call_path: _AccessPath,
    by_path: Mapping[tuple[str, tuple[str, ...]], list[RegistrationFact]],
) -> list[RegistrationFact]:
    matches: list[RegistrationFact] = []
    facts = by_path.get((call_path.base, call_path.fields), ())
    if facts:
        for fact in facts:
            if fact.registration_kind in (
                KIND_ARGUMENT_PASS,
                KIND_RETURN_POINTER,
                KIND_REGISTRATION_CALL,
            ):
                continue
            registration_path = parse_access_path(fact.object_path)
            if registration_path.base != call_path.base:
                continue
            if registration_path.fields != call_path.fields:
                continue
            if len(registration_path.qualifiers) != len(call_path.qualifiers):
                continue
            if not _qualifier_matches(
                call_path.qualifiers, registration_path.qualifiers
            ):
                continue
            matches.append(fact)
    return matches


def _qualifier_matches(
    call_qualifiers: tuple[str, ...], registration_qualifiers: tuple[str, ...]
) -> bool:
    """Whether a call site qualifier chain can reference the registration path.

    Exact qualifiers must match; when both chains are constant but different the
    match is rejected (distinct array slots/objects). A dynamic qualifier (index
    variable, runtime field) can alias the other chain, so it is retained as a
    candidate regardless of the other side.
    """

    if not call_qualifiers and not registration_qualifiers:
        return True
    if all(
        _key(call_q) == _key(registration_q)
        for call_q, registration_q in zip(call_qualifiers, registration_qualifiers)
    ):
        return True
    call_dynamic = any(not _is_constant_qualifier(q) for q in call_qualifiers)
    registration_dynamic = any(
        not _is_constant_qualifier(q) for q in registration_qualifiers
    )
    return call_dynamic or registration_dynamic


def _has_dynamic_qualifier(path: _AccessPath) -> bool:
    return any(not _is_constant_qualifier(q) for q in path.qualifiers)


# --------------------------------------------------------------------------- #
# Signatures and orchestration
# --------------------------------------------------------------------------- #

def _register_functions(units: Sequence[Any]) -> dict[str, FunctionRecord]:
    mapping: dict[str, FunctionRecord] = {}
    for unit in units:
        for function in getattr(unit, "functions", ()) or ():
            mapping.setdefault(function.id, function)
    return mapping


def build_function_signatures(
    units: Sequence[Any],
    function_ids: Iterable[str] | None = None,
) -> dict[str, _Signature]:
    wanted = set(function_ids) if function_ids is not None else None
    result: dict[str, _Signature] = {}
    for unit in units:
        for function in getattr(unit, "functions", ()) or ():
            if wanted is not None and function.id not in wanted:
                continue
            if not getattr(function, "is_definition", False):
                continue
            cursor = _definition_cursor(unit, function)
            if cursor is None:
                continue
            signature = _signature_from_type(cursor.type)
            if signature is not None:
                result.setdefault(function.id, signature)
    return result


def extract_target_signatures(
    unit: Any, function_ids: Iterable[str]
) -> dict[str, _Signature]:
    """Signatures of the given definition ids inside one parsed TU.

    A DB post-processing pass can call this per TU to build the
    ``signatures`` mapping accepted by :func:`resolve_cross_tu_targets`
    without keeping parsed units resident.
    """

    wanted = set(function_ids)
    result: dict[str, _Signature] = {}
    if not wanted:
        return result
    for function in getattr(unit, "functions", ()) or ():
        if function.id not in wanted or not getattr(function, "is_definition", False):
            continue
        cursor = _definition_cursor(unit, function)
        if cursor is None:
            continue
        signature = _signature_from_type(cursor.type)
        if signature is not None:
            result.setdefault(function.id, signature)
    return result


def analyze_cross_tu(
    units: Sequence[Any],
    *,
    cache: SourceCache | None = None,
) -> CrossTuResult:
    """Run extraction + resolution over a batch of parsed TUs."""

    if cache is None:
        repo_root = ""
        for unit in units:
            record = getattr(unit, "tu", None)
            repo_root = str(getattr(record, "repo_root", "") or "")
            if repo_root:
                break
        cache = SourceCache(repo_root)
    registrations: list[RegistrationFact] = []
    call_sites: list[IndirectCallSite] = []
    for unit in units:
        facts, sites = extract_registrations(unit, cache=cache)
        registrations.extend(facts)
        call_sites.extend(sites)
    functions = _register_functions(units)
    target_ids = {
        fact.target_function_id for fact in registrations if fact.target_function_id
    }
    signatures = build_function_signatures(units, target_ids)
    call_targets, links = resolve_cross_tu_targets(
        call_sites,
        registrations,
        functions,
        signatures=signatures,
    )
    stats = {
        "units": len(units),
        "registrations": len(registrations),
        "registration_kinds": _count_by(registrations, "registration_kind"),
        "indirect_call_sites": len(call_sites),
        "call_targets": len(call_targets),
        "links": len(links),
    }
    return CrossTuResult(
        registrations=sorted(registrations, key=lambda f: (f.file, f.line, f.col, f.object_path, f.target_function_name)),
        call_sites=sorted(call_sites, key=lambda s: (s.file, s.line, s.col, s.call_id)),
        call_targets=call_targets,
        links=links,
        stats=stats,
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _clang_tu(unit: Any) -> Any:
    method = getattr(unit, "clang_tu", None)
    if not callable(method):
        return None
    try:
        return method()
    except Exception:  # pragma: no cover - parse details
        return None


def _main_file(cache: SourceCache, unit: Any) -> str:
    record = getattr(unit, "tu", None)
    path = getattr(record, "file", "") if record is not None else ""
    return cache.repo_path(path) if path else ""


def _param_index(function_cursor: Any) -> dict[str, int]:
    result: dict[str, int] = {}
    index = 0
    try:
        for child in function_cursor.get_children():
            if child.kind == CursorKind.PARM_DECL:
                if child.spelling:
                    result[child.spelling] = index
                index += 1
    except Exception:  # pragma: no cover - defensive
        return result
    return result


def _target_refs(node: Any, kind: str) -> list[_FuncRef]:
    from ca.analyze.callbacks import _refs_from_expr

    refs: list[_FuncRef] = []
    seen: set[tuple[str, str]] = set()
    for ref in _refs_from_expr(node, "", None, None):
        if ref.kind != "func" or not ref.name:
            continue
        key = (ref.usr, ref.name)
        if key in seen:
            continue
        seen.add(key)
        refs.append(_FuncRef(usr=ref.usr, name=ref.name))
    return refs


def _call_id_for_site(
    unit: Any,
    cache: SourceCache,
    function: FunctionRecord,
    call_cursor: Any,
    sloc: Sloc,
    parse_calls: Mapping[tuple[int, int], Any],
) -> str:
    record = parse_calls.get((sloc.line, sloc.col))
    if record is not None and getattr(record, "id", ""):
        return record.id
    text = normalize_text(cache.text_for(call_cursor, 160))
    return core_ids.call_id(
        caller_id=function.id,
        callee_usr="",
        callee_name=text,
        sloc=sloc,
    )


def _initializer_designator(
    cache: SourceCache, node: Any
) -> tuple[str, str | None]:
    """Return ``(field_name, index)`` for a designated initializer child."""

    current = unwrap(node)
    if current is None:
        return "", None
    tokens = token_list(current, 16)
    spellings = [token.spelling for token in tokens]
    for index, spelling in enumerate(spellings):
        if spelling == "." and index + 1 < len(spellings):
            name = spellings[index + 1]
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                return name, None
        if spelling == "[" and index + 2 < len(spellings):
            value = spellings[index + 1]
            if re.fullmatch(r"[-+]?[0-9+A-Za-z_][A-Za-z0-9_+\-x]*", value):
                return "", value
    return "", None


def _field_of(object_path: str) -> str:
    path = parse_access_path(object_path)
    return path.fields[-1] if path.fields else ""


def _module_of(file: str) -> str:
    parts = [part for part in file.split("/") if part]
    return "/".join(parts[:2]) if len(parts) >= 2 else (parts[0] if parts else "")


def _fact_id(
    tu_id: str,
    object_path: str,
    function_id: str,
    function_name: str,
    file: str,
    line: int,
    kind: str,
) -> str:
    return "cbreg:" + core_ids.digest(
        tu_id, object_path, function_id, function_name, file, line, kind
    )


def _function_name_for_ref(unit: Any, ref: _FuncRef) -> str:
    return ref.name


def _function_id_for_ref(unit: Any, ref: _FuncRef) -> str:
    for function in getattr(unit, "functions", ()) or ():
        if ref.usr and function.usr == ref.usr:
            return function.id
        if not ref.usr and function.name == ref.name:
            return function.id
    return ""


def _signature_matches(site: _Signature, candidate: _Signature | None) -> bool:
    return site.matches(candidate)


def _count_by(items: Sequence[Any], attribute: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        value = str(getattr(item, attribute, "") or "")
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _location(cache: SourceCache, cursor: Any) -> str:
    sloc = cursor_sloc(cache, cursor)
    return f"{sloc.file}:{sloc.line}" if sloc.file else f"line {sloc.line}"


__all__ = [
    "CrossTuResult",
    "IndirectCallSite",
    "KIND_ARGUMENT_PASS",
    "KIND_ARRAY_ELEMENT",
    "KIND_DESIGNATED",
    "KIND_GLOBAL_ASSIGN",
    "KIND_REGISTRATION_CALL",
    "KIND_RETURN_POINTER",
    "KIND_STRUCT_FIELD",
    "RegistrationFact",
    "analyze_cross_tu",
    "build_function_signatures",
    "extract_indirect_call_sites",
    "extract_registrations",
    "extract_target_signatures",
    "parse_access_path",
    "resolve_cross_tu_targets",
]
