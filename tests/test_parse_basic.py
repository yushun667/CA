"""Acceptance tests for the libclang AST extraction engine (``ca.parse``).

Fixtures live in ``tests/fixtures/ast_*`` and are small hand-written C files.
Expected boundaries are documented in the fixture headers; the tests assert the
contract from ``ca.core.interfaces``/``ca.core.schema``: repo-relative
locations, stable ids from ``ca.core.ids``, call resolution rules, failure
isolation and the detached/picklable ``ParseResult``.
"""

from __future__ import annotations

import os
import pickle
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ca.core import ids
from ca.core.interfaces import ParsedUnit
from ca.core.schema import CompilationUnit, ParseResult, record_to_dict
from ca.parse import ParseOptions, ParsedTranslationUnit, parse_compilation_unit

FIXTURES = Path(__file__).resolve().parent / "fixtures"
REPO_ROOT = Path(__file__).resolve().parents[1]


def make_unit(
    filename: str,
    *,
    arguments: tuple[str, ...] = ("-std=gnu99",),
    repo_root: Path | str = FIXTURES,
    directory: Path | str | None = None,
    driver: str = "",
) -> CompilationUnit:
    file_path = (FIXTURES / filename).resolve()
    root = Path(repo_root).resolve()
    workdir = Path(directory).resolve() if directory is not None else root
    return CompilationUnit(
        id=ids.tu_id(str(file_path), str(workdir), arguments),
        file=str(file_path),
        directory=str(workdir),
        repo_root=str(root),
        arguments=arguments,
        language="c",
        source="test",
        driver=driver,
    )


def by_name(parsed: ParsedTranslationUnit) -> dict[str, object]:
    return {function.name: function for function in parsed.functions}


@pytest.fixture(scope="module")
def basic() -> ParsedTranslationUnit:
    return parse_compilation_unit(make_unit("ast_basic.c"))


# --------------------------------------------------------------------------- #
# Functions
# --------------------------------------------------------------------------- #


def test_parse_ok_and_record_counts(basic: ParsedTranslationUnit) -> None:
    assert basic.parse_ok is True
    assert basic.error == ""
    assert basic.diagnostics == []
    assert isinstance(basic, ParsedTranslationUnit)
    # Includes the definition+declaration of 'add' collapsed into one record.
    assert len(basic.functions) == 8
    assert len(basic.types) == 11
    assert len(basic.fields) == 14
    assert len(basic.calls) == 6
    assert basic.elapsed_ms > 0


def test_function_definition_record(basic: ParsedTranslationUnit) -> None:
    functions = by_name(basic)
    callee = functions["callee"]
    assert callee.is_definition is True
    assert callee.usr == "c:@F@callee"
    assert callee.kind == "function"
    assert callee.qualified_name == "callee"
    assert callee.return_type == "int"
    assert callee.signature == "int callee(int)"
    assert callee.linkage == "external"
    assert callee.is_static is False
    assert callee.is_inline is False
    assert callee.is_variadic is False
    assert callee.storage_class == ""
    assert callee.sloc.file == "ast_basic.c"
    assert callee.sloc.line == 62
    assert callee.sloc.end_line >= callee.sloc.line
    assert [param.name for param in callee.params] == ["x"]
    assert callee.params[0].type_spelling == "int"
    assert callee.params[0].index == 0
    assert callee.parent_id == ""


def test_function_declaration_vs_definition(basic: ParsedTranslationUnit) -> None:
    functions = by_name(basic)
    # 'add' is declared then defined; the definition wins and is deduplicated.
    add = functions["add"]
    assert add.is_definition is True
    assert add.sloc.line == 57
    assert add.params[0].name == "a" and add.params[1].name == "b"
    # Declaration-only entities are still recorded.
    variadic = functions["variadic_decl"]
    assert variadic.is_definition is False
    assert variadic.is_variadic is True
    assert variadic.signature == "int variadic_decl(const char *, ...)"
    declared = functions["declared_in_header"]
    assert declared.is_definition is False
    assert declared.sloc.file == "ast_basic.h"


