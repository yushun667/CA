"""Cross-TU callback registration extraction and resolution (task-21)."""

from __future__ import annotations

import dataclasses

from test_analyze_common import (
    FakeParsedUnit,
    FIXTURES,
)

try:  # pragma: no cover - import layout shim
    from test_analyze_common import REPO_ROOT
except ImportError:  # pragma: no cover
    from tests.test_analyze_common import REPO_ROOT  # type: ignore

from ca.analyze.registrations import (
    KIND_ARGUMENT_PASS,
    build_function_signatures,
    KIND_ARRAY_ELEMENT,
    KIND_DESIGNATED,
    KIND_GLOBAL_ASSIGN,
    KIND_RETURN_POINTER,
    KIND_STRUCT_FIELD,
    RegistrationFact,
    analyze_cross_tu,
    extract_registrations,
    extract_target_signatures,
    parse_access_path,
    resolve_cross_tu_targets,
)

REGISTRY = FIXTURES / "ast_crosstu_registry.c"
CALLERS = FIXTURES / "ast_crosstu_callers.c"


def _units() -> tuple[FakeParsedUnit, FakeParsedUnit]:
    return FakeParsedUnit(REGISTRY), FakeParsedUnit(CALLERS)


def _edge_pairs(result) -> set[tuple[str, str]]:
    callers = {}
    for unit in _units():
        for function in unit.functions:
            callers[function.id] = function.name
    return {(callers.get(record.caller_id, record.caller_id), callers.get(record.callee_id, record.callee_id)) for record in result.call_targets}


def test_registration_facts_cover_global_forms():
    registry, callers = _units()
    cache = None
    facts, sites = extract_registrations(registry, cache=cache)
    fact_paths = {fact.object_path: fact for fact in facts}
    # struct field, body array element, designated initializer and global assign
    assert "g_ops_a.fn" in fact_paths
    assert fact_paths["g_ops_a.fn"].registration_kind == KIND_STRUCT_FIELD
    assert "g_table[0]" in fact_paths
    assert fact_paths["g_table[0]"].registration_kind == KIND_ARRAY_ELEMENT
    assert "g_init_table[0]" in fact_paths
    assert fact_paths["g_init_table[0]"].registration_kind == KIND_DESIGNATED
    assert "g_voidcb" in fact_paths
    assert fact_paths["g_voidcb"].registration_kind == KIND_GLOBAL_ASSIGN
    # return-pointer facts live on the registry side; argument-pass facts are
    # produced where the callback is passed to the consuming function.
    registry_kinds = {fact.registration_kind for fact in facts}
    assert KIND_RETURN_POINTER in registry_kinds
    assert KIND_ARGUMENT_PASS not in registry_kinds
    caller_facts, _ = extract_registrations(callers)
    assert any(
        fact.registration_kind == KIND_ARGUMENT_PASS for fact in caller_facts
    )
    # local registration (set_local_only) must not leak into cross-TU facts
    assert all("local" not in fact.object_path for fact in facts)
    # storage-compatible attrs and stable ids
    fact = fact_paths["g_ops_a.fn"]
    payload = dataclasses.asdict(fact)
    for key in (
        "object_path",
        "object_field",
        "target_function_name",
        "target_function_id",
        "target_usr",
        "registration_kind",
        "file",
        "line",
        "tu_id",
        "module",
        "directory",
        "evidence",
        "id",
    ):
        assert key in payload, key
    assert fact.id.startswith("cbreg:")
    assert fact.tu_id == registry.tu.id
    assert fact.object_field == "fn"
    assert fact.target_function_name == "impl_a"
    # call sites from both TUs are extracted for the resolution pass
    _, caller_sites = extract_registrations(callers)
    assert any(site.callee_text.endswith(".fn") for site in caller_sites)


def test_cross_tu_targets_resolve_qualifier_aware():
    result = analyze_cross_tu(_units())
    pairs = _edge_pairs(result)
    expected = {
        ("call_ops_a", "impl_a"),
        ("call_ops_b", "impl_b"),
        ("call_table", "impl_a"),
        ("call_table", "impl_b"),
        ("call_table2", "impl_a"),
        ("call_table2", "impl_b"),
        ("call_init_table", "impl_a"),
        ("call_init_table", "impl_b"),
        ("apply", "caller_impl"),
        ("call_return", "impl_a"),
    }
    assert pairs == expected
    # qualifiers prevent cross-instance confusion and filters drop negatives
    assert ("call_ops_a", "impl_b") not in pairs
    assert ("call_ops_b", "impl_a") not in pairs
    assert not any(caller in {"call_wrong", "call_local"} for caller, _ in pairs)
    # precision/recall against the fixture ground truth
    precision = len(pairs & expected) / len(pairs)
    recall = len(pairs & expected) / len(expected)
    assert precision == 1.0
    assert recall == 1.0


