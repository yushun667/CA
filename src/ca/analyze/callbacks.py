"""Callback / function-pointer candidate resolution for indirect calls.

The parser marks calls through function pointers as ``kind="indirect"`` but
cannot name a callee. This module recovers *candidate* targets within one
translation unit by tracking how function addresses are stored and passed:

* assignment / initializer: ``ops.fn = handler``, ``o->fn = &handler``,
  ``int (*tbl[2])(int) = {h1, h2}``, typedef'd callback variables;
* parameter passing: ``register_cb(handler)`` followed by a call through the
  parameter inside the callee (same TU call sites);
* function returning a callback pointer (one level);
* copy chains: ``fp = other_fp``.

Candidates are filtered by the call expression's function-pointer signature
(return type, parameter canonical types, variadicity). No evidence => no
record (precision first). Everything is intra-TU; the record carries a
confidence and a human-auditable ``reason`` naming the registration site.

Contract: :class:`ca.core.schema.CallTargetRecord` with
``kind="indirect_candidate"`` and :func:`ca.core.ids.call_target_id`.
"""

from __future__ import annotations

import weakref
from dataclasses import dataclass, field
from typing import Any, Iterable

from clang.cindex import CursorKind, TypeKind

from ca.analyze.astutil import (
    SourceCache,
    assignment_split,
    cursor_sloc,
    find_child_stmt,
    normalize_text,
    token_list,
    unwrap,
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
        getattr(TypeKind, "CONSTANTARRAY", None),
        getattr(TypeKind, "INCOMPLETEARRAY", None),
        getattr(TypeKind, "VARIABLEARRAY", None),
    )
    if kind is not None
)
_INITIALIZER_KINDS = tuple(
    kind
    for kind in (
        getattr(CursorKind, "DESIGNATED_INIT_EXPR", None),
        getattr(CursorKind, "COMPOUND_LITERAL_EXPR", None),
        getattr(CursorKind, "PACK_EXPANSION_EXPR", None),
    )
    if kind is not None
)
_MAX_REGISTRY_FUNCTIONS = 600
_MAX_CANDIDATES_PER_CALL = 32
_MAX_TOTAL_TARGETS = 64


@dataclass(frozen=True)
class _Signature:
    return_type: str
    params: tuple[str, ...]
    variadic: bool

    def matches(self, other: "_Signature | None") -> bool:
        return (
            other is not None
            and self.return_type == other.return_type
            and self.params == other.params
            and self.variadic == other.variadic
        )


@dataclass(frozen=True)
class _FuncRef:
    usr: str
    name: str


@dataclass(frozen=True)
class _Ref:
    """A symbolic reference to a function address."""

    kind: str  # func | var | param | call_return
    evidence: str
    usr: str = ""
    name: str = ""
    func_id: str = ""
    index: int = -1


@dataclass(frozen=True)
class _Candidate:
    function: FunctionRecord
    evidence: str


@dataclass
class _CallSite:
    cursor: Any
    callee_cursor: Any
    callee_text: str
    signature: _Signature
    sloc: Sloc
    line: int
    col: int