def test_static_and_header_inline_functions(basic: ParsedTranslationUnit) -> None:
    functions = by_name(basic)
    static_helper = functions["static_helper"]
    assert static_helper.is_static is True
    assert static_helper.linkage == "internal"
    assert static_helper.storage_class == "static"
    assert static_helper.sloc.file == "ast_basic.c"
    inline = functions["header_inline"]
    assert inline.is_definition is True
    assert inline.is_inline is True
    assert inline.is_static is True
    assert inline.linkage == "internal"
    assert inline.sloc.file == "ast_basic.h"
    assert inline.sloc.line == 22


def test_function_ids_follow_core_rules_and_are_stable(basic: ParsedTranslationUnit) -> None:
    again = parse_compilation_unit(make_unit("ast_basic.c"))
    first = {function.name: function.id for function in basic.functions}
    second = {function.name: function.id for function in again.functions}
    assert first == second
    assert len(set(first.values())) == len(first)
    for function in basic.functions:
        expected = ids.function_id(
            name=function.name,
            usr=function.usr,
            sloc=function.sloc,
            linkage=function.linkage,
        )
        assert function.id == expected, function.name
        assert function.id == ids.function_id(
            name=function.name,
            usr=function.usr,
            sloc=function.sloc,
            linkage=function.linkage,
        )


# --------------------------------------------------------------------------- #
# Types and fields
# --------------------------------------------------------------------------- #


def test_type_records(basic: ParsedTranslationUnit) -> None:
    types = {record.name: record for record in basic.types if record.name}
    assert set(types) == {
        "point",
        "point_t",
        "value",
        "color",
        "flags",
        "nested",
        "hdr_record",
        "uint64_alias",
        "hdr_enum",
    }
    point = types["point"]
    assert point.kind == "struct"
    assert point.is_definition is True
    assert point.size_bytes == 8
    assert point.align_bytes == 4
    assert point.sloc.file == "ast_basic.c"
    assert point.sloc.line == 15
    typedef = types["point_t"]
    assert typedef.kind == "typedef"
    assert typedef.underlying_type == "struct point"
    assert typedef.size_bytes == 8
    enum = types["color"]
    assert enum.kind == "enum"
    assert enum.underlying_type == "unsigned int"
    header_record = types["hdr_record"]
    assert (header_record.sloc.file, header_record.size_bytes) == ("ast_basic.h", 16)
    assert header_record.align_bytes == 8
    union = types["value"]
    assert union.kind == "union" and union.size_bytes == 4
    for record in basic.types:
        assert record.id == ids.type_id(
            name=record.name,
            usr=record.usr,
            sloc=record.sloc,
            kind=record.kind,
        )


def test_anonymous_types(basic: ParsedTranslationUnit) -> None:
    anonymous = [record for record in basic.types if record.is_anonymous]
    assert len(anonymous) == 2
    assert {record.kind for record in anonymous} == {"struct", "union"}
    for record in anonymous:
        assert record.name == ""
        assert record.sloc.file == "ast_basic.c"
        assert record.is_definition is True
        assert record.size_bytes > 0


def test_field_offsets_bitfields_and_ids(basic: ParsedTranslationUnit) -> None:
    fields = {(field.type_id, field.name): field for field in basic.fields}
    flags = next(record for record in basic.types if record.name == "flags")
    bit_a = fields[(flags.id, "a")]
    assert (bit_a.offset_bits, bit_a.bit_width) == (0, 3)
    assert bit_a.is_bitfield is True and bit_a.index == 0
    bit_b = fields[(flags.id, "b")]
    assert (bit_b.offset_bits, bit_b.bit_width) == (3, 5)
    int_c = fields[(flags.id, "c")]
    assert (int_c.offset_bits, int_c.bit_width, int_c.is_bitfield) == (32, -1, False)
    assert int_c.index == 2

    point = next(record for record in basic.types if record.name == "point")
    assert fields[(point.id, "y")].offset_bits == 32
    nested = next(record for record in basic.types if record.name == "nested")
    assert fields[(nested.id, "variant")].offset_bits == 64
    assert fields[(nested.id, "variant")].type_spelling.startswith("union (unnamed")

    type_ids = {record.id for record in basic.types}
    for field in basic.fields:
        assert field.id == ids.field_id(
            type_id_value=field.type_id,
            name=field.name,
            sloc=field.sloc,
            index=field.index,
        )
        assert field.type_id in type_ids, field.name
        assert field.sloc.file in {"ast_basic.c", "ast_basic.h"}


