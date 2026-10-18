"""Header function-declaration index tests (task-25 item 4, P0 query UX).

Large-corpus indexing uses ``record_scope="main_file"`` so per-TU output stays
small. Query UX still needs repo-header function declarations (``find_symbol``
should report declaration locations and "definition missing"). The additive
``ParseOptions.emit_header_declarations`` switch indexes them:

- header *declarations* become ``FunctionRecord``s with ``is_definition=False``,
  signature/location/USR/params intact;
- header *definitions* stay excluded (unchanged main_file semantics);
- functions defined in the TU's own file are not duplicated (definition wins);
- declaration ids follow the same ``ca.core.ids`` rules and are stable across
  TUs, so store dedupe and call resolution keep working;
- ``header_declaration_limit`` bounds header-heavy C++ TUs (audit note emitted).

Default ``emit_header_declarations=False`` is fully backwards compatible.
"""

from __future__ import annotations

import os
import pickle
from pathlib import Path

import pytest

from ca.core import ids
from ca.core.schema import CompilationUnit
from ca.parse import ParseOptions, parse_compilation_unit

FIXTURES = Path(__file__).resolve().parent / "fixtures"
HEADER = "ast_header_decls.h"


def make_unit(filename: str) -> CompilationUnit:
    file_path = (FIXTURES / filename).resolve()
    arguments = ("-std=gnu99",)
    return CompilationUnit(
        id=ids.tu_id(str(file_path), str(FIXTURES), arguments),
        file=str(file_path),
        directory=str(FIXTURES),
        repo_root=str(FIXTURES),
        arguments=arguments,
        language="c",
        source="test",
    )


def functions_by_name(parsed) -> dict[str, list]:
    grouped: dict[str, list] = {}
    for function in parsed.functions:
        grouped.setdefault(function.name, []).append(function)
    return grouped


@pytest.fixture(scope="module")
def definition_side():
    return parse_compilation_unit(
        make_unit("ast_header_decls.c"),
        ParseOptions(record_scope="main_file", emit_header_declarations=True),
    )


@pytest.fixture(scope="module")
def user_side():
    return parse_compilation_unit(
        make_unit("ast_header_decls_user.c"),
        ParseOptions(record_scope="main_file", emit_header_declarations=True),
    )


def test_default_main_file_scope_has_no_header_declarations() -> None:
    # Backwards compatibility: the new switch is off by default.
    parsed = parse_compilation_unit(
        make_unit("ast_header_decls.c"), ParseOptions(record_scope="main_file")
    )
    names = set(functions_by_name(parsed))
    assert {"hdr_defined_in_c", "hdr_use_declared"} <= names
    assert "hdr_declared_only" not in names
    assert "hdr_register_cb" not in names
    assert "hdr_inline_impl" not in names


def test_header_declarations_are_indexed(definition_side) -> None:
    assert definition_side.parse_ok is True, definition_side.error
    grouped = functions_by_name(definition_side)
    assert "hdr_declared_only" in grouped
    declaration = grouped["hdr_declared_only"][0]
    assert declaration.is_definition is False
    assert declaration.sloc.file == HEADER
    assert declaration.sloc.line > 0
    assert declaration.usr == "c:@F@hdr_declared_only"
    assert declaration.signature == "int hdr_declared_only(int, const char *)"
    assert [param.type_spelling for param in declaration.params] == ["int", "const char *"]
    assert declaration.linkage == "external"
    assert declaration.id == ids.function_id(
        name=declaration.name,
        usr=declaration.usr,
        sloc=declaration.sloc,
        linkage=declaration.linkage,
    )
    assert "hdr_register_cb" in grouped
    assert grouped["hdr_register_cb"][0].is_definition is False
    assert grouped["hdr_register_cb"][0].params[0].type_spelling == "hdr_cb_t"


def test_header_definitions_stay_excluded_and_definitions_win(definition_side) -> None:
    grouped = functions_by_name(definition_side)
    # Header inline definition is not indexed as a declaration nor definition.
    assert "hdr_inline_impl" not in grouped
    # hdr_defined_in_c: declaration in header replaced by the .c definition.
    records = grouped["hdr_defined_in_c"]
    assert len(records) == 1
    assert records[0].is_definition is True
    assert records[0].sloc.file == "ast_header_decls.c"
    # No duplicate ids inside the TU.
    assert len({record.id for record in definition_side.functions}) == len(
        definition_side.functions
    )