@dataclass
class _Registry:
    unit: Any
    cache: SourceCache
    by_usr: dict[str, FunctionRecord] = field(default_factory=dict)
    by_name: dict[str, FunctionRecord] = field(default_factory=dict)
    pointer_refs: dict[str, list[_Ref]] = field(default_factory=dict)
    call_arg_refs: dict[tuple[str, int], list[_Ref]] = field(default_factory=dict)
    return_refs: dict[str, list[_Ref]] = field(default_factory=dict)
    exact: dict[str, dict[str, _Candidate]] = field(default_factory=dict)
    suffix: dict[str, dict[str, _Candidate]] = field(default_factory=dict)
    param: dict[tuple[str, int], dict[str, _Candidate]] = field(default_factory=dict)
    returns: dict[str, dict[str, _Candidate]] = field(default_factory=dict)
    built: bool = False

    # -- construction ----------------------------------------------------
    def build(self) -> None:
        if self.built:
            return
        self.built = True
        for function in self.unit.functions:
            if getattr(function, "usr", ""):
                self.by_usr.setdefault(function.usr, function)
            self.by_name.setdefault(function.name, function)
        main_file = _main_file(self.cache, self.unit)
        tu = _clang_tu(self.unit)
        if tu is not None:
            try:
                for cursor in tu.cursor.get_children():
                    if cursor.kind == CursorKind.VAR_DECL:
                        self._scan_global_var(cursor)
            except Exception:  # pragma: no cover - defensive
                pass
        scanned = 0
        for function in self.unit.functions:
            if scanned >= _MAX_REGISTRY_FUNCTIONS:
                break
            if not getattr(function, "is_definition", False):
                continue
            if main_file and function.sloc.file != main_file:
                continue
            cursor = _definition_cursor(self.unit, function)
            if cursor is None:
                continue
            body = find_child_stmt(cursor)
            if body is None:
                continue
            scanned += 1
            param_index = _param_index(cursor)
            self._scan_statements(body, function, param_index)
        self._resolve_all()

    def _scan_global_var(self, var_cursor: Any) -> None:
        name = var_cursor.spelling
        if not name or not _is_function_pointer_storage(var_cursor.type):
            return
        evidence = f"global initializer {name} at {_location_text(self.cache, var_cursor)}"
        refs: list[_Ref] = []
        for child in var_cursor.get_children():
            refs.extend(_refs_from_expr(child, evidence, None, None))
        if refs:
            self.pointer_refs.setdefault(_key(name), []).extend(refs)

    def _scan_statements(
        self, node: Any, function: FunctionRecord, param_index: dict[str, int]
    ) -> None:
        kind = node.kind
        if kind in (CursorKind.BINARY_OPERATOR, CursorKind.COMPOUND_ASSIGNMENT_OPERATOR):
            self._scan_assignment(node, function, param_index)
        elif kind == CursorKind.CALL_EXPR:
            self._scan_direct_call(node, function, param_index)
        elif kind == CursorKind.RETURN_STMT:
            evidence = f"return at {_location_text(self.cache, node)}"
            refs: list[_Ref] = []
            for child in node.get_children():
                refs.extend(_refs_from_expr(child, evidence, function.id, param_index))
            if refs:
                self.return_refs.setdefault(function.id, []).extend(refs)
        for child in node.get_children():
            if child.kind in _FUNCTION_KINDS:
                continue
            self._scan_statements(child, function, param_index)

    def _scan_assignment(
        self, node: Any, function: FunctionRecord, param_index: dict[str, int]
    ) -> None:
        children = list(node.get_children())
        if len(children) < 2:
            return
        lhs = unwrap(children[0])
        if lhs is None or lhs.kind not in (
            CursorKind.DECL_REF_EXPR,
            CursorKind.MEMBER_REF_EXPR,
            CursorKind.ARRAY_SUBSCRIPT_EXPR,
            CursorKind.UNARY_OPERATOR,
        ):
            return
        if node.kind == CursorKind.BINARY_OPERATOR:
            split = assignment_split(self.cache, node)
            if split is None or split[0] != "=":
                return
        key = _expression_key(self.cache, lhs)
        if not key:
            return
        rhs = unwrap(children[-1])
        evidence = (
            f"assigned to {key} at {_location_text(self.cache, node)}"
        )
        refs = _refs_from_expr(rhs, evidence, function.id, param_index)
        if not refs:
            return
        self.pointer_refs.setdefault(key, []).extend(refs)

    def _scan_direct_call(
        self, node: Any, function: FunctionRecord, param_index: dict[str, int]
    ) -> None:
        children = list(node.get_children())
        if not children:
            return
        callee = _function_ref_from_expr(children[0])
        if callee is None:
            return
        target = self._function_for(callee)
        if target is None:
            return
        for index, argument in enumerate(children[1:]):
            evidence = (
                f"passed as argument {index} to {target.name} at "
                f"{_location_text(self.cache, node)}"
            )
            refs = _refs_from_expr(
                argument, evidence, function.id, param_index
            )
            if refs:
                self.call_arg_refs.setdefault((target.id, index), []).extend(refs)

    # -- resolution ------------------------------------------------------
    def _resolve_all(self) -> None:
        exact: dict[str, dict[str, _Candidate]] = {}
        param: dict[tuple[str, int], dict[str, _Candidate]] = {}
        returns: dict[str, dict[str, _Candidate]] = {}
        for _round in range(4):
            new_exact: dict[str, dict[str, _Candidate]] = {}
            for key, refs in self.pointer_refs.items():
                resolved = _resolve_refs(
                    refs, exact, param, returns, self._function_for
                )
                if resolved:
                    new_exact[key] = resolved
            new_param: dict[tuple[str, int], dict[str, _Candidate]] = {}
            for key, refs in self.call_arg_refs.items():
                resolved = _resolve_refs(
                    refs, new_exact, param, returns, self._function_for
                )
                if resolved:
                    new_param[key] = resolved
            new_returns: dict[str, dict[str, _Candidate]] = {}
            for func_id, refs in self.return_refs.items():
                resolved = _resolve_refs(
                    refs, new_exact, new_param, returns, self._function_for
                )
                if resolved:
                    new_returns[func_id] = resolved
            stable = (
                new_exact == exact
                and new_param == param
                and new_returns == returns
            )
            exact, param, returns = new_exact, new_param, new_returns
            if stable:
                break
        self.exact = exact
        self.param = param
        self.returns = returns
        suffix: dict[str, dict[str, _Candidate]] = {}
        for key, candidates in exact.items():
            suffix_key = _field_suffix(key)
            if suffix_key:
                bucket = suffix.setdefault(suffix_key, {})
                for candidate_key, candidate in candidates.items():
                    bucket.setdefault(candidate_key, candidate)
        self.suffix = suffix

    def _function_for(self, ref: _FuncRef) -> FunctionRecord | None:
        if ref.usr:
            found = self.by_usr.get(ref.usr)
            if found is not None:
                return found
        return self.by_name.get(ref.name)