# --------------------------------------------------------------------------- #
# Calls
# --------------------------------------------------------------------------- #


def test_call_records_and_resolution(basic: ParsedTranslationUnit) -> None:
    functions = by_name(basic)
    caller = functions["caller"]
    calls = {call.sloc.line: call for call in basic.calls}
    assert set(calls) == {70, 71, 72, 73, 74, 82}
    # Lines 70-74 live in caller(); the macro-generated call is in macro_user().
    assert {call.caller_id for call in basic.calls if call.sloc.line != 82} == {caller.id}
    assert calls[82].caller_id == functions["macro_user"].id

    direct = calls[70]
    assert direct.callee_name == "callee"
    assert direct.callee_usr == "c:@F@callee"
    assert direct.callee_id == functions["callee"].id
    assert direct.kind == "direct" and direct.is_resolved is True
    assert direct.arg_count == 1

    indirect = calls[71]
    assert indirect.kind == "indirect"
    assert indirect.is_resolved is False
    assert indirect.callee_id == ""
    assert indirect.callee_usr == ""
    assert indirect.callee_name == "fp"
    assert indirect.arg_count == 1

    assert calls[72].callee_id == functions["static_helper"].id
    assert calls[72].callee_usr == "c:ast_basic.c@F@static_helper"
    assert calls[73].callee_id == functions["add"].id
    assert calls[73].arg_count == 2
    assert calls[74].callee_id == functions["header_inline"].id
    # Macro-generated call is attributed to the expansion site (line 82).
    assert calls[82].callee_id == functions["callee"].id
    assert calls[82].sloc.file == "ast_basic.c"
    assert calls[82].sloc.line == 82
    assert calls[82].is_resolved is True

    for call in basic.calls:
        assert call.id == ids.call_id(
            caller_id=call.caller_id,
            callee_usr=call.callee_usr,
            callee_name=call.callee_name,
            sloc=call.sloc,
        )
        assert call.tu_id == basic.tu.id


def test_macro_expansion_flag_is_opt_in() -> None:
    unit = make_unit("ast_basic.c")
    default = parse_compilation_unit(unit)
    assert all(call.is_macro_expansion is False for call in default.calls)
    flagged = parse_compilation_unit(unit, ParseOptions(detect_macro_expansions=True))
    macro_calls = {call.sloc.line: call for call in flagged.calls if call.is_macro_expansion}
    assert set(macro_calls) == {82}
    assert macro_calls[82].callee_name == "callee"


def test_duplicate_basename_static_functions_get_distinct_ids() -> None:
    parsed_a = parse_compilation_unit(make_unit("ast_static_a/unit.c"))
    parsed_b = parse_compilation_unit(make_unit("ast_static_b/unit.c"))
    helper_a = by_name(parsed_a)["helper"]
    helper_b = by_name(parsed_b)["helper"]
    # libclang USR only carries the basename; internal linkage must add the file.
    assert helper_a.usr == helper_b.usr == "c:unit.c@F@helper"
    assert helper_a.linkage == helper_b.linkage == "internal"
    assert helper_a.id != helper_b.id
    assert by_name(parsed_a)["unit_a"].id != by_name(parsed_b)["unit_b"].id
    call_a = next(call for call in parsed_a.calls if call.callee_name == "helper")
    call_b = next(call for call in parsed_b.calls if call.callee_name == "helper")
    assert call_a.callee_id == helper_a.id
    assert call_b.callee_id == helper_b.id


# --------------------------------------------------------------------------- #
# Live AST, detached result, path filtering
# --------------------------------------------------------------------------- #


def test_live_ast_and_detached_parse_result(basic: ParsedTranslationUnit) -> None:
    assert isinstance(basic, ParsedUnit)
    clang_tu = basic.clang_tu()
    assert clang_tu is not None
    assert clang_tu.spelling.endswith("ast_basic.c")
    caller = by_name(basic)["caller"]
    cursor = basic.definition_cursor(caller.id)
    assert cursor is not None and cursor.spelling == "caller"
    assert basic.definition_cursor("fn:does-not-exist") is None

    result = basic.parse_result()
    assert isinstance(result, ParseResult)
    assert result.tu is basic.tu
    assert result.parse_ok is True
    assert [record.id for record in result.functions] == [record.id for record in basic.functions]
    restored = pickle.loads(pickle.dumps(result))
    assert isinstance(restored, ParseResult)
    assert restored.parse_ok is True
    assert [record.id for record in restored.calls] == [record.id for record in basic.calls]
    # Records must be plain data usable by the JSONL/store layers.
    payload = record_to_dict(result.functions[0])
    assert payload["kind"] == "function"
    assert payload["schema"]


