"""OpenHarmony-style compile-argument compatibility tests (task P3-5).

The real OH compile database is produced on the build server, but its flag
surface is reproducible locally: ``--target=aarch64-linux-ohos``,
``--sysroot=<oh sysroot>``, ``-march=armv8-a``, ``-D__OHOS__``, unknown
``-Xclang``/``-mllvm`` vendor passthrough options (OH clang version differences)
and GCC-style warning flags. This suite builds a throw-away sysroot and asserts:

- the faithful attempt is retried through the existing compat replay, which
  drops only frontend passthrough options, keeps the real target/sysroot and
  records a ``note`` diagnostic (never silently);
- the resulting AST is the OH aarch64/ELF one (target-specific marker function
  present, host-replay marker absent), and sysroot headers are found/recorded;
- ``-Werror`` with an unknown OH warning flag degrades instead of failing;
- a C++ TU with the same OH-style argv still parses (C++ path regression).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ca.core import ids
from ca.core.schema import CompilationUnit
from ca.parse import ParseOptions, ParsedTranslationUnit, parse_compilation_unit

FIXTURES = Path(__file__).resolve().parent / "fixtures"
STUB_HEADER = """\
/* ohos_compat_stub.h — minimal OH sysroot header for the compat tests. */
#ifndef OHOS_COMPAT_STUB_H
#define OHOS_COMPAT_STUB_H

static inline int ohos_stub_add(int a, int b)
{
    return a + b;
}

static inline int ohos_stub_mul(int a, int b)
{
    return a * b;
}