def test_cross_tu_reasons_links_and_target_sets():
    result = analyze_cross_tu(_units())
    assert result.stats["registrations"] > 0
    assert result.stats["indirect_call_sites"] > 0
    assert result.stats["call_targets"] == len(result.call_targets)
    assert len(result.links) == len(result.call_targets)
    names = {record.callee_name for record in result.call_targets}
    assert {"impl_a", "caller_impl"} <= names
    for record in result.call_targets:
        assert record.kind == "indirect_candidate"
        assert "cross-TU" in record.reason
        assert record.reason


def test_cross_tu_deterministic_and_public_paths():
    first = analyze_cross_tu(_units())
    second = analyze_cross_tu(_units())
    assert [record.id for record in first.call_targets] == [
        record.id for record in second.call_targets
    ]
    assert [record.reason for record in first.call_targets] == [
        record.reason for record in second.call_targets
    ]
    path = parse_access_path("devsw[f->major].read")
    assert path.base == "devsw"
    assert path.fields == ("read",)
    registry, _ = _units()
    facts, _ = extract_registrations(registry)
    signatures = extract_target_signatures(registry, {fact.target_function_id for fact in facts})
    assert any(sig is not None for sig in signatures.values())
    # explicit two-step API returns the same candidates as the orchestrator
    from ca.analyze.registrations import build_function_signatures

    result = analyze_cross_tu(_units())
    units = _units()
    functions = {
        function.id: function for unit in units for function in unit.functions
    }
    signatures = build_function_signatures(units)
    call_targets, links = resolve_cross_tu_targets(
        result.call_sites,
        result.registrations,
        functions,
        signatures=signatures,
    )
    assert [record.id for record in call_targets] == [
        record.id for record in result.call_targets
    ]
    assert len(links) == len(call_targets)


def _resolver_bundle():
    units = _units()
    result = analyze_cross_tu(units)
    functions = {
        function.id: function for unit in units for function in unit.functions
    }
    signatures = build_function_signatures(units)
    return result, functions, signatures


def test_logical_dedupe_keeps_deterministic_representative_and_merges_evidence():
    result, functions, signatures = _resolver_bundle()
    base = next(
        site for site in result.call_sites if site.callee_text == "g_ops_a.fn"
    )
    lower = dataclasses.replace(
        base, call_id="call:dedupe-lower", callee_text="g_table[i]"
    )
    raw, _raw_links = resolve_cross_tu_targets(
        [base, lower],
        result.registrations,
        functions,
        signatures=signatures,
        dedupe=False,
    )
    merged, links = resolve_cross_tu_targets(
        [base, lower],
        result.registrations,
        functions,
        signatures=signatures,
        dedupe=True,
    )
    # header-inline duplication here is simulated by two call sites that map to
    # the same logical edge (same caller/callee USR and call file:line).
    assert len(raw) == 3
    assert len(merged) == 2
    impl_a = next(record for record in merged if record.callee_name == "impl_a")
    # Representative is deterministic (smallest call_id), not score-ranked.
    assert impl_a.call_id == min(base.call_id, lower.call_id)
    assert " ; also " in impl_a.reason
    assert "g_table[" in impl_a.reason
    # every matching registration id still links to the retained candidate edge
    assert len(links) == 3
    assert any(target_id == impl_a.id for _fact_id, target_id in links)


def test_dedupe_disabled_preserves_per_site_records():
    result, functions, signatures = _resolver_bundle()
    base = next(
        site for site in result.call_sites if site.callee_text == "g_ops_a.fn"
    )
    lower = dataclasses.replace(
        base, call_id="call:dedupe-lower", callee_text="g_table[i]"
    )
    raw, _links = resolve_cross_tu_targets(
        [base, lower],
        result.registrations,
        functions,
        signatures=signatures,
        dedupe=False,
    )
    assert len(raw) == 3
    assert {record.call_id for record in raw} == {base.call_id, "call:dedupe-lower"}
    assert all(" ; also " not in record.reason for record in raw)


def test_negative_qualifier_and_signature_rules_are_documented():
    result = analyze_cross_tu(_units())
    reasons = [record.reason for record in result.call_targets]
    assert any("qualifier=dynamic" in reason for reason in reasons)
    assert any("qualifier=exact" in reason for reason in reasons)
    # wrong-signature registration must not produce a candidate for call_wrong;
    # the fact itself is still extracted for audit.
    registry, _ = _units()
    facts, _ = extract_registrations(registry)
    assert any(fact.object_path == "g_voidcb" for fact in facts)