def test_only_repo_root_entities_are_recorded(basic: ParsedTranslationUnit) -> None:
    for record in [*basic.functions, *basic.types, *basic.fields, *basic.calls]:
        assert not os.path.isabs(record.sloc.file), record.id
        assert record.sloc.file
    assert {record.sloc.file for record in basic.functions} == {"ast_basic.c", "ast_basic.h"}


def test_sloc_paths_are_relative_to_repo_root() -> None:
    rooted = parse_compilation_unit(make_unit("ast_basic.c", repo_root=REPO_ROOT))
    files = {record.sloc.file for record in rooted.functions}
    assert files == {"tests/fixtures/ast_basic.c", "tests/fixtures/ast_basic.h"}
    assert by_name(rooted)["callee"].sloc.file == "tests/fixtures/ast_basic.c"


# --------------------------------------------------------------------------- #
# Argument handling and failure isolation
# --------------------------------------------------------------------------- #


def test_working_directory_is_applied_for_relative_includes() -> None:
    unit = make_unit(
        "ast_include/main.c",
        arguments=("-std=gnu99", "-I."),
        directory=FIXTURES / "ast_include",
    )
    parsed = parse_compilation_unit(unit)
    assert parsed.parse_ok is True, parsed.error
    functions = by_name(parsed)
    assert {"include_user", "fixture_only"} <= set(functions)
    assert functions["fixture_only"].sloc.file == "ast_include/fixture_only.h"
    call = next(call for call in parsed.calls if call.callee_name == "fixture_only")
    assert call.is_resolved is True
    assert call.callee_id == functions["fixture_only"].id


def test_output_and_build_arguments_are_filtered() -> None:
    source = str((FIXTURES / "ast_basic.c").resolve())
    arguments = (
        "-std=gnu99",
        "-c",
        "-o",
        "build/ast_basic.o",
        "-MD",
        "-MF",
        "build/ast_basic.d",
        "-MT",
        "build/ast_basic.o",
        source,
    )
    parsed = parse_compilation_unit(make_unit("ast_basic.c", arguments=arguments))
    assert parsed.parse_ok is True
    assert parsed.diagnostics == []
    assert len(parsed.functions) == 8


def test_error_translation_unit_is_isolated() -> None:
    parsed = parse_compilation_unit(make_unit("ast_errors.c"))
    assert parsed.parse_ok is False
    assert "missing_type_t" in parsed.error
    functions = by_name(parsed)
    assert "good_function" in functions
    assert "broken_function" in functions
    types = {record.name for record in parsed.types}
    assert "ok_type" in types
    errors = [record for record in parsed.diagnostics if record.severity == "error"]
    assert errors
    assert errors[0].sloc.file == "ast_errors.c"
    assert errors[0].sloc.line == 17


def test_werror_is_relaxed_but_recorded() -> None:
    unit = make_unit("ast_werror.c", arguments=("-std=gnu99", "-Wall", "-Werror"))
    relaxed = parse_compilation_unit(unit)
    assert relaxed.parse_ok is True
    assert "uses_werror" in by_name(relaxed)
    severities = [record.severity for record in relaxed.diagnostics]
    assert "warning" in severities
    notes = [record for record in relaxed.diagnostics if record.severity == "note"]
    assert notes and "-Werror" in notes[0].message

    strict = parse_compilation_unit(unit, ParseOptions(relax_on_errors=False))
    assert strict.parse_ok is False
    assert "unused variable" in strict.error
    assert "uses_werror" in by_name(strict)


def test_missing_source_file_fails_cleanly() -> None:
    missing = FIXTURES / "does_not_exist.c"
    unit = CompilationUnit(
        id=ids.tu_id(str(missing), str(FIXTURES), ()),
        file=str(missing),
        directory=str(FIXTURES),
        repo_root=str(FIXTURES),
    )
    parsed = parse_compilation_unit(unit)
    assert parsed.parse_ok is False
    assert "source file not found" in parsed.error
    assert parsed.functions == [] and parsed.calls == []
    assert parsed.parse_result().parse_ok is False