#endif /* OHOS_COMPAT_STUB_H */
"""


def _make_repo(tmp_path: Path) -> Path:
    """Copy the fixtures into a repo that also contains a fake OH sysroot."""

    repo = tmp_path / "ohos_repo"
    repo.mkdir()
    for name in ("ast_ohos.c", "ast_fnptr.cpp"):
        shutil.copyfile(FIXTURES / name, repo / name)
    include_dir = repo / "sysroot" / "usr" / "include"
    include_dir.mkdir(parents=True)
    (include_dir / "ohos_compat_stub.h").write_text(STUB_HEADER, encoding="utf-8")
    return repo


# Typical global cflags from OH's build config (compiler/BUILD.gn), so the
# synthetic argv also covers the flags the real OpenHarmony database will carry.
OH_GLOBAL_CFLAGS = (
    "-fPIC",
    "-fstack-protector-strong",
    "-fno-omit-frame-pointer",
    "-ffunction-sections",
    "-fdata-sections",
    "-fvisibility=hidden",
    "-O2",
    "-D_FORTIFY_SOURCE=2",
    "-Wall",
    "-Wextra",
    "-Wformat=2",
    "-Werror=format-security",
)


def ohos_arguments(repo: Path, extra: tuple[str, ...] = ()) -> tuple[str, ...]:
    return (
        "--target=aarch64-linux-ohos",
        f"--sysroot={repo / 'sysroot'}",
        "-march=armv8-a",
        "-D__OHOS__",
        "-std=gnu99",
        "-Xclang",
        "-clang-vendor-feature=+oh-vendor",
        "-mllvm",
        "-enable-ohos-pass",
    ) + OH_GLOBAL_CFLAGS + extra


def make_unit(repo: Path, filename: str, arguments: tuple[str, ...], language: str = "c") -> CompilationUnit:
    file_path = (repo / filename).resolve()
    return CompilationUnit(
        id=ids.tu_id(str(file_path), str(repo), arguments),
        file=str(file_path),
        directory=str(repo),
        repo_root=str(repo),
        arguments=arguments,
        language=language,
        source="test",
    )


def functions_by_name(parsed: ParsedTranslationUnit) -> dict[str, object]:
    return {function.name: function for function in parsed.functions}


def test_ohos_style_argv_is_replayed_and_target_preserved(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    parsed = parse_compilation_unit(make_unit(repo, "ast_ohos.c", ohos_arguments(repo)))
    assert parsed.parse_ok is True, parsed.error
    functions = functions_by_name(parsed)
    # Target-specific marker: aarch64 + ELF + not Apple proves the real target
    # survived the compat replay instead of a host fallback.
    assert "ohos_aarch64_marker" in functions
    assert "host_apple_marker" not in functions
    assert "ohos_dispatch" in functions
    notes = [record for record in parsed.diagnostics if record.severity == "note"]
    assert notes, "compat replay must be recorded as a note diagnostic"
    assert "frontend passthrough" in notes[0].message
    assert "host target" not in notes[0].message
    assert not [record for record in parsed.diagnostics if record.severity in ("error", "fatal")]

    # Sysroot header was found *and* is inside repo_root, so its inline functions
    # are recorded with repo-relative locations.
    stub_functions = {
        name: function for name, function in functions.items() if name.startswith("ohos_stub_")
    }
    assert set(stub_functions) == {"ohos_stub_add", "ohos_stub_mul"}
    assert stub_functions["ohos_stub_add"].sloc.file == "sysroot/usr/include/ohos_compat_stub.h"
    dispatch = functions["ohos_dispatch"]
    stub_calls = [call for call in parsed.calls if call.callee_name == "ohos_stub_add"]
    assert len(stub_calls) == 1
    assert stub_calls[0].is_resolved is True
    assert stub_calls[0].callee_id == stub_functions["ohos_stub_add"].id
    assert stub_calls[0].caller_id == dispatch.id


def test_ohos_werror_and_unknown_warning_flags_degrade(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    arguments = ohos_arguments(repo, extra=("-Werror", "-Wno-ohos-unknown-flag"))
    parsed = parse_compilation_unit(make_unit(repo, "ast_ohos.c", arguments))
    assert parsed.parse_ok is True, parsed.error
    assert "ohos_aarch64_marker" in functions_by_name(parsed)
    # Unknown warning becomes a plain warning after the relaxed replay, and the
    # passthrough removal is still noted; nothing is silently dropped.
    severities = [record.severity for record in parsed.diagnostics]
    assert "error" not in severities and "fatal" not in severities
    assert "warning" in severities
    assert any("frontend passthrough" in record.message for record in parsed.diagnostics)


def test_ohos_function_pointer_dispatch(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    parsed = parse_compilation_unit(make_unit(repo, "ast_ohos.c", ohos_arguments(repo)))
    typedefs = {record.name: record for record in parsed.types}
    assert typedefs["ohos_callback_t"].underlying_type == "int (*)(int)"
    dispatch = functions_by_name(parsed)["ohos_dispatch"]
    assert dispatch.params[0].type_spelling == "ohos_callback_t"
    indirect = [call for call in parsed.calls if call.caller_id == dispatch.id and call.callee_name == "cb"]
    assert len(indirect) == 1
    assert indirect[0].kind == "indirect" and indirect[0].is_resolved is False


def test_ohos_cpp_translation_unit_still_parses(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    arguments = (
        "--target=aarch64-linux-ohos",
        f"--sysroot={repo / 'sysroot'}",
        "-march=armv8-a",
        "-D__OHOS__",
        "-std=c++17",
        "-Xclang",
        "-clang-vendor-feature=+oh-vendor",
    )
    parsed = parse_compilation_unit(
        make_unit(repo, "ast_fnptr.cpp", arguments, language="c++")
    )
    assert parsed.parse_ok is True, parsed.error
    qualified = {(record.qualified_name or record.name) for record in parsed.types}
    assert "ast_cpp_fnptr::callback_t" in qualified
    assert "ast_cpp_fnptr::Dispatcher" in qualified
    functions = {(function.qualified_name) for function in parsed.functions}
    assert "ast_cpp_fnptr::Dispatcher::dispatch" in functions
    notes = [record for record in parsed.diagnostics if record.severity == "note"]
    assert notes


def test_ohos_clang_resource_headers_are_discovered(tmp_path: Path) -> None:
    """OH clang builtin headers (stdarg.h) come from the driver resource dir.

    The bundled PyPI libclang ships no resource headers, so a cross OHOS TU that
    uses <stdarg.h> only parses when the parser queries the real cross driver
    (``clang -print-resource-dir``) exactly like it does for GCC drivers.
    """

    compiler = shutil.which("clang++") or shutil.which("clang")
    if compiler is None:
        pytest.skip("no clang driver available")
    completed = subprocess.run(
        [compiler, "-print-resource-dir"], capture_output=True, text=True, check=False
    )
    resource_include = Path(completed.stdout.strip()) / "include"
    if completed.returncode != 0 or not (resource_include / "stdarg.h").is_file():
        pytest.skip("clang driver has no resource-dir headers")

    repo = _make_repo(tmp_path)
    source = repo / "ohos_stdarg.c"
    source.write_text(
        "#include <stdarg.h>\n"
        "int ohos_vararg(int count, ...)\n"
        "{\n"
        "    va_list args;\n"
        "    va_start(args, count);\n"
        "    va_end(args);\n"
        "    return count;\n"
        "}\n",
        encoding="utf-8",
    )
    arguments = (
        "--target=aarch64-linux-ohos",
        f"--sysroot={repo / 'sysroot'}",
        "-march=armv8-a",
        "-D__OHOS__",
        "-std=gnu99",
    )
    unit = make_unit(repo, "ohos_stdarg.c", arguments)
    unit.driver = compiler
    parsed = parse_compilation_unit(unit)
    assert parsed.parse_ok is True, parsed.error
    assert "ohos_vararg" in functions_by_name(parsed)


def test_oh_gn_vendor_passthrough_sequence(tmp_path: Path) -> None:
    """Exact vendor passthrough sequences emitted by OH's build/config.

    OH's ``build/config/compiler/BUILD.gn`` appends
    ``-Xclang -mllvm -Xclang -instcombine-lower-dbg-declare=0`` and
    ``-Xclang -fdebug-compilation-dir -Xclang .``. The compat replay must drop
    those pairs as one unit (not leak the value tokens as positional inputs)
    while keeping the real target and parsing the TU.
    """

    repo = _make_repo(tmp_path)
    arguments = (
        "--target=aarch64-linux-ohos",
        f"--sysroot={repo / 'sysroot'}",
        "-march=armv8-a",
        "-D__OHOS__",
        "-std=gnu99",
        "-Xclang",
        "-mllvm",
        "-Xclang",
        "-instcombine-lower-dbg-declare=0",
        "-Xclang",
        "-fdebug-compilation-dir",
        "-Xclang",
        ".",
    )
    parsed = parse_compilation_unit(make_unit(repo, "ast_ohos.c", arguments))
    assert parsed.parse_ok is True, parsed.error
    assert "ohos_aarch64_marker" in functions_by_name(parsed)
    assert "host_apple_marker" not in functions_by_name(parsed)
    # libclang 18 accepts these OH-clang 15 cc1 options natively; if a future
    # bundled libclang rejects them the compat replay parses with a note, which
    # is equally acceptable -- either way nothing may be dropped silently into
    # the positional argument list (parse_ok above proves that).
    notes = [record.message for record in parsed.diagnostics if record.severity == "note"]
    assert all("frontend passthrough" in note for note in notes)


# Exact OH-clang 15 options seen in the real OpenHarmony build database. The
# bundled libclang 18 reports them as "unknown argument" error diagnostics while
# still producing an AST; the compat layer must strip only those argv tokens.
OH_UNKNOWN_OPTIONS = (
    "-fstack-protector-ret-strong",
    "-fcfi-trap-function=__cfi_fail_report",
    "-enable-trivial-auto-var-init-zero-knowing-it-will-be-removed-from-clang",
)


def test_oh_unknown_compiler_options_are_stripped_and_noted(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    for extra in (OH_UNKNOWN_OPTIONS, ("-Werror",) + OH_UNKNOWN_OPTIONS):
        parsed = parse_compilation_unit(
            make_unit(repo, "ast_ohos.c", ohos_arguments(repo, extra=extra))
        )
        assert parsed.parse_ok is True, parsed.error
        functions = functions_by_name(parsed)
        assert "ohos_aarch64_marker" in functions
        assert "host_apple_marker" not in functions
        # Stripped options are auditable, never silent.
        notes = [record.message for record in parsed.diagnostics if record.severity == "note"]
        if any(
            note.startswith("removed compiler options")
            and "unsupported by the bundled libclang" in note
            for note in notes
        ):
            # Older bundled libclang rejects the OH vendor options: every removed
            # option must be listed in the audit note.
            removal_notes = " ".join(notes)
            for flag in OH_UNKNOWN_OPTIONS:
                assert flag in removal_notes
        # A matched-version backend (OH libclang 15) understands these options
        # natively, so no removal note is required -- only the AST must be right.
        assert not [record for record in parsed.diagnostics if record.severity in ("error", "fatal")]


def test_relative_driver_path_is_resolved_for_resource_headers(tmp_path: Path) -> None:
    """OH databases store the clang driver relative to the build directory."""

    compiler = shutil.which("clang++") or shutil.which("clang")
    if compiler is None:
        pytest.skip("no clang driver available")
    completed = subprocess.run(
        [compiler, "-print-resource-dir"], capture_output=True, text=True, check=False
    )
    resource_include = Path(completed.stdout.strip()) / "include"
    if completed.returncode != 0 or not (resource_include / "stdarg.h").is_file():
        pytest.skip("clang driver has no resource-dir headers")

    repo = _make_repo(tmp_path)
    tools = repo / "tools"
    tools.mkdir()
    os.symlink(compiler, tools / "clang++")
    source = repo / "ohos_relative_driver.c"
    source.write_text(
        "#include <stdarg.h>\n"
        "int ohos_relative_driver(int count, ...)\n"
        "{\n"
        "    va_list args;\n"
        "    va_start(args, count);\n"
        "    va_end(args);\n"
        "    return count;\n"
        "}\n",
        encoding="utf-8",
    )
    arguments = (
        "--target=aarch64-linux-ohos",
        f"--sysroot={repo / 'sysroot'}",
        "-std=gnu99",
    )
    unit = make_unit(repo, "ohos_relative_driver.c", arguments)
    unit.driver = "tools/clang++"  # relative to unit.directory, like OH compdb
    parsed = parse_compilation_unit(unit)
    assert parsed.parse_ok is True, parsed.error
    assert "ohos_relative_driver" in functions_by_name(parsed)


def test_codegen_only_options_are_ignored_with_note(tmp_path: Path) -> None:
    """OH sanitizer/profile flags only change codegen; libclang lacks their data."""

    repo = _make_repo(tmp_path)
    arguments = ohos_arguments(
        repo,
        extra=(
            "-fsanitize=cfi-icall",
            "-fsanitize-ignorelist=/nonexistent/ohos_cfi_ignorelist.txt",
            "-fprofile-instr-generate",
            "-fcoverage-mapping",
        ),
    )
    parsed = parse_compilation_unit(make_unit(repo, "ast_ohos.c", arguments))
    assert parsed.parse_ok is True, parsed.error
    assert "ohos_aarch64_marker" in functions_by_name(parsed)
    notes = " ".join(record.message for record in parsed.diagnostics if record.severity == "note")
    assert "codegen-only options" in notes
    assert "-fsanitize=cfi-icall" in notes
    assert not [record for record in parsed.diagnostics if record.severity in ("error", "fatal")]


def test_cpp_toolchain_libcxx_include_is_discovered(tmp_path: Path) -> None:
    """Cross clang toolchains carry libc++ next to the resource dir.

    OH databases call the driver as a relative path and rely on the driver to
    add ``<llvm>/include/c++/v1``; the bundled libclang must be given that path.
    A fake driver script + toolchain layout keeps this test environment-neutral.
    """

    from ca.parse import libclang_parser as parser_impl

    llvm = tmp_path / "fake_llvm"
    resource = llvm / "lib" / "clang" / "15.0.4"
    (resource / "include").mkdir(parents=True)
    (resource / "include" / "stdarg.h").write_text("#pragma once\n", encoding="utf-8")
    libcxx = llvm / "include" / "c++" / "v1"
    libcxx.mkdir(parents=True)
    (libcxx / "vector").write_text("#pragma once\n", encoding="utf-8")

    repo = _make_repo(tmp_path)
    tools = repo / "tools"
    tools.mkdir()
    driver = tools / "clang++"
    driver.write_text(f"#!/bin/sh\necho {resource}\n", encoding="utf-8")
    driver.chmod(0o755)

    cpp_unit = make_unit(
        repo, "ast_fnptr.cpp", ("--target=aarch64-linux-ohos", "-std=c++17"), language="c++"
    )
    cpp_unit.driver = "tools/clang++"
    prepared = parser_impl._prepare_arguments(cpp_unit, ParseOptions(), cpp_unit.file)
    assert str(libcxx) in prepared
    assert "-resource-dir" in prepared and str(resource) in prepared
    assert prepared.index("-stdlib=libc++") >= 0

    c_unit = make_unit(repo, "ast_ohos.c", ("--target=aarch64-linux-ohos", "-std=gnu99"))
    c_unit.driver = "tools/clang++"
    prepared_c = parser_impl._prepare_arguments(c_unit, ParseOptions(), c_unit.file)
    assert str(libcxx) not in prepared_c
    assert "-resource-dir" in prepared_c and str(resource) in prepared_c


def test_preflight_makes_unknown_options_single_full_parse(tmp_path: Path) -> None:
    """After the tiny preflight probe, each TU needs exactly one full parse.

    This is the runtime-cost guarantee for large OH corpora: unknown vendor
    options are learned before the first real parse (or upfront per process) and
    repeating them on the next TU does not trigger another full parse.
    """

    from ca.parse import libclang_parser as parser_impl

    repo = _make_repo(tmp_path)
    parser_impl._learned_unknown_options.clear()
    parser_impl._preflight_done = False
    successful_loads: list[str] = []
    original_load = parser_impl._load

    def counting_load(source, arguments, options, unsaved_files=()):
        result = original_load(source, arguments, options, unsaved_files=unsaved_files)
        if source not in {path for path, _ in unsaved_files}:
            successful_loads.append(source)
        return result

    parser_impl._load = counting_load
    try:
        unit = make_unit(
            repo,
            "ast_ohos.c",
            ohos_arguments(repo, extra=OH_UNKNOWN_OPTIONS),
        )
        first = parse_compilation_unit(unit)
        second = parse_compilation_unit(unit)
    finally:
        parser_impl._load = original_load
        parser_impl._learned_unknown_options.clear()
        parser_impl._preflight_done = False

    assert first.parse_ok is True, first.error
    assert second.parse_ok is True, second.error
    assert len(successful_loads) == 2, successful_loads


def test_record_scope_main_file_reduces_header_records(tmp_path: Path) -> None:
    from ca.parse import ParseOptions

    repo = _make_repo(tmp_path)
    unit = make_unit(repo, "ast_ohos.c", ohos_arguments(repo))
    all_scope = parse_compilation_unit(unit, ParseOptions())
    main_scope = parse_compilation_unit(unit, ParseOptions(record_scope="main_file"))
    assert all_scope.parse_ok is True and main_scope.parse_ok is True
    # ast_ohos.c has no header-defined functions; the sysroot stub header does.
    assert any(function.sloc.file == "sysroot/usr/include/ohos_compat_stub.h"
               for function in all_scope.functions)
    assert all(function.sloc.file == "ast_ohos.c" for function in main_scope.functions)
    assert main_scope.types  # types/fields stay available for layout analysis
    assert len(main_scope.functions) < len(all_scope.functions)
    assert {call.caller_id for call in main_scope.calls} <= {
        function.id for function in main_scope.functions
    }


def test_emit_header_types_cuts_header_records(tmp_path: Path) -> None:
    from ca.parse import ParseOptions

    repo = _make_repo(tmp_path)
    unit = make_unit(repo, "ast_ohos.c", ohos_arguments(repo))
    full = parse_compilation_unit(unit, ParseOptions())
    lean = parse_compilation_unit(
        unit,
        ParseOptions(record_scope="main_file", emit_header_types=False),
    )
    assert full.parse_ok is True and lean.parse_ok is True
    # The stub header adds no types, but ast_ohos.c's own typedef must stay.
    assert all(record.sloc.file == "ast_ohos.c" for record in lean.types)
    assert all(record.sloc.file == "ast_ohos.c" for record in lean.fields)
    assert {record.name for record in lean.types} == {"ohos_callback_t"}
    assert len(lean.functions) <= len(full.functions)
    # Types/fields remain available in the default mode for layout analysis.
    assert len(full.types) >= len(lean.types)


def test_nostdinc_keeps_resource_dir_but_not_implicit_libcxx(tmp_path: Path) -> None:
    """`-nostdinc` TUs keep the compiler builtin (*resource-dir*) but not our
    implicit libc++ injection: an explicit -nostdinc build owns its header tree.
    The resource dir restores clang's own stddef.h/stdarg.h, which no build can
    provide through -isystem reliably.
    """

    from ca.parse import libclang_parser as parser_impl

    llvm = tmp_path / "fake_llvm2"
    resource = llvm / "lib" / "clang" / "15.0.4"
    (resource / "include").mkdir(parents=True)
    (resource / "include" / "stddef.h").write_text("#pragma once\n", encoding="utf-8")
    libcxx = llvm / "include" / "libcxx-ohos" / "include" / "c++" / "v1"
    libcxx.mkdir(parents=True)
    (libcxx / "vector").write_text("#pragma once\n", encoding="utf-8")
    (libcxx / "__config_site").write_text("#pragma once\n", encoding="utf-8")

    repo = _make_repo(tmp_path)
    tools = repo / "tools"
    tools.mkdir()
    driver = tools / "clang++"
    driver.write_text(f"#!/bin/sh\necho {resource}\n", encoding="utf-8")
    driver.chmod(0o755)

    unit = make_unit(
        repo,
        "ast_fnptr.cpp",
        ("--target=aarch64-linux-ohos", "-std=c++17", "-nostdinc"),
        language="c++",
    )
    unit.driver = "tools/clang++"
    prepared = parser_impl._prepare_arguments(unit, ParseOptions(), unit.file)
    assert "-resource-dir" in prepared and str(resource) in prepared
    assert str(libcxx) not in prepared


def test_host_tool_tu_without_target_gets_toolchain_includes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OH host-tool TUs carry no --target but still use the OH clang toolchain.

    On a non-Darwin host the driver-derived resource-dir + libc++ must still be
    injected, otherwise their `<stddef.h>`/`<string>` lookups fail (this was the
    last remaining class of parser-path sample_100 errors).
    """

    import sys as _sys

    from ca.parse import libclang_parser as parser_impl

    monkeypatch.setattr(_sys, "platform", "linux")
    llvm = tmp_path / "fake_llvm3"
    resource = llvm / "lib" / "clang" / "15.0.4"
    (resource / "include").mkdir(parents=True)
    (resource / "include" / "stddef.h").write_text("#pragma once\n", encoding="utf-8")
    libcxx = llvm / "include" / "libcxx-ohos" / "include" / "c++" / "v1"
    libcxx.mkdir(parents=True)
    (libcxx / "vector").write_text("#pragma once\n", encoding="utf-8")
    (libcxx / "__config_site").write_text("#pragma once\n", encoding="utf-8")

    repo = _make_repo(tmp_path)
    tools = repo / "tools"
    tools.mkdir()
    driver = tools / "clang++"
    driver.write_text(f"#!/bin/sh\necho {resource}\n", encoding="utf-8")
    driver.chmod(0o755)

    unit = make_unit(repo, "ast_fnptr.cpp", ("-std=c++17",), language="c++")
    unit.driver = "tools/clang++"
    prepared = parser_impl._prepare_arguments(unit, ParseOptions(), unit.file)
    assert "-resource-dir" in prepared and str(resource) in prepared
    assert str(libcxx) in prepared
    assert "-stdlib=libc++" in prepared
