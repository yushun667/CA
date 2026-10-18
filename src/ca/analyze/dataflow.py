"""Intraprocedural resource-state data-flow analysis on the CFG.

State is tracked per resource-target expression (``p``, ``&lk``, ``fd``):

* forward reachability from the function entry detects releases that run on a
  path where the resource was already released (``double_release``) and
  releases with no acquiring path in this function (``unreleased``);
* forward reachability from each acquire site detects paths that reach the
  exit while the resource is still live (``leak``).

The analysis is a bounded worklist over ``(block, status)`` states. It is
intentionally conservative: results are only reported when a path exists, so
cross-function ownership transfer, function pointers, macros and exceptions
are not modeled (an acquire passed to another function looks live).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any

from ca.analyze.facts import PendingRecord, resource_target_key
from ca.core.schema import (
    BranchRecord,
    CfgBlockRecord,
    CfgEdgeRecord,
    FindingRecord,
    ResourceOpRecord,
)

_EDGE_ORDER = {
    "true": 0,
    "false": 1,
    "switch_case": 2,
    "switch_default": 3,
    "back": 4,
    "unconditional": 5,
}
_MAX_STATES = 20000
_MAX_FINDINGS = 200


@dataclass
class _OpSite:
    op: ResourceOpRecord
    block_index: int
    seq: int
    ordinal: int


def analyze_flows(
    *,
    function_id: str,
    blocks: list[CfgBlockRecord],
    edges: list[CfgEdgeRecord],
    ops: list[PendingRecord],
    branches: list[BranchRecord],
    transferred: set[str],
    local_targets: set[str],
    acquired_kinds: set[str],
    result_vars: dict[str, str],
    function_name: str,
    param_names: set[str],
    entry_index: int,
    exit_index: int,
) -> list[FindingRecord]:
    block_ids: list[str] = [block.id for block in blocks]
    succs = _successors(blocks, edges)
    sites = _group_sites(ops)
    null_tests = _null_test_edges(branches, blocks)
    var_keys_all = sorted(sites)
    needs_guards = any(
        any(site.op.op == "release" for site in sites[var])
        and any(site.op.op in ("acquire", "copy") for site in sites[var])
        for var in var_keys_all
    )
    guards = (
        _guard_contexts(branches, blocks, edges, entry_index)
        if needs_guards
        else {}
    )
    needs_cycles = any(
        sum(1 for site in sites[var] if site.op.op == "release") >= 2
        for var in var_keys_all
    )
    cyclic = _blocks_in_cycles(succs) if needs_cycles else set()
    findings: list[FindingRecord] = []
    var_keys = sorted(sites)

    for var in var_keys:
        var_sites = sites[var]
        findings.extend(
            _analyze_releases(
                var,
                var_sites,
                succs,
                entry_index,
                exit_index,
                block_ids,
                local_targets,
                acquired_kinds,
                cyclic,
                guards,
            )
        )
        if var not in transferred:
            findings.extend(
                _analyze_acquires(
                    var,
                    var_sites,
                    succs,
                    exit_index,
                    block_ids,
                    function_id,
                    null_tests.get(var, {}),
                    guards,
                    local_targets,
                    branches,
                    blocks,
                    result_vars,
                    function_name,
                    param_names,
                )
            )
        if len(findings) >= _MAX_FINDINGS:
            break

    findings.sort(
        key=lambda item: (item.kind, item.acquire_id, item.message)
    )
    return findings[:_MAX_FINDINGS]


# --------------------------------------------------------------------------- #
# Setup
# --------------------------------------------------------------------------- #


def _successors(
    blocks: list[CfgBlockRecord], edges: list[CfgEdgeRecord]
) -> dict[int, list[tuple[int, str]]]:
    index_of = {block.id: block.index for block in blocks}
    outgoing: dict[int, list[tuple[int, int, str]]] = {}
    for edge in edges:
        src = index_of.get(edge.src)
        dst = index_of.get(edge.dst)
        if src is None or dst is None:
            continue
        outgoing.setdefault(src, []).append((_EDGE_ORDER.get(edge.kind, 99), dst, edge.kind))
    result: dict[int, list[tuple[int, str]]] = {}
    for src, items in outgoing.items():
        items.sort()
        result[src] = [(dst, kind) for _rank, dst, kind in items]
    return result


def _null_test_edges(
    branches: list[BranchRecord], blocks: list[CfgBlockRecord]
) -> dict[str, dict[int, str]]:
    """Map ``var -> {branch_block: edge kind that means "not acquired"}``.

    Allocators and ``open``-style APIs return a sentinel (NULL, 0, -1) on
    failure; the common ``if (!p) return`` / ``if (fd < 0) return`` guards must
    not be reported as leaks. This is a purely syntactic convention check on
    the recorded branch condition text.
    """

    index_of = {block.id: block.index for block in blocks}
    result: dict[str, dict[int, str]] = {}
    for branch in branches:
        block_index = index_of.get(branch.block_id)
        if block_index is None:
            continue
        for var in _simple_vars(branch.condition):
            direction = _failure_direction(branch.condition, var)
            if direction:
                result.setdefault(var, {}).setdefault(block_index, direction)
                break
    return result


def _simple_vars(condition: str) -> list[str]:
    compact = "".join(condition.split())
    names: list[str] = []
    token = ""
    for char in compact:
        if char.isalnum() or char == "_":
            token += char
        else:
            if token and not token[0].isdigit():
                names.append(token)
            token = ""
    if token and not token[0].isdigit():
        names.append(token)
    return names


_FAILURE_LEFT = ("<0", "<=-1")
_FAILURE_RIGHT = (">0",)
_FAILURE_EQ_ZERO = ("==0", "==NULL", "==nullptr")
_FAILURE_NE_ZERO = ("!=0", "!=NULL", "!=nullptr")


def _failure_direction(condition: str, var: str) -> str:
    compact = "".join(condition.split())
    if compact == f"!{var}":
        return "true"
    err_checks = (
        f"IS_ERR({var})",
        f"IS_ERR_OR_NULL({var})",
        f"IS_ERR({var})==1",
    )
    if any(compact == check for check in err_checks):
        return "true"
    if compact.startswith("!") and any(
        compact == "!" + check for check in err_checks
    ):
        return "false"
    if any(check in compact for check in err_checks):
        # e.g. unlikely(IS_ERR(p)) -> macro spelling is preserved in text
        return "true"
    if f"!IS_ERR({var})" in compact or f"!IS_ERR_OR_NULL({var})" in compact:
        return "false"
    if compact in (f"{var}!=-1", f"-1!={var}"):
        return "false"
    if compact in (f"{var}==-1", f"-1=={var}"):
        return "true"
    if (compact.endswith("<0") or compact.endswith("<=-1")) and var in _simple_vars(
        condition
    ):
        return "true"
    for suffix in _FAILURE_EQ_ZERO:
        if compact == f"{var}{suffix}" or compact == f"0=={var}" or compact == f"NULL=={var}" or compact == f"nullptr=={var}":
            return "true"
    for suffix in _FAILURE_NE_ZERO:
        if compact == f"{var}{suffix}" or compact == f"0!={var}" or compact == f"NULL!={var}" or compact == f"nullptr!={var}":
            return "false"
    if compact == f"{var}<0" or compact == f"0>{var}" or compact == f"{var}<=-1" or compact == f"-1>={var}" or compact == f"-1=={var}":
        return "true"
    if compact == f"{var}>=0" or compact == f"0<={var}" or compact == f"{var}>-1" or compact == f"-1<={var}" or compact == f"-1!={var}":
        return "false"
    return ""


def _group_sites(ops: list[PendingRecord]) -> dict[str, list[_OpSite]]:
    grouped: dict[str, list[_OpSite]] = {}
    for pending in ops:
        record: ResourceOpRecord = pending.record
        key = resource_target_key(record.target)
        if key.startswith("&") and record.resource_kind != "lock":
            # Out-parameter handles are passed by address at acquire time and
            # by value at release time (``__pidfd_prepare(&pidfile)`` vs
            # ``fput(pidfile)``); compare the pointee.
            key = key[1:]
        if not key:
            continue
        if record.op not in ("acquire", "release", "copy"):
            continue
        grouped.setdefault(key, []).append(
            _OpSite(record, pending.block_index, pending.seq, pending.ordinal)
        )
    for items in grouped.values():
        items.sort(key=lambda site: (site.block_index, site.seq, site.ordinal))
    return grouped


def _leak_target_is_trackable(
    target: str,
    local_targets: set[str],
    resource_kind: str,
    param_names: set[str],
) -> bool:
    key = resource_target_key(target)
    simple = key.lstrip("&")
    if not simple.isidentifier():
        return False
    if resource_kind == "lock":
        # Lock objects are borrowed pointers: acquire/release discipline is
        # local to the function even when the lock lives in a struct/param.
        # Global locks, however, are often locked/unlocked in paired helper
        # functions (``foo_start`` / ``foo_stop``); do not report those.
        return simple in local_targets or simple in param_names
    # For memory/refcount/irq/integer out-parameter handles (``&dev``) only
    # function-local variables are actionable.
    return simple in local_targets


def _target_is_simple(target: str) -> bool:
    key = resource_target_key(target)
    if not key:
        return False
    simple = key.lstrip("&")
    return simple.isidentifier()


def _blocks_in_cycles(succs: dict[int, list[tuple[int, str]]]) -> set[int]:
    """Blocks that can reach themselves through at least one edge."""

    cyclic: set[int] = set()
    for start in sorted(succs):
        stack = [dst for dst, _kind in succs.get(start, ())]
        seen: set[int] = set()
        while stack:
            current = stack.pop()
            if current == start:
                cyclic.add(start)
                break
            if current in seen:
                continue
            seen.add(current)
            stack.extend(dst for dst, _kind in succs.get(current, ()))
    return cyclic


def _target_is_local(op: ResourceOpRecord, local_targets: set[str]) -> bool:
    key = resource_target_key(op.target)
    if not key:
        return False
    simple = key.lstrip("&")
    return simple.isidentifier() and simple in local_targets


def _guard_contexts(
    branches: list[BranchRecord],
    blocks: list[CfgBlockRecord],
    edges: list[CfgEdgeRecord],
    entry_index: int,
) -> dict[int, set[tuple[str, str]]]:
    """For each block, the ``(condition, edge polarity)`` guards on all paths.

    A block is guarded by ``(cond, "true")`` when the true-successor of a
    branch whose condition text is ``cond`` dominates it. Two call sites that
    share a guard are paired: under that condition both always run together.
    """

    index_of = {block.id: block.index for block in blocks}
    all_blocks = set(index_of.values())
    if not all_blocks:
        return {}
    predecessors: dict[int, set[int]] = {index: set() for index in all_blocks}
    for edge in edges:
        src = index_of.get(edge.src)
        dst = index_of.get(edge.dst)
        if src is not None and dst is not None:
            predecessors[dst].add(src)
    dominators: dict[int, set[int]] = {
        index: (set(all_blocks) if index != entry_index else {entry_index})
        for index in all_blocks
    }
    changed = True
    while changed:
        changed = False
        for index in sorted(all_blocks):
            if index == entry_index:
                continue
            preds = predecessors[index]
            if not preds:
                new = {index}
            else:
                new = set(all_blocks)
                for pred in preds:
                    new &= dominators[pred]
                new.add(index)
            if new != dominators[index]:
                dominators[index] = new
                changed = True
    guards: dict[int, set[tuple[str, str]]] = {}
    for branch in branches:
        condition = "".join(branch.condition.split())
        if not condition:
            continue
        targets = []
        true_index = index_of.get(branch.true_block)
        if true_index is not None:
            targets.append(("true", true_index))
        false_index = index_of.get(branch.false_block)
        if false_index is not None:
            targets.append(("false", false_index))
        for polarity, target in targets:
            for index in all_blocks:
                if target in dominators[index]:
                    guards.setdefault(index, set()).add((condition, polarity))
    return guards


# --------------------------------------------------------------------------- #
# Release-side analysis: double release and unmatched release
# --------------------------------------------------------------------------- #


def _analyze_releases(
    var: str,
    var_sites: list[_OpSite],
    succs: dict[int, list[tuple[int, str]]],
    entry_index: int,
    exit_index: int,
    block_ids: list[str],
    local_targets: set[str],
    acquired_kinds: set[str],
    cyclic_blocks: set[int],
    guards: dict[int, set[tuple[str, str]]],
) -> list[FindingRecord]:
    release_sites = [site for site in var_sites if site.op.op == "release"]
    if not release_sites:
        return []
    by_block: dict[int, list[_OpSite]] = {}
    for site in var_sites:
        by_block.setdefault(site.block_index, []).append(site)
    for items in by_block.values():
        items.sort(key=lambda site: (site.seq, site.ordinal))

    queue: deque[tuple[int, int]] = deque([(entry_index, 0)])
    visited: set[tuple[int, int]] = {(entry_index, 0)}
    seen_double: dict[str, _OpSite] = {}
    seen_live: set[str] = set()
    seen_dead: dict[str, _OpSite] = {}
    processed_blocks = 0

    while queue:
        block_index, status = queue.popleft()
        processed_blocks += 1
        if processed_blocks > _MAX_STATES:
            break
        for site in by_block.get(block_index, ()):
            if site.op.op in ("acquire", "copy"):
                status = 1
            elif site.op.op == "release":
                if status == 2:
                    seen_double.setdefault(site.op.id, site)
                elif status == 1:
                    seen_live.add(site.op.id)
                    status = 2
                else:
                    seen_dead.setdefault(site.op.id, site)
        for next_block, _edge_kind in succs.get(block_index, ()):
            state = (next_block, status)
            if state in visited:
                continue
            visited.add(state)
            queue.append(state)

    findings: list[FindingRecord] = []
    for site in release_sites:
        op = site.op
        if (
            op.id in seen_double
            and site.block_index not in cyclic_blocks
            and not guards.get(site.block_index)
            and _target_is_simple(op.target)
        ):
            findings.append(
                FindingRecord(
                    id="",
                    function_id=op.function_id,
                    kind="double_release",
                    resource_kind=op.resource_kind,
                    message=(
                        f"{op.api_name} releases {op.target or var!r} on a path where "
                        f"it was already released"
                    ),
                    severity="warning",
                    acquire_id=op.id,
                    release_ids=(op.id,),
                    path_blocks=(),
                    evidence=(f"matcher={op.matcher}", f"release={op.sloc.file}:{op.sloc.line}"),
                )
            )
        elif (
            op.id in seen_dead
            and op.id not in seen_live
            and _target_is_local(op, local_targets)
            and op.resource_kind in acquired_kinds
        ):
            # Releases of parameters / fields usually hand ownership to this
            # function from a different one; without cross-function analysis
            # those are not actionable, so only local targets are reported.
            findings.append(
                FindingRecord(
                    id="",
                    function_id=op.function_id,
                    kind="unreleased",
                    resource_kind=op.resource_kind,
                    message=(
                        f"{op.api_name} releases {op.target or var!r} but no acquiring "
                        f"call reaches it in this function"
                    ),
                    severity="info",
                    acquire_id=op.id,
                    release_ids=(op.id,),
                    path_blocks=(),
                    evidence=(f"matcher={op.matcher}", f"release={op.sloc.file}:{op.sloc.line}"),
                )
            )
    return findings


# --------------------------------------------------------------------------- #
# Acquire-side analysis: leak paths
# --------------------------------------------------------------------------- #


def _analyze_acquires(
    var: str,
    var_sites: list[_OpSite],
    succs: dict[int, list[tuple[int, str]]],
    exit_index: int,
    block_ids: list[str],
    function_id: str,
    null_edges: dict[int, str],
    guards: dict[int, set[tuple[str, str]]],
    local_targets: set[str],
    branches: list[BranchRecord],
    blocks: list[CfgBlockRecord],
    result_vars: dict[str, str],
    function_name: str,
    param_names: set[str],
) -> list[FindingRecord]:
    acquire_sites = [site for site in var_sites if site.op.op in ("acquire", "copy")]
    if not acquire_sites:
        return []
    by_block: dict[int, list[_OpSite]] = {}
    for site in var_sites:
        by_block.setdefault(site.block_index, []).append(site)
    for items in by_block.values():
        items.sort(key=lambda site: (site.seq, site.ordinal))

    if (
        function_name.endswith("lock")
        and not any(site.op.op == "release" for site in var_sites)
    ):
        # Split lock helpers (``foo_lock()`` / ``foo_unlock()``) release the
        # lock in their counterpart function; a per-function "leak" is not
        # actionable.
        acquire_sites = [
            site for site in acquire_sites if site.op.resource_kind != "lock"
        ]
        if not acquire_sites:
            return []
    release_guards: set[tuple[str, str]] = set()
    for site in var_sites:
        if site.op.op == "release":
            release_guards |= guards.get(site.block_index, set())

    findings: list[FindingRecord] = []
    for acquire in acquire_sites:
        if not _target_is_simple(acquire.op.target):
            # Storing into ``*out`` / ``obj->field`` transfers ownership to the
            # caller or a global; a function-local leak is not actionable.
            continue
        if acquire.op.api_name.startswith("devm_"):
            # devm_* allocations are owned and released by the driver core.
            continue
        if acquire.op.resource_kind == "refcount" and any(
            site.op.op == "release" for site in var_sites
        ):
            # Reference counts are frequently re-assigned and handed over;
            # without value/alias tracking a per-path leak is not actionable
            # when the function does drop a reference somewhere.
            continue
        if not _leak_target_is_trackable(
            acquire.op.target,
            local_targets,
            acquire.op.resource_kind,
            param_names,
        ):
            # Only function-local pointers (or ``&lock`` style handles) are
            # actionable; globals are persistent by design.
            continue
        acquire_guards = guards.get(acquire.block_index, set())
        if acquire_guards and acquire_guards & release_guards:
            # Same guard expression protects both calls (for example
            # ``if (panicking == 0) acquire(...); ... if (panicking == 0)
            # release(...)``): treat them as paired.
            continue
        failure_edges: dict[int, str] = {}
        status_var = result_vars.get(acquire.op.id)
        if status_var:
            failure_edges = _status_failure_edges(branches, blocks, status_var)
        path = _find_leak_path(
            acquire,
            by_block,
            succs,
            exit_index,
            block_ids,
            null_edges,
            failure_edges,
        )
        if path is None:
            continue
        op = acquire.op
        block_path = tuple(block_ids[index] for index in path)
        findings.append(
            FindingRecord(
                id="",
                function_id=function_id,
                kind="leak",
                resource_kind=op.resource_kind,
                message=(
                    f"{op.api_name} acquiring {op.target!r} may leak: a path reaches "
                    f"the function exit without a release"
                ),
                severity="warning",
                acquire_id=op.id,
                release_ids=(),
                path_blocks=block_path,
                evidence=(
                    f"matcher={op.matcher}",
                    f"acquire={op.sloc.file}:{op.sloc.line}",
                    f"exit_block={block_ids[exit_index] if block_ids else ''}",
                ),
            )
        )
        if len(findings) >= _MAX_FINDINGS:
            break
    return findings


def _status_failure_edges(
    branches: list[BranchRecord],
    blocks: list[CfgBlockRecord],
    var: str,
) -> dict[int, str]:
    """Branch edges that mean "the acquire call reported failure".

    Kernel APIs such as ``request_irq`` return an errno while putting the
    handle in an out-parameter; ``if (ret) goto err;`` makes the failure path
    stop counting as an acquired-but-unreleased resource.
    """

    index_of = {block.id: block.index for block in blocks}
    result: dict[int, str] = {}
    for branch in branches:
        block_index = index_of.get(branch.block_id)
        if block_index is None:
            continue
        compact = "".join(branch.condition.split())
        polarity = ""
        if compact == var or compact in (f"{var}!=0", f"{var}>0", f"{var}<0"):
            polarity = "true"
        elif compact == f"!{var}" or compact in (f"{var}==0", f"{var}<=0"):
            polarity = "false"
        if polarity:
            result.setdefault(block_index, polarity)
    return result


def _find_leak_path(
    acquire: _OpSite,
    by_block: dict[int, list[_OpSite]],
    succs: dict[int, list[tuple[int, str]]],
    exit_index: int,
    block_ids: list[str],
    null_edges: dict[int, str],
    failure_edges: dict[int, str] | None = None,
) -> list[int] | None:
    start_block = acquire.block_index
    start_seq = acquire.seq
    queue: deque[tuple[int, int, int]] = deque([(start_block, 1, start_seq)])
    # State key is (block, status); the start state processes only sites that
    # follow the acquiring call inside the same block.
    visited: set[tuple[int, int]] = set()
    predecessor: dict[tuple[int, int], tuple[int, int] | None] = {}
    leak_state: tuple[int, int] | None = None
    steps = 0

    while queue:
        block_index, status, seq_floor = queue.popleft()
        steps += 1
        if steps > _MAX_STATES:
            return None
        state = (block_index, status)
        if state in visited:
            continue
        visited.add(state)
        predecessor.setdefault(state, None)
        for site in by_block.get(block_index, ()):
            if seq_floor >= 0 and site.seq >= 0 and site.seq <= seq_floor:
                continue
            if site.op.op in ("acquire", "copy"):
                status = 1
            elif site.op.op == "release":
                status = 0
        if block_index == exit_index and status == 1:
            leak_state = (block_index, status)
            break
        for next_block, edge_kind in succs.get(block_index, ()):
            next_status = status
            if null_edges.get(block_index) == edge_kind or (
                failure_edges and failure_edges.get(block_index) == edge_kind
            ):
                next_status = 0
            next_state = (next_block, next_status)
            if next_state in visited:
                continue
            if next_state not in predecessor:
                predecessor[next_state] = (block_index, 1 if status else 0)
            queue.append((next_block, next_status, -1))

    if leak_state is None:
        return None
    path_states: list[tuple[int, int]] = []
    current: tuple[int, int] | None = leak_state
    while current is not None:
        path_states.append(current)
        current = predecessor.get(current)
    path_states.reverse()
    return [state[0] for state in path_states]


__all__ = ["analyze_flows"]