def _resolve_refs(
    refs: Iterable[_Ref],
    exact: dict[str, dict[str, _Candidate]],
    param: dict[tuple[str, int], dict[str, _Candidate]],
    returns: dict[str, dict[str, _Candidate]],
    lookup: Any,
) -> dict[str, _Candidate]:
    resolved: dict[str, _Candidate] = {}
    for ref in refs:
        if ref.kind == "func":
            function = lookup(_FuncRef(ref.usr, ref.name))
            if function is not None:
                resolved.setdefault(function.id, _Candidate(function, ref.evidence))
        elif ref.kind == "var":
            for candidate in exact.get(_key(ref.name), {}).values():
                resolved.setdefault(
                    candidate.function.id,
                    _Candidate(candidate.function, ref.evidence),
                )
        elif ref.kind == "param":
            for candidate in param.get((ref.func_id, ref.index), {}).values():
                resolved.setdefault(
                    candidate.function.id,
                    _Candidate(candidate.function, ref.evidence),
                )
        elif ref.kind == "call_return":
            for candidate in returns.get(ref.func_id, {}).values():
                resolved.setdefault(
                    candidate.function.id,
                    _Candidate(candidate.function, ref.evidence),
                )
    return resolved


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #

_REGISTRY_CACHE: "weakref.WeakKeyDictionary[Any, _Registry]" = weakref.WeakKeyDictionary()


def resolve_call_targets(
    unit: Any,
    function: FunctionRecord,
    cursor: Any,
    cache: SourceCache,
    *,
    max_confidence: float = 0.95,
    min_confidence: float = 0.4,
    max_targets: int = _MAX_TOTAL_TARGETS,
) -> list[CallTargetRecord]:
    """Return deterministic indirect-call candidate records for one function."""

    if cursor is None:
        return []
    call_sites = _indirect_call_sites(cache, cursor)
    if not call_sites:
        return []
    registry = _registry_for(unit, cache)
    parse_calls = _parse_calls_by_position(unit, function)
    records: list[CallTargetRecord] = []
    for site in call_sites:
        candidates = _candidates_for_site(
            site, function, registry, cache, unit
        )
        if not candidates:
            continue
        call_id = _call_id_for_site(unit, function, site, parse_calls)
        for candidate, confidence, reason in candidates:
            if confidence < min_confidence:
                continue
            records.append(
                CallTargetRecord(
                    id=core_ids.call_target_id(
                        call_id, candidate.id, "indirect_candidate"
                    ),
                    call_id=call_id,
                    caller_id=function.id,
                    callee_id=candidate.id,
                    callee_name=candidate.name,
                    kind="indirect_candidate",
                    confidence=min(round(confidence, 3), max_confidence),
                    reason=reason,
                    sloc=site.sloc,
                )
            )
    records.sort(
        key=lambda record: (
            record.sloc.line,
            record.sloc.col,
            record.call_id,
            -record.confidence,
            record.callee_id,
        )
    )
    unique: list[CallTargetRecord] = []
    seen: set[tuple[str, str]] = set()
    for record in records:
        key = (record.call_id, record.callee_id)
        if key in seen:
            continue
        seen.add(key)
        unique.append(record)
        if len(unique) >= max_targets:
            break
    return unique


# --------------------------------------------------------------------------- #
# Call-site discovery
# --------------------------------------------------------------------------- #


