"""C++ language coverage tests for the libclang extraction engine (task P2-3).

The translation units are parsed from a *real* compile database: each test
session invokes the local ``clang++`` (``-MJ``) to capture the actual argv, then
loads it through :func:`ca.build.load_compilation_database`; a second test drives
a CMake-generated ``compile_commands.json``. No flags are guessed in the test.

Coverage: namespaces, class/struct, constructors/destructors, inline/const/
static member functions, overloads, virtual dispatch, templates (declaration +
instantiation call probe), operator overload, anonymous namespace, and
cross-translation-unit id stability. Known precision limits that this suite
pins down are listed in the module docstrings of the fixtures.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ca.build import load_compilation_database
from ca.core import ids
from ca.core.schema import FunctionRecord, TypeRecord
from ca.parse import ParsedTranslationUnit, parse_compilation_unit

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CPP_SOURCES = (FIXTURES / "ast_cpp_basic.cpp", FIXTURES / "ast_cpp_second.cpp")
CPP_COMPILER = shutil.which("clang++") or shutil.which("clang")


def _require_cpp_compiler() -> str:
    if CPP_COMPILER is None:
        pytest.skip("no clang++ available to capture a real compile database")
    return CPP_COMPILER


def _line_of(filename: str, needle: str) -> int:
    """1-based source line of the first line containing ``needle``."""

    text = (FIXTURES / filename).read_text(encoding="utf-8")
    for number, line in enumerate(text.splitlines(), start=1):
        if needle in line:
            return number
    raise AssertionError(f"{needle!r} not found in {filename}")


def _write_mj_compile_database(directory: Path, sources: tuple[Path, ...]) -> Path:
    """Capture the real clang++ argv for each source via ``-MJ``."""

    compiler = _require_cpp_compiler()
    entries = []
    for index, source in enumerate(sources):
        mj_path = directory / f"entry-{index}.json"
        object_path = directory / f"{source.stem}.o"
        completed = subprocess.run(
            [
                compiler,
                "-std=c++17",
                "-Wall",
                "-Wextra",
                "-I",
                str(FIXTURES),
                "-c",
                str(source),
                "-o",
                str(object_path),
                "-MJ",
                str(mj_path),
            ],
            cwd=str(directory),
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr[-2000:]
        raw = mj_path.read_text(encoding="utf-8").strip()
        entries.append(json.loads(raw.rstrip(", \n")))
    compdb_path = directory / "compile_commands.json"
    compdb_path.write_text(json.dumps(entries, indent=1), encoding="utf-8")
    return compdb_path


@pytest.fixture(scope="module")
def cpp_units(tmp_path_factory: pytest.TempPathFactory) -> list:
    workdir = tmp_path_factory.mktemp("cpp_mj_compdb")
    compdb_path = _write_mj_compile_database(workdir, CPP_SOURCES)
    units = load_compilation_database(str(compdb_path), str(FIXTURES))
    assert len(units) == len(CPP_SOURCES)
    return units


@pytest.fixture(scope="module")
def cpp_parsed(cpp_units: list) -> list[ParsedTranslationUnit]:
    return [parse_compilation_unit(unit) for unit in cpp_units]


def functions_by_qname(parsed: ParsedTranslationUnit) -> dict[str, FunctionRecord]:
    result: dict[str, FunctionRecord] = {}
    for function in parsed.functions:
        result.setdefault(function.qualified_name, function)
    return result


def types_by_qname(parsed: ParsedTranslationUnit) -> dict[str, TypeRecord]:
    return {record.qualified_name or record.name: record for record in parsed.types}


def calls_by_name(parsed: ParsedTranslationUnit, name: str) -> list:
    return [call for call in parsed.calls if call.callee_name == name]


# --------------------------------------------------------------------------- #
# Real compile database
# --------------------------------------------------------------------------- #


def test_real_compile_database_units(cpp_units: list) -> None:
    assert {os.path.basename(unit.file) for unit in cpp_units} == {
        "ast_cpp_basic.cpp",
        "ast_cpp_second.cpp",
    }
    for unit in cpp_units:
        assert unit.source == "compile_commands"
        assert unit.language == "c++"
        assert unit.arguments
        assert "clang" in os.path.basename(unit.driver)
        assert os.path.isabs(unit.file) and os.path.isabs(unit.repo_root)
        # The captured argv really contains the C++ standard and -c/-o entries.
        assert any(arg.startswith("-std=c++") for arg in unit.arguments)
        assert "-c" in unit.arguments


def test_parse_ok_for_cpp_tus(cpp_parsed: list[ParsedTranslationUnit]) -> None:
    for parsed in cpp_parsed:
        assert parsed.parse_ok is True, parsed.error
        assert parsed.error == ""
        fatal = [d for d in parsed.diagnostics if d.severity in ("error", "fatal")]
        assert fatal == []
    basic, second = cpp_parsed
    assert len(basic.functions) > 10
    assert len(basic.types) > 3
    assert len(basic.calls) > 5
    assert "second_entry" in functions_by_qname(second)


def test_no_absolute_paths_and_header_entities_are_recorded(
    cpp_parsed: list[ParsedTranslationUnit],
) -> None:
    basic = cpp_parsed[0]
    files = {record.sloc.file for record in [*basic.functions, *basic.types, *basic.fields]}
    assert "ast_cpp_basic.hpp" in files
    assert "ast_cpp_basic.cpp" in files
    for record in [*basic.functions, *basic.types, *basic.fields, *basic.calls]:
        assert record.sloc.file
        assert not os.path.isabs(record.sloc.file)


# --------------------------------------------------------------------------- #
# Entities: namespaces, classes, methods, constructors, templates
# --------------------------------------------------------------------------- #


def test_namespace_qualified_names(cpp_parsed: list[ParsedTranslationUnit]) -> None:
    basic = cpp_parsed[0]
    functions = functions_by_qname(basic)
    assert "ast_cpp::helper" in functions
    assert "ast_cpp::Base::value" in functions
    assert "ast_cpp::Widget::value" in functions
    assert "ast_cpp::Base::Base" in functions
    assert "ast_cpp::Base::~Base" in functions
    assert "ast_cpp::max_of" in functions
    assert "ast_cpp::Box::get" in functions
    assert "cpp_entry" in functions

    helper = functions["ast_cpp::helper"]
    assert helper.name == "helper"
    assert helper.kind == "function"
    assert helper.is_definition is True
    assert helper.sloc.file == "ast_cpp_basic.cpp"
    assert helper.sloc.line == _line_of("ast_cpp_basic.cpp", "int helper(int x)")
    assert helper.return_type == "int"
    assert helper.signature == "int helper(int)"
    assert helper.usr == "c:@N@ast_cpp@F@helper#I#"


def test_class_and_struct_type_records(cpp_parsed: list[ParsedTranslationUnit]) -> None:
    types = types_by_qname(cpp_parsed[0])
    point = types["ast_cpp::Point"]
    assert point.kind == "struct"
    assert point.is_definition is True
    assert (point.size_bytes, point.align_bytes) == (8, 4)
    assert point.sloc.file == "ast_cpp_basic.hpp"
    assert point.sloc.line == _line_of("ast_cpp_basic.hpp", "struct Point {")

    base = types["ast_cpp::Base"]
    assert base.kind == "class"
    assert base.is_definition is True
    assert base.size_bytes > 0 and base.align_bytes > 0
    widget = types["ast_cpp::Widget"]
    assert widget.kind == "class" and widget.is_definition is True
    # Inheritance relation itself is not in the schema (known contract limit);
    # both classes must still be independently recorded.
    assert widget.id != base.id

    color = types["ast_cpp::Color"]
    assert color.kind == "enum"
    assert color.underlying_type == "unsigned char"
    assert color.size_bytes == 1


def test_using_alias_maps_to_typedef(cpp_parsed: list[ParsedTranslationUnit]) -> None:
    alias = types_by_qname(cpp_parsed[0])["ast_cpp::PointAlias"]
    assert alias.kind == "typedef"
    assert alias.is_definition is True
    assert alias.underlying_type == "Point"
    assert (alias.size_bytes, alias.align_bytes) == (8, 4)


def test_constructor_and_destructor_records(cpp_parsed: list[ParsedTranslationUnit]) -> None:
    basic = cpp_parsed[0]
    functions = functions_by_qname(basic)
    constructor = functions["ast_cpp::Base::Base"]
    assert constructor.kind == "constructor"
    assert constructor.name == "Base"
    assert constructor.return_type == ""
    assert constructor.signature == "Base(int)"
    assert constructor.is_definition is True
    assert constructor.sloc.line == _line_of("ast_cpp_basic.cpp", "Base::Base(int seed)")
    assert constructor.parent_id == types_by_qname(basic)["ast_cpp::Base"].id
    assert [param.type_spelling for param in constructor.params] == ["int"]

    destructor = functions["ast_cpp::Base::~Base"]
    assert destructor.kind == "destructor"
    assert destructor.name == "~Base"
    assert destructor.return_type == ""
    assert destructor.signature == "~Base()"

    widget_ctor = functions["ast_cpp::Widget::Widget"]
    assert widget_ctor.kind == "constructor" and widget_ctor.is_definition is True


def test_member_function_kinds_flags_and_parent_ids(
    cpp_parsed: list[ParsedTranslationUnit],
) -> None:
    basic = cpp_parsed[0]
    functions = functions_by_qname(basic)
    types = types_by_qname(basic)
    widget_id = types["ast_cpp::Widget"].id
    base_id = types["ast_cpp::Base"].id

    base_value = functions["ast_cpp::Base::value"]
    assert base_value.kind == "method"
    assert base_value.signature == "int value(int) const"
    assert base_value.parent_id == base_id
    assert base_value.is_definition is True
    assert base_value.sloc.line == _line_of("ast_cpp_basic.cpp", "int Base::value")

    inline_seed = functions["ast_cpp::Base::seed"]
    assert inline_seed.is_inline is True
    assert inline_seed.is_definition is True
    assert inline_seed.sloc.file == "ast_cpp_basic.hpp"
    assert inline_seed.parent_id == base_id

    static_live = functions["ast_cpp::Base::live_count"]
    assert static_live.kind == "method"
    assert static_live.is_static is True
    assert static_live.is_definition is True
    assert static_live.parent_id == base_id

    widget_bump = functions["ast_cpp::Widget::bump"]
    assert widget_bump.is_inline is True
    assert widget_bump.is_definition is True
    assert widget_bump.sloc.file == "ast_cpp_basic.hpp"
    assert widget_bump.parent_id == widget_id

    overloads = [f for f in basic.functions if f.name == "value" and f.parent_id == widget_id]
    assert len(overloads) == 2
    assert {f.signature for f in overloads} == {
        "int value(int) const",
        "int value(int, int) const",
    }
    assert len({f.id for f in overloads}) == 2
    assert len({f.usr for f in overloads}) == 2

    for function in basic.functions:
        assert function.id == ids.function_id(
            name=function.name,
            usr=function.usr,
            sloc=function.sloc,
            linkage=function.linkage,
        )


def test_templates_declarations(cpp_parsed: list[ParsedTranslationUnit]) -> None:
    basic = cpp_parsed[0]
    functions = functions_by_qname(basic)
    types = types_by_qname(basic)

    template_function = functions["ast_cpp::max_of"]
    assert template_function.kind == "function"
    assert template_function.is_definition is True
    assert "@FT@" in template_function.usr
    assert template_function.sloc.file == "ast_cpp_basic.hpp"
    assert [param.type_spelling for param in template_function.params] == ["T", "T"]

    box = types["ast_cpp::Box"]
    assert box.kind == "class"
    assert box.is_definition is True
    assert box.size_bytes == -1  # uninstantiated template has no layout

    box_get = functions["ast_cpp::Box::get"]
    assert box_get.kind == "method"
    assert box_get.parent_id == box.id
    assert box_get.is_inline is True
    assert box_get.signature == "T get() const"


def test_anonymous_namespace_function(cpp_parsed: list[ParsedTranslationUnit]) -> None:
    basic = cpp_parsed[0]
    functions = functions_by_qname(basic)
    local = functions["(anonymous namespace)::local_helper"]
    assert local.kind == "function"
    assert local.linkage == "internal"
    assert local.sloc.file == "ast_cpp_basic.cpp"
    assert local.id == ids.function_id(
        name=local.name,
        usr=local.usr,
        sloc=local.sloc,
        linkage=local.linkage,
    )
    # Anonymous-namespace USRs only embed the file basename; the repo-relative
    # file component keeps ids unique even for same-named files.
    assert "ast_cpp_basic.cpp" in local.usr


# --------------------------------------------------------------------------- #
# Calls: methods, free functions, virtual dispatch, overloads, templates
# --------------------------------------------------------------------------- #


def test_free_function_and_member_calls(cpp_parsed: list[ParsedTranslationUnit]) -> None:
    basic = cpp_parsed[0]
    functions = functions_by_qname(basic)
    cpp_entry = functions["cpp_entry"]

    helper_call = calls_by_name(basic, "helper")
    assert len(helper_call) == 1
    call = helper_call[0]
    assert call.caller_id == cpp_entry.id
    assert call.callee_qualified_name == "ast_cpp::helper"
    assert call.callee_id == functions["ast_cpp::helper"].id
    assert call.kind == "direct" and call.is_resolved is True
    assert call.arg_count == 1

    inline_call = calls_by_name(basic, "bump")[0]
    assert inline_call.callee_id == functions["ast_cpp::Widget::bump"].id
    assert inline_call.sloc.line == _line_of("ast_cpp_basic.cpp", "widget.bump();")
    assert inline_call.kind == "direct"

    # Constructor invocation in the member-initializer list is a call site too.
    ctor_calls = [
        call for call in calls_by_name(basic, "Base") if call.caller_id == functions["ast_cpp::Widget::Widget"].id
    ]
    assert len(ctor_calls) == 1
    assert ctor_calls[0].callee_id == functions["ast_cpp::Base::Base"].id
    assert ctor_calls[0].callee_qualified_name == "ast_cpp::Base::Base"


def test_virtual_dispatch_and_overload_resolution(
    cpp_parsed: list[ParsedTranslationUnit],
) -> None:
    basic = cpp_parsed[0]
    functions = functions_by_qname(basic)
    cpp_entry = functions["cpp_entry"]
    entry_calls = [call for call in basic.calls if call.caller_id == cpp_entry.id]

    derived_virtual = next(
        call for call in entry_calls if call.callee_qualified_name == "ast_cpp::Widget::value"
        and call.sloc.line == _line_of("ast_cpp_basic.cpp", "widget.value(2)")
    )
    assert derived_virtual.kind == "virtual"
    assert derived_virtual.is_virtual is True
    assert derived_virtual.is_resolved is True
    assert derived_virtual.callee_id == functions["ast_cpp::Widget::value"].id

    base_virtual = next(
        call for call in entry_calls if call.callee_qualified_name == "ast_cpp::Base::value"
    )
    assert base_virtual.kind == "virtual" and base_virtual.is_virtual is True
    assert base_virtual.callee_id == functions["ast_cpp::Base::value"].id

    widget_id = types_by_qname(basic)["ast_cpp::Widget"].id
    two_arg = next(
        call for call in entry_calls
        if call.callee_name == "value" and call.arg_count == 2 and call.callee_qualified_name == "ast_cpp::Widget::value"
    )
    overload_id = next(
        f.id for f in basic.functions
        if f.name == "value" and f.parent_id == widget_id and "int, int" in f.signature
    )
    assert two_arg.callee_id == overload_id
    assert two_arg.kind == "direct"


def test_static_member_function_calls(cpp_parsed: list[ParsedTranslationUnit]) -> None:
    basic = cpp_parsed[0]
    functions = functions_by_qname(basic)
    entry_calls = [
        call for call in basic.calls if call.caller_id == functions["cpp_entry"].id
    ]
    live = next(call for call in entry_calls if call.callee_name == "live_count")
    assert live.callee_id == functions["ast_cpp::Base::live_count"].id
    assert live.is_resolved is True
    created = next(call for call in entry_calls if call.callee_name == "created")
    assert created.callee_id == functions["ast_cpp::Widget::created"].id


def test_operator_overload_call(cpp_parsed: list[ParsedTranslationUnit]) -> None:
    basic = cpp_parsed[0]
    functions = functions_by_qname(basic)
    operator_calls = calls_by_name(basic, "operator+")
    assert len(operator_calls) == 1
    call = operator_calls[0]
    assert call.callee_qualified_name == "ast_cpp::Vec2::operator+"
    assert call.callee_id == functions["ast_cpp::Vec2::operator+"].id
    assert call.kind == "direct" and call.is_resolved is True
    # Member operator calls include the implicit object argument.
    assert call.arg_count == 2


def test_template_instantiation_call_probe(cpp_parsed: list[ParsedTranslationUnit]) -> None:
    """libclang exposes template *instantiations* only through call references.

    The call site resolves to a specialization USR (stable, usable as a
    placeholder id), while no FunctionRecord exists for the specialized body in
    this TU; this is the documented template limitation.
    """

    basic = cpp_parsed[0]
    function_usrs = {function.usr for function in basic.functions}
    function_ids = {function.id for function in basic.functions}

    max_calls = calls_by_name(basic, "max_of")
    assert max_calls, "template instantiation call should be visible"
    for call in max_calls:
        assert call.is_resolved is True
        assert call.callee_qualified_name == "ast_cpp::max_of"
        assert "@F@max_of<" in call.callee_usr  # specialization USR
        assert call.callee_id
        assert call.callee_usr not in function_usrs
        assert call.callee_id not in function_ids

    box_calls = [
        call for call in calls_by_name(basic, "get") if "Box>#" in call.callee_usr
    ]
    assert box_calls, "instantiated class-template member call should be visible"
    for call in box_calls:
        assert call.is_resolved is True
        assert call.callee_usr not in function_usrs


def test_anonymous_namespace_call(cpp_parsed: list[ParsedTranslationUnit]) -> None:
    basic = cpp_parsed[0]
    functions = functions_by_qname(basic)
    local_calls = calls_by_name(basic, "local_helper")
    assert len(local_calls) == 1
    assert local_calls[0].callee_id == functions["(anonymous namespace)::local_helper"].id
    assert local_calls[0].callee_qualified_name == "(anonymous namespace)::local_helper"


def test_every_call_caller_is_a_recorded_function(
    cpp_parsed: list[ParsedTranslationUnit],
) -> None:
    for parsed in cpp_parsed:
        function_ids = {function.id for function in parsed.functions}
        for call in parsed.calls:
            assert call.caller_id in function_ids
            assert call.callee_name
            assert call.id == ids.call_id(
                caller_id=call.caller_id,
                callee_usr=call.callee_usr,
                callee_name=call.callee_name,
                sloc=call.sloc,
            )


# --------------------------------------------------------------------------- #
# Cross-TU stability and real CMake compile database
# --------------------------------------------------------------------------- #


def test_cross_translation_unit_ids_are_stable(
    cpp_units: list, cpp_parsed: list[ParsedTranslationUnit]
) -> None:
    basic, second = cpp_parsed
    basic_types = types_by_qname(basic)
    second_types = types_by_qname(second)
    for name in ("ast_cpp::Point", "ast_cpp::Color", "ast_cpp::Base", "ast_cpp::Widget", "ast_cpp::Box"):
        assert basic_types[name].id == second_types[name].id, name

    basic_functions = functions_by_qname(basic)
    second_functions = functions_by_qname(second)
    for name in ("ast_cpp::helper", "ast_cpp::Base::value", "ast_cpp::max_of", "ast_cpp::Box::get"):
        assert basic_functions[name].id == second_functions[name].id, name
    # The first TU has the definitions, the second only declarations.
    assert basic_functions["ast_cpp::helper"].is_definition is True
    assert second_functions["ast_cpp::helper"].is_definition is False

    # Parsing the same unit twice yields identical ids.
    again = [parse_compilation_unit(unit) for unit in cpp_units]
    for first, repeated in zip(cpp_parsed, again):
        assert [f.id for f in first.functions] == [f.id for f in repeated.functions]
        assert [c.id for c in first.calls] == [c.id for c in repeated.calls]


def test_cmake_generated_compile_database(tmp_path: Path) -> None:
    cmake = shutil.which("cmake")
    compiler = _require_cpp_compiler()
    if cmake is None:
        pytest.skip("cmake not available")

    project = tmp_path / "ca_cpp_project"
    source_dir = project / "src"
    source_dir.mkdir(parents=True)
    for name in ("ast_cpp_basic.cpp", "ast_cpp_basic.hpp", "ast_cpp_second.cpp"):
        shutil.copyfile(FIXTURES / name, source_dir / name)
    (project / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.16)\n"
        "project(ca_cpp_fixture CXX)\n"
        "set(CMAKE_CXX_STANDARD 17)\n"
        "set(CMAKE_CXX_STANDARD_REQUIRED ON)\n"
        "set(CMAKE_EXPORT_COMPILE_COMMANDS ON)\n"
        "add_library(ca_cpp_fixture STATIC src/ast_cpp_basic.cpp src/ast_cpp_second.cpp)\n",
        encoding="utf-8",
    )
    build_dir = project / "build"
    completed = subprocess.run(
        [
            cmake,
            "-S",
            str(project),
            "-B",
            str(build_dir),
            "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
            f"-DCMAKE_CXX_COMPILER={compiler}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    compdb = build_dir / "compile_commands.json"
    assert compdb.is_file()

    units = load_compilation_database(str(compdb), str(project))
    assert len(units) == 2
    parsed = [parse_compilation_unit(unit) for unit in units]
    for result in parsed:
        assert result.parse_ok is True, result.error
        assert not [d for d in result.diagnostics if d.severity in ("error", "fatal")]
    combined_types = {record.qualified_name for result in parsed for record in result.types}
    combined_functions = {record.qualified_name for result in parsed for record in result.functions}
    assert "ast_cpp::Base" in combined_types
    assert "ast_cpp::Widget" in combined_types
    assert "ast_cpp::helper" in combined_functions
    assert sum(len(result.functions) for result in parsed) > 20