def test_options_mapping_and_validation() -> None:
    unit = make_unit("ast_basic.c")
    minimal = parse_compilation_unit(unit, {"include_declarations": False})
    names = set(by_name(minimal))
    assert "callee" in names
    assert "variadic_decl" not in names
    assert "declared_in_header" not in names
    with pytest.raises(TypeError):
        parse_compilation_unit(unit, 42)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Apple SDK sysroot fallback (environment dependent; skipped elsewhere)
# --------------------------------------------------------------------------- #


def _apple_sdk_path_or_skip() -> str:
    if sys.platform != "darwin" or shutil.which("xcrun") is None:
        pytest.skip("Apple SDK sysroot fallback is macOS-only")
    completed = subprocess.run(
        ["xcrun", "--show-sdk-path"], capture_output=True, text=True, check=False
    )
    sdk = completed.stdout.strip()
    if completed.returncode != 0 or not sdk or not os.path.isdir(sdk):
        pytest.skip("xcrun --show-sdk-path unavailable")
    return sdk


def test_darwin_sysroot_is_added_when_missing(tmp_path: Path) -> None:
    _apple_sdk_path_or_skip()
    probe = tmp_path / "probe.c"
    probe.write_text(
        '#include <stdio.h>\nint probe(void) { printf("x"); return 0; }\n',
        encoding="utf-8",
    )
    unit = CompilationUnit(
        id=ids.tu_id(str(probe), str(tmp_path), ("-std=gnu99",)),
        file=str(probe),
        directory=str(tmp_path),
        repo_root=str(tmp_path),
        arguments=("-std=gnu99",),
        language="c",
        source="test",
    )
    parsed = parse_compilation_unit(unit)
    assert parsed.parse_ok is True, parsed.error
    assert "probe" in by_name(parsed)

    without_sysroot = parse_compilation_unit(unit, ParseOptions(add_sysroot=False))
    assert without_sysroot.parse_ok is False
    assert any("stdio.h" in record.message for record in without_sysroot.diagnostics)


# --------------------------------------------------------------------------- #
# Symlink-normalized repository roots
# --------------------------------------------------------------------------- #