def _indirect_call_sites(cache: SourceCache, function_cursor: Any) -> list[_CallSite]:
    body = find_child_stmt(function_cursor)
    if body is None:
        return []
    sites: list[_CallSite] = []

    def visit(node: Any) -> None:
        if node.kind == CursorKind.CALL_EXPR:
            children = list(node.get_children())
            if children:
                callee = unwrap(children[0])
                if callee is not None and _function_ref_from_expr(callee) is None:
                    signature = _signature_from_type(callee.type)
                    if signature is not None:
                        sloc = cursor_sloc(cache, node)
                        sites.append(
                            _CallSite(
                                cursor=node,
                                callee_cursor=callee,
                                callee_text=_expression_key(cache, callee),
                                signature=signature,
                                sloc=sloc,
                                line=sloc.line,
                                col=sloc.col,
                            )
                        )
        for child in node.get_children():
            if child.kind in _FUNCTION_KINDS:
                continue
            visit(child)

    visit(body)
    return sites


def _parse_calls_by_position(
    unit: Any, function: FunctionRecord
) -> dict[tuple[int, int], Any]:
    mapping: dict[tuple[int, int], Any] = {}
    for call in getattr(unit, "calls", ()) or ():
        if getattr(call, "caller_id", "") != function.id:
            continue
        if getattr(call, "kind", "") == "direct":
            continue
        mapping.setdefault((call.sloc.line, call.sloc.col), call)
    return mapping


def _call_id_for_site(
    unit: Any,
    function: FunctionRecord,
    site: _CallSite,
    parse_calls: dict[tuple[int, int], Any],
) -> str:
    record = parse_calls.get((site.line, site.col))
    if record is not None and getattr(record, "id", ""):
        return record.id
    return core_ids.call_id(
        caller_id=function.id,
        callee_usr="",
        callee_name=site.callee_text,
        sloc=site.sloc,
    )


# --------------------------------------------------------------------------- #
# Candidate construction
# --------------------------------------------------------------------------- #


def _candidates_for_site(
    site: _CallSite,
    function: FunctionRecord,
    registry: _Registry,
    cache: SourceCache,
    unit: Any,
) -> list[tuple[FunctionRecord, float, str]]:
    collected: dict[str, tuple[FunctionRecord, float, str]] = {}
    key = site.callee_text
    base, inner = _callee_keys(site, cache)

    def add(candidates: dict[str, _Candidate], confidence: float) -> None:
        for candidate in candidates.values():
            _merge(collected, candidate, confidence, site, function, unit)

    if key and key in registry.exact:
        add(registry.exact[key], 0.9)
    if base and base != key and base in registry.exact:
        add(registry.exact[base], 0.75)
    if inner and inner in registry.exact:
        add(registry.exact[inner], 0.8)
    # Function-pointer parameter of this function.
    for param_name, index in _param_index(_definition_cursor(unit, function)).items():
        if inner == _key(param_name) or key == _key(param_name):
            add(registry.param.get((function.id, index), {}), 0.7)
    suffix = _field_suffix(key)
    if suffix and suffix in registry.suffix:
        add(registry.suffix[suffix], 0.55)
    # Call of a function returning a callback pointer: callbacks()(...).
    callee_kind = site.callee_cursor.kind
    if callee_kind == CursorKind.CALL_EXPR:
        inner_callee = _function_ref_from_expr(
            list(site.callee_cursor.get_children())[0]
        ) if list(site.callee_cursor.get_children()) else None
        if inner_callee is not None:
            target = registry._function_for(inner_callee)
            if target is not None:
                add(registry.returns.get(target.id, {}), 0.6)
    filtered: list[tuple[FunctionRecord, float, str]] = []
    for candidate in collected.values():
        candidate_signature = _function_signature(unit, candidate[0])
        if not site.signature.matches(candidate_signature):
            continue
        if candidate[0].id == function.id and candidate[1] < 0.7:
            # A low-confidence (field-suffix) match that resolves to the caller
            # itself is almost always a same-named field collision; drop it.
            continue
        filtered.append(candidate)
    if len(filtered) > _MAX_CANDIDATES_PER_CALL:
        filtered = filtered[:_MAX_CANDIDATES_PER_CALL]
    if len(filtered) > 1:
        filtered = [
            (candidate, max(0.4, confidence - 0.1), reason)
            for candidate, confidence, reason in filtered
        ]
    return filtered