def test_header_declaration_ids_stable_across_tus(definition_side, user_side) -> None:
    assert user_side.parse_ok is True, user_side.error
    first_all = {record.name: record for record in definition_side.functions}
    second_decls = {
        record.name: record
        for record in user_side.functions
        if not record.is_definition
    }
    second_all = {record.name: record for record in user_side.functions}
    # Declaration-only functions: same ids/USRs in both TUs.
    for name in ("hdr_declared_only", "hdr_register_cb"):
        assert first_all[name].id == second_decls[name].id, name
        assert first_all[name].usr == second_decls[name].usr
        assert second_decls[name].is_definition is False
    # In the definition TU hdr_defined_in_c is a definition; in the user TU the
    # same id is present but only as a declaration (defined elsewhere).
    assert first_all["hdr_defined_in_c"].is_definition is True
    assert second_decls["hdr_defined_in_c"].is_definition is False
    assert first_all["hdr_defined_in_c"].id == second_decls["hdr_defined_in_c"].id
    user_defined = {
        record.name: record
        for record in user_side.functions
        if record.is_definition
    }
    assert "hdr_defined_in_c" not in user_defined
    # The user TU still records its own definition and call resolution works.
    assert "hdr_user" in user_defined
    calls = {call.callee_name: call for call in user_side.calls}
    assert calls["hdr_defined_in_c"].callee_id == second_decls["hdr_defined_in_c"].id


def test_call_resolution_matches_declaration_record(definition_side) -> None:
    declarations = {
        record.name: record for record in definition_side.functions
    }
    calls = {call.callee_name: call for call in definition_side.calls}
    assert calls["hdr_declared_only"].callee_id == declarations["hdr_declared_only"].id
    assert calls["hdr_declared_only"].is_resolved is True
    # Calls into the inline header definition still resolve by USR even though the
    # header definition is not recorded in main_file scope.
    assert calls["hdr_inline_impl"].is_resolved is True
    assert calls["hdr_inline_impl"].callee_usr == "c:ast_header_decls.h@F@hdr_inline_impl"


def test_emit_header_declarations_is_noop_for_all_scope() -> None:
    plain = parse_compilation_unit(
        make_unit("ast_header_decls.c"), ParseOptions(record_scope="all")
    )
    explicit = parse_compilation_unit(
        make_unit("ast_header_decls.c"),
        ParseOptions(record_scope="all", emit_header_declarations=True),
    )
    # record_scope="all" already records repo-header declarations; the new switch
    # must not change the record set there.
    assert [record.id for record in plain.functions] == [
        record.id for record in explicit.functions
    ]


def test_header_declaration_limit_truncates_with_note() -> None:
    parsed = parse_compilation_unit(
        make_unit("ast_header_decls.c"),
        ParseOptions(
            record_scope="main_file",
            emit_header_declarations=True,
            header_declaration_limit=1,
        ),
    )
    # With limit=1 only the first header declaration is emitted (hdr_defined_in_c,
    # later replaced by its .c definition in this TU), the rest are cut off.
    declarations = [record for record in parsed.functions if not record.is_definition]
    assert len(declarations) <= 1
    notes = [record.message for record in parsed.diagnostics if record.severity == "note"]
    assert any("header declarations truncated" in note for note in notes)

    unlimited = parse_compilation_unit(
        make_unit("ast_header_decls.c"),
        ParseOptions(
            record_scope="main_file",
            emit_header_declarations=True,
            header_declaration_limit=0,
        ),
    )
    unlimited_declarations = [
        record for record in unlimited.functions if not record.is_definition
    ]
    assert len(unlimited_declarations) >= 2
    assert not any(
        "header declarations truncated" in record.message for record in unlimited.diagnostics
    )


def test_header_declaration_records_are_picklable_and_small(definition_side) -> None:
    result = definition_side.parse_result()
    restored = pickle.loads(pickle.dumps(result))
    assert len(restored.functions) == len(definition_side.functions)
    assert any(not record.is_definition for record in restored.functions)
    # Types/fields are unaffected by the declaration switch (still full set).
    assert definition_side.types