def _copy_basic_fixture(target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for name in ("ast_basic.c", "ast_basic.h"):
        shutil.copyfile(FIXTURES / name, target / name)


def test_repo_root_and_source_paths_with_symlinks(tmp_path: Path) -> None:
    real_repo = tmp_path / "real_repo"
    _copy_basic_fixture(real_repo)
    link_repo = tmp_path / "link_repo"
    try:
        link_repo.symlink_to(real_repo, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - platform dependent
        pytest.skip("symlinks are not available")

    # repo_root resolved, source passed through the unresolved symlink spelling.
    resolved_root_unit = CompilationUnit(
        id="tu:symlink-resolved",
        file=str(link_repo / "ast_basic.c"),
        directory=str(link_repo),
        repo_root=os.path.realpath(real_repo),
        arguments=("-std=gnu99",),
        source="test",
    )
    parsed = parse_compilation_unit(resolved_root_unit)
    assert parsed.parse_ok is True, parsed.error
    assert len(parsed.functions) == 8
    assert by_name(parsed)["callee"].sloc.file == "ast_basic.c"

    # repo_root unresolved, source reported through the resolved spelling.
    unresolved_root_unit = CompilationUnit(
        id="tu:symlink-unresolved",
        file=os.path.realpath(real_repo / "ast_basic.c"),
        directory=str(real_repo),
        repo_root=str(link_repo),
        arguments=("-std=gnu99",),
        source="test",
    )
    parsed = parse_compilation_unit(unresolved_root_unit)
    assert parsed.parse_ok is True, parsed.error
    assert len(parsed.functions) == 8
    assert {record.sloc.file for record in parsed.functions} == {"ast_basic.c", "ast_basic.h"}


# --------------------------------------------------------------------------- #
# Real compile-database argument handling (xv6 regression coverage)
# --------------------------------------------------------------------------- #


def test_compdb_relative_source_operand_is_filtered() -> None:
    # A real compile_commands.json command line repeats the source as a
    # positional operand, relative to ``directory``; libclang must not receive it
    # a second time or it fails to load the TU.
    relative_operand = make_unit(
        "ast_basic.c",
        arguments=("-std=gnu99", "-c", "-o", "ast_basic.o", "ast_basic.c"),
    )
    parsed = parse_compilation_unit(relative_operand)
    assert parsed.parse_ok is True, parsed.error
    assert len(parsed.functions) == 8
    absolute_operand = make_unit(
        "ast_basic.c",
        arguments=(
            "-std=gnu99",
            "-c",
            "-o",
            "ast_basic.o",
            str((FIXTURES / "ast_basic.c").resolve()),
        ),
    )
    parsed = parse_compilation_unit(absolute_operand)
    assert parsed.parse_ok is True, parsed.error
    assert len(parsed.functions) == 8


def test_link_only_flags_are_ignored_without_relaxing_werror() -> None:
    unit = make_unit(
        "ast_basic.c",
        arguments=(
            "-std=gnu99",
            "-Wall",
            "-Werror",
            "-fno-pie",
            "-no-pie",
            "-c",
            "-o",
            "ast_basic.o",
            "ast_basic.c",
        ),
    )
    parsed = parse_compilation_unit(unit)
    assert parsed.parse_ok is True, parsed.error
    assert not [record for record in parsed.diagnostics if record.severity in ("error", "fatal")]
    # Link-only flags are dropped up front, so no degraded retry is needed.
    assert not [record for record in parsed.diagnostics if record.severity == "note"]


def test_cross_target_and_abi_inference() -> None:
    from ca.parse import libclang_parser as parser_impl

    rv64 = CompilationUnit(
        id="tu:riscv64",
        file="/tmp/riscv.c",
        directory="/tmp",
        repo_root="/tmp",
        arguments=("-march=rv64gc", "-mcmodel=medany", "-mno-relax", "-std=gnu99"),
        driver="/opt/xpack/bin/riscv-none-elf-gcc",
    )
    assert parser_impl._inferred_target(rv64, rv64.arguments) == "riscv64-unknown-elf"
    prepared = parser_impl._prepare_arguments(rv64, ParseOptions(), "/tmp/riscv.c")
    assert "--target=riscv64-unknown-elf" in prepared
    assert "-mabi=lp64d" in prepared
    # Real machine flags stay: clang must validate the actual target semantics.
    for flag in ("-march=rv64gc", "-mcmodel=medany", "-mno-relax"):
        assert flag in prepared

    rv32 = CompilationUnit(
        id="tu:riscv32",
        file="/tmp/riscv32.c",
        directory="/tmp",
        repo_root="/tmp",
        arguments=("-march=rv32imac", "-mabi=ilp32"),
        driver="/opt/xpack/bin/riscv-none-elf-gcc",
    )
    assert parser_impl._inferred_target(rv32, rv32.arguments) == "riscv32-unknown-elf"
    prepared32 = parser_impl._prepare_arguments(rv32, ParseOptions(), "/tmp/riscv32.c")
    assert "--target=riscv32-unknown-elf" in prepared32
    assert "-mabi=lp64d" not in prepared32

    aarch64 = CompilationUnit(
        id="tu:aarch64",
        file="/tmp/a.c",
        directory="/tmp",
        repo_root="/tmp",
        arguments=("-std=gnu99",),
        driver="/opt/cross/bin/aarch64-linux-gnu-gcc",
    )
    prepared_aarch64 = parser_impl._prepare_arguments(aarch64, ParseOptions(), "/tmp/a.c")
    assert "--target=aarch64-linux-gnu" in prepared_aarch64


def test_unused_command_line_diagnostic_is_not_a_parse_failure() -> None:
    from types import SimpleNamespace

    from ca.parse import cursor_utils as cu
    from ca.parse import libclang_parser as parser_impl

    raw = SimpleNamespace(
        severity=3,
        spelling="argument unused during compilation: '-no-pie' [-Wunused-command-line-argument]",
        location=None,
    )
    fake_tu = SimpleNamespace(diagnostics=[raw])
    unit = make_unit("ast_basic.c")
    diagnostics, errors = parser_impl._collect_diagnostics(
        fake_tu, unit, cu.PathMapper(str(FIXTURES))
    )
    assert errors == []
    assert diagnostics[0].severity == "warning"