def _merge(
    collected: dict[str, tuple[FunctionRecord, float, str]],
    candidate: _Candidate,
    confidence: float,
    site: _CallSite,
    function: FunctionRecord,
    unit: Any,
) -> None:
    existing = collected.get(candidate.function.id)
    if existing is not None and existing[1] >= confidence:
        return
    reason = (
        f"indirect call {site.callee_text or '?'} at "
        f"{site.sloc.file}:{site.sloc.line}: {candidate.evidence}"
    )
    collected[candidate.function.id] = (candidate.function, confidence, reason)


# --------------------------------------------------------------------------- #
# AST helpers
# --------------------------------------------------------------------------- #


def _definition_cursor(unit: Any, function: FunctionRecord) -> Any:
    method = getattr(unit, "definition_cursor", None)
    if callable(method):
        try:
            return method(function.id)
        except Exception:  # pragma: no cover - parse detail
            return None
    return None


def _clang_tu(unit: Any) -> Any:
    method = getattr(unit, "clang_tu", None)
    if not callable(method):
        return None
    try:
        return method()
    except Exception:  # pragma: no cover
        return None


def _main_file(cache: SourceCache, unit: Any) -> str:
    record = getattr(unit, "tu", None)
    path = getattr(record, "file", "") if record is not None else ""
    return cache.repo_path(path) if path else ""


def _registry_for(unit: Any, cache: SourceCache) -> _Registry:
    try:
        registry = _REGISTRY_CACHE.get(unit)
    except TypeError:  # pragma: no cover - non-weakrefable unit
        registry = None
    if registry is None:
        registry = _Registry(unit=unit, cache=cache)
        try:
            _REGISTRY_CACHE[unit] = registry
        except TypeError:  # pragma: no cover
            pass
        registry.build()
    return registry


def _param_index(function_cursor: Any) -> dict[str, int]:
    result: dict[str, int] = {}
    if function_cursor is None:
        return result
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


def _function_ref_from_expr(expr: Any) -> _FuncRef | None:
    current = unwrap(expr)
    if current is None:
        return None
    if current.kind == CursorKind.UNARY_OPERATOR:
        children = list(current.get_children())
        tokens = token_list(current)
        if children and tokens and tokens[0].spelling == "&":
            return _function_ref_from_expr(children[0])
        return None
    if current.kind != CursorKind.DECL_REF_EXPR:
        return None
    try:
        referenced = current.referenced
    except Exception:  # pragma: no cover - external decls
        return None
    if referenced is None or referenced.kind not in _FUNCTION_KINDS:
        return None
    try:
        usr = referenced.get_usr()
    except Exception:  # pragma: no cover
        usr = ""
    return _FuncRef(usr=usr, name=referenced.spelling)


def _refs_from_expr(
    expr: Any,
    evidence: str,
    function_id: str | None,
    param_index: dict[str, int] | None,
) -> list[_Ref]:
    current = unwrap(expr)
    if current is None:
        return []
    if current.kind == CursorKind.UNARY_OPERATOR:
        children = list(current.get_children())
        tokens = token_list(current)
        if children and tokens and tokens[0].spelling == "&":
            return _refs_from_expr(children[0], evidence, function_id, param_index)
        return []
    if current.kind == CursorKind.ARRAY_SUBSCRIPT_EXPR:
        children = list(current.get_children())
        if children:
            return _refs_from_expr(children[0], evidence, function_id, param_index)
        return []
    if current.kind == CursorKind.UNEXPOSED_EXPR:
        # Multi-child unexposed wrapper around a designated initializer
        # (``[SYS_fork] = sys_fork``); unwrap() cannot reduce it.
        refs = []
        for child in current.get_children():
            refs.extend(
                _refs_from_expr(child, evidence, function_id, param_index)
            )
        return refs
    if current.kind == CursorKind.INIT_LIST_EXPR:
        refs: list[_Ref] = []
        for child in current.get_children():
            refs.extend(
                _refs_from_expr(child, evidence, function_id, param_index)
            )
        return refs
    if current.kind in _INITIALIZER_KINDS:
        # Designated initializers (``[SYS_fork] = sys_fork``) and compound
        # literals wrap the function reference in an extra node.
        refs = []
        for child in current.get_children():
            refs.extend(
                _refs_from_expr(child, evidence, function_id, param_index)
            )
        return refs
    if current.kind == CursorKind.CALL_EXPR:
        children = list(current.get_children())
        if children:
            callee = _function_ref_from_expr(children[0])
            if callee is not None:
                return [_Ref(kind="call_return", evidence=evidence, usr=callee.usr, name=callee.name)]
        return []
    if current.kind == CursorKind.DECL_REF_EXPR:
        function_ref = _function_ref_from_expr(current)
        if function_ref is not None:
            return [
                _Ref(
                    kind="func",
                    evidence=evidence,
                    usr=function_ref.usr,
                    name=function_ref.name,
                )
            ]
        try:
            referenced = current.referenced
        except Exception:  # pragma: no cover
            referenced = None
        name = current.spelling
        if not name:
            return []
        if (
            referenced is not None
            and referenced.kind == CursorKind.PARM_DECL
            and param_index is not None
            and name in param_index
            and function_id
        ):
            return [
                _Ref(
                    kind="param",
                    evidence=evidence,
                    func_id=function_id,
                    index=param_index[name],
                )
            ]
        return [_Ref(kind="var", evidence=evidence, name=name)]
    return []


