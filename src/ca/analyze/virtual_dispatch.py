"""C++ virtual dispatch candidate resolution (same-TU part).

Contract with the parse module: ``FunctionRecord.overrides`` carries the ids of
the directly overridden declarations (``clang_getOverriddenCursors`` based) and
``CallRecord.kind == "virtual"`` / ``is_virtual`` marks virtual call sites whose
``callee_id`` is the statically resolved method.

This module turns that metadata into **candidate target sets**, never certain
edges and never scored predictions:

- for a virtual call with static target ``T``, every known override of ``T``
  (transitively through the override chain, depth <= 3) is a possible dynamic
  target;
- ``reason`` records the override relationship and depth as evidence; depth is
  not converted into a confidence/probability;
- no override metadata means no candidate (the direct call edge stays as-is);
- results are deterministic, deduplicated and capped per call site/function.

Cross-TU resolution is done at store query time from the persisted
``virtual_overrides`` metadata; this module covers overrides visible inside the
TU being analyzed (headers included in that TU), which is the immediate result
``analyze_function`` returns.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterator

from ca.core import ids as core_ids
from ca.core.schema import CallTargetRecord, FunctionRecord

_MAX_DEPTH = 3


def _call_records(unit: Any, function: FunctionRecord) -> Iterator[Any]:
    for call in getattr(unit, "calls", ()) or ():
        if call.caller_id == function.id:
            yield call


def _descendant_types(unit: Any, root_id: str) -> set[str]:
    """Type ids derived from ``root_id`` using local ``TypeRecord.base_types``."""

    types = list(getattr(unit, "types", ()) or ())
    if not root_id:
        return set()
    children: dict[str, list[str]] = defaultdict(list)
    known: set[str] = {root_id}
    for record in types:
        known.add(record.id)
        for base_id in getattr(record, "base_types", ()) or ():
            children[str(base_id)].append(record.id)
    descendants: set[str] = {root_id}
    stack = [root_id]
    while stack:
        current = stack.pop()
        for child in children.get(current, ()):
            if child not in descendants:
                descendants.add(child)
                stack.append(child)
    return descendants & known


def _override_index(functions: list[FunctionRecord]) -> dict[str, list[FunctionRecord]]:
    index: dict[str, list[FunctionRecord]] = defaultdict(list)
    for candidate in functions:
        for base_id in candidate.overrides:
            index[base_id].append(candidate)
    for bucket in index.values():
        bucket.sort(key=lambda record: record.id)
    return index


def resolve_virtual_targets(
    unit: Any,
    function: FunctionRecord,
    *,
    max_targets: int = 64,
    max_depth: int = _MAX_DEPTH,
) -> list[CallTargetRecord]:
    """Deterministic ``virtual_candidate`` edges for one function's virtual calls."""

    functions = list(getattr(unit, "functions", ()) or ())
    if not functions:
        return []
    by_id = {record.id: record for record in functions}
    override_index = _override_index(functions)
    receiver_descendants: dict[str, set[str]] = {}
    records: list[CallTargetRecord] = []
    for call in _call_records(unit, function):
        if not (getattr(call, "kind", "") == "virtual" or getattr(call, "is_virtual", False)):
            continue
        static_id = getattr(call, "callee_id", "") or ""
        if not static_id or static_id not in by_id:
            continue
        static_target = by_id[static_id]
        seen_ids: set[str] = {static_id}
        frontier = [static_id]
        depth = 0
        while frontier and depth < max_depth:
            depth += 1
            next_frontier: list[str] = []
            for base_id in frontier:
                for candidate in override_index.get(base_id, ()):
                    if candidate.id in seen_ids:
                        continue
                    if getattr(call, "kind", "") == "virtual_this":
                        caller_type = function.parent_id
                        if caller_type:
                            allowed = receiver_descendants.setdefault(
                                caller_type, _descendant_types(unit, caller_type)
                            )
                            candidate_type = getattr(candidate, "parent_id", "") or ""
                            if candidate_type and allowed and candidate_type not in allowed:
                                continue
                    seen_ids.add(candidate.id)
                    next_frontier.append(candidate.id)
                    records.append(
                        CallTargetRecord(
                            id=core_ids.call_target_id(
                                call.id, candidate.id, "virtual_candidate"
                            ),
                            call_id=call.id,
                            caller_id=function.id,
                            callee_id=candidate.id,
                            callee_name=candidate.name,
                            kind="virtual_candidate",
                            reason=(
                                f"virtual override {candidate.qualified_name} of "
                                f"{static_target.qualified_name} "
                                f"(override depth {depth})"
                            ),
                            sloc=call.sloc,
                        )
                    )
            frontier = next_frontier

    records.sort(
        key=lambda record: (
            record.sloc.line,
            record.sloc.col,
            record.call_id,
            record.callee_id,
        )
    )
    unique: list[CallTargetRecord] = []
    seen_pairs: set[tuple[str, str]] = set()
    for record in records:
        key = (record.call_id, record.callee_id)
        if key in seen_pairs:
            continue
        seen_pairs.add(key)
        unique.append(record)
        if len(unique) >= max_targets:
            break
    return unique
