"""C++ virtual dispatch metadata tests (task-37, parser side).

The parser records the metadata virtual-target resolution needs:

- ``TypeRecord.base_types``: direct base type ids (USR based, stable across TUs);
- ``FunctionRecord.is_virtual`` / ``is_pure_virtual`` on methods and
  ``FunctionRecord.overrides``: ids of the directly overridden declarations;
- virtual calls already carry ``CallRecord.kind="virtual"``/``is_virtual`` with
  the static target as ``callee_id``.

All ids follow ``ca.core.ids`` rules, so same-TU and cross-TU resolution can join
on them. No override metadata means no candidate may be invented downstream.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ca.core import ids
from ca.core.schema import CompilationUnit
from ca.parse import ParseOptions, parse_compilation_unit

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def make_unit(filename: str, arguments: tuple[str, ...] = ("-std=c++17",)) -> CompilationUnit:
    file_path = (FIXTURES / filename).resolve()
    return CompilationUnit(
        id=ids.tu_id(str(file_path), str(FIXTURES), arguments),
        file=str(file_path),
        directory=str(FIXTURES),
        repo_root=str(FIXTURES),
        arguments=arguments,
        language="c++",
        source="test",
    )


@pytest.fixture(scope="module")
def base_side():
    return parse_compilation_unit(make_unit("ast_virtual_base.cpp"))


@pytest.fixture(scope="module")
def derived_side():
    return parse_compilation_unit(make_unit("ast_virtual_derived.cpp"))


def functions_by_qname(parsed) -> dict[str, object]:
    return {function.qualified_name: function for function in parsed.functions}


def types_by_qname(parsed) -> dict[str, object]:
    return {record.qualified_name: record for record in parsed.types}


def test_base_types_recorded(base_side, derived_side) -> None:
    for parsed in (base_side, derived_side):
        assert parsed.parse_ok is True, parsed.error
        types = types_by_qname(parsed)
        base = types["ast_virtual::Base"]
        derived = types["ast_virtual::Derived"]
        leaf = types["ast_virtual::Leaf"]
        assert base.base_types == ()
        assert derived.base_types == (base.id,)
        assert leaf.base_types == (derived.id,)
        assert types["ast_virtual::Unrelated"].base_types == ()
    base_types = types_by_qname(base_side)
    derived_types = types_by_qname(derived_side)
    assert base_types["ast_virtual::Base"].id == derived_types["ast_virtual::Base"].id
    assert base_types["ast_virtual::Derived"].id == derived_types["ast_virtual::Derived"].id


def test_virtual_and_override_metadata(derived_side) -> None:
    functions = functions_by_qname(derived_side)
    base_value = functions["ast_virtual::Base::value"]
    derived_value = functions["ast_virtual::Derived::value"]
    leaf_value = functions["ast_virtual::Leaf::value"]
    assert base_value.is_virtual is True
    assert derived_value.is_virtual is True
    assert leaf_value.is_virtual is True
    assert base_value.overrides == ()
    assert derived_value.overrides == (base_value.id,)
    assert leaf_value.overrides == (derived_value.id,)
    plain = functions["ast_virtual::Base::plain"]
    assert plain.is_virtual is False and plain.overrides == ()
    unrelated = functions["ast_virtual::Unrelated::other"]
    assert unrelated.is_virtual is True and unrelated.overrides == ()
    # Override ids are normal FunctionRecord ids from ca.core.ids.
    for function in (base_value, derived_value, leaf_value):
        assert function.id == ids.function_id(
            name=function.name,
            usr=function.usr,
            sloc=function.sloc,
            linkage=function.linkage,
        )


def test_override_metadata_merges_declaration_and_definition(base_side) -> None:
    # Out-of-line definitions replace the in-class declarations but must keep
    # the virtual/override metadata collected from the declaration.
    functions = functions_by_qname(base_side)
    base_value = functions["ast_virtual::Base::value"]
    assert base_value.is_definition is True
    assert base_value.is_virtual is True


def test_virtual_calls_and_no_fake_override(derived_side) -> None:
    calls = {call.callee_qualified_name: call for call in derived_side.calls}
    through_base = calls["ast_virtual::Base::value"]
    assert through_base.kind == "virtual"
    assert through_base.is_virtual is True
    assert through_base.is_resolved is True
    assert through_base.callee_id == functions_by_qname(derived_side)["ast_virtual::Base::value"].id
    unrelated = calls["ast_virtual::Unrelated::other"]
    assert unrelated.kind == "virtual"
    # No overrides exist for Unrelated::other: downstream candidate resolution
    # must produce no virtual_candidate edge for this call site.
    functions = functions_by_qname(derived_side)
    assert not any(
        function.overrides and unrelated.callee_id in function.overrides
        for function in functions.values()
    )


def test_virtual_metadata_with_header_declaration_index() -> None:
    # The task-25 header-declaration mode must keep virtual metadata on header
    # declarations (needed for cross-TU resolution in main_file scope).
    parsed = parse_compilation_unit(
        make_unit("ast_virtual_derived.cpp"),
        ParseOptions(record_scope="main_file", emit_header_declarations=True),
    )
    declarations = {
        function.qualified_name: function
        for function in parsed.functions
        if not function.is_definition
    }
    base_value = declarations["ast_virtual::Base::value"]
    assert base_value.is_virtual is True
    # Derived::value is defined by this TU, so it is a definition record whose
    # override metadata still points at the header declaration of Base::value.
    derived_value = {
        function.qualified_name: function
        for function in parsed.functions
        if function.is_definition
    }["ast_virtual::Derived::value"]
    assert base_value.id in derived_value.overrides


def test_implicit_this_virtual_call_is_marked() -> None:
    parsed = parse_compilation_unit(make_unit("ast_virtual_this.cpp"))
    assert parsed.parse_ok is True, parsed.error
    calls = {call.callee_qualified_name: call for call in parsed.calls}
    implicit = calls["vthis::IService::Open"]
    # Both the implicit and explicit call resolve to IService::Open; the parser
    # distinguishes them so resolution can narrow the implicit receiver.
    kinds = {call.kind for call in parsed.calls if call.callee_name == "Open"}
    assert kinds == {"virtual", "virtual_this"}
    assert implicit.is_virtual is True