def _is_function_pointer_storage(type_obj: Any) -> bool:
    try:
        canonical = type_obj.get_canonical()
        kind = canonical.kind
        if kind in _ARRAY_TYPE_KINDS:
            element = canonical.element_type.get_canonical()
            return _signature_from_canonical(element) is not None
        return _signature_from_canonical(canonical) is not None
    except Exception:  # pragma: no cover - defensive
        return False


def _signature_from_type(type_obj: Any) -> _Signature | None:
    try:
        return _signature_from_canonical(type_obj.get_canonical())
    except Exception:  # pragma: no cover - defensive
        return None


def _signature_from_canonical(canonical: Any) -> _Signature | None:
    try:
        kind = canonical.kind
        if kind == TypeKind.POINTER:
            pointee = canonical.get_pointee().get_canonical()
        elif kind == TypeKind.FUNCTIONPROTO:
            pointee = canonical
        else:
            return None
        if pointee.kind != TypeKind.FUNCTIONPROTO:
            return None
        return _Signature(
            return_type=_canonical_spelling(pointee.get_result()),
            params=tuple(
                _canonical_spelling(argument)
                for argument in pointee.argument_types()
            ),
            variadic=bool(pointee.is_function_variadic()),
        )
    except Exception:  # pragma: no cover - defensive
        return None


def _canonical_spelling(type_obj: Any) -> str:
    try:
        spelling = type_obj.get_canonical().spelling
    except Exception:  # pragma: no cover
        spelling = type_obj.spelling
    return "".join(spelling.split())


def _function_signature(unit: Any, function: FunctionRecord) -> _Signature | None:
    cursor = _definition_cursor(unit, function)
    if cursor is None:
        return None
    return _signature_from_type(cursor.type)


def _expression_key(cache: SourceCache, cursor: Any) -> str:
    text = normalize_text(cache.text_for(cursor, 160))
    return _key(text)


def _key(text: str) -> str:
    return "".join(text.split())


def _callee_keys(site: _CallSite, cache: SourceCache) -> tuple[str, str]:
    """Return ``(base, inner)`` keys: ``obj->fn`` -> ``("", "obj->fn")``.

    ``table[i](...)`` yields base ``table``; ``(*fp)(...)`` yields inner ``fp``.
    """

    key = site.callee_text
    base = ""
    inner = ""
    bracket = key.find("[")
    if bracket > 0:
        base = key[:bracket]
    stripped = key
    if stripped.startswith("(*") and stripped.endswith(")"):
        inner = stripped[2:-1]
    elif stripped.startswith("*"):
        inner = stripped[1:]
    return base, inner


def _field_suffix(key: str) -> str:
    """Last struct field name of an access path (subscripts ignored).

    ``devsw[f->major].read`` -> ``read``; ``o->fn`` -> ``fn``.
    """

    stripped = _strip_brackets(key)
    arrow = stripped.rfind("->")
    if arrow >= 0:
        return stripped[arrow + 2 :]
    dot = stripped.rfind(".")
    if dot >= 0:
        return stripped[dot + 1 :]
    return ""


def _strip_brackets(key: str) -> str:
    result: list[str] = []
    depth = 0
    for char in key:
        if char == "[":
            depth += 1
            continue
        if char == "]":
            depth = max(0, depth - 1)
            continue
        if depth == 0:
            result.append(char)
    return "".join(result)


def _location_text(cache: SourceCache, cursor: Any) -> str:
    sloc = cursor_sloc(cache, cursor)
    return f"{sloc.file}:{sloc.line}" if sloc.file else f"line {sloc.line}"


__all__ = ["resolve_call_targets"]
