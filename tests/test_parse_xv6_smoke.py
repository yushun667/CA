"""Smoke test: parse real xv6-riscv translation units with real Makefile flags.

The corpus lives in the git-ignored ``.tmp/`` directory, so this test skips when
the checkout has no cloned corpus (e.g. CI without the corpus step). It parses
the first N kernel translation units and checks the parser is robust against
real GCC flags (``-march=rv64gc``, ``-mcmodel=medany``, ``-MD -o``, ...),
cross-target inference and the detached/picklable ``ParseResult``.

Expected boundaries (reported as known limitations): freestanding headers such
as ``stdarg.h`` come from the cross GCC's internal include directory. The parser
adds it when the real driver is resolvable; with a bare synthesized driver name
(no toolchain on PATH) TUs using ``<stdarg.h>`` stay in the "failed but records
kept" bucket, which is why the pass-rate assertion allows a small fraction.
"""

from __future__ import annotations

import json
import os
import pickle
import shlex
import shutil
from pathlib import Path

import pytest

from ca.core import ids
from ca.core.schema import CompilationUnit
from ca.parse import ParseOptions, parse_compilation_unit

REPO_ROOT = Path(__file__).resolve().parents[1]
XV6 = REPO_ROOT / ".tmp" / "xv6-riscv"
COMPDB = REPO_ROOT / ".tmp" / "corpus" / "xv6-riscv" / "compile_commands.json"
TU_LIMIT = 8

# Flags from .tmp/xv6-riscv/Makefile. Temporary source for the smoke test until
# the build stage's captured compile_commands.json covers this checkout; the
# test prefers that database when it exists.
MAKEFILE_CFLAGS = [
    "-Wall",
    "-Werror",
    "-Wno-unknown-attributes",
    "-O",
    "-fno-omit-frame-pointer",
    "-ggdb",
    "-gdwarf-2",
    "-march=rv64gc",
    "-std=gnu99",
    "-MD",
    "-mcmodel=medany",
    "-ffreestanding",
    "-fno-common",
    "-nostdlib",
    "-fno-delete-null-pointer-checks",
    "-fno-builtin-memcpy",
    "-Wno-main",
    "-I.",
    "-fno-stack-protector",
]


def _require_corpus() -> None:
    kernel = XV6 / "kernel"
    if not kernel.is_dir():
        pytest.skip(f"no xv6 corpus under {XV6}")


def _driver() -> str:
    for candidate in ("riscv64-unknown-elf-gcc", "riscv64-linux-gnu-gcc"):
        found = shutil.which(candidate)
        if found:
            return found
    return "riscv64-unknown-elf-gcc"


def _looks_like_compiler(arg: str) -> bool:
    name = os.path.basename(arg).lower()
    return any(token in name for token in ("gcc", "g++", "clang", "cc", "c++"))


def _units_from_compdb(limit: int) -> list[CompilationUnit]:
    repo_root = os.path.realpath(COMPDB.parent)
    # Prefer the build stage's loader: it separates argv[0] into ``driver`` so
    # the parser can query the cross GCC internal include dir (stdarg.h).
    try:
        from ca.build import load_compilation_database

        return load_compilation_database(COMPDB, repo_root)[:limit]
    except Exception:  # pragma: no cover - fallback if build stage changes
        pass

    entries = json.loads(COMPDB.read_text(encoding="utf-8"))[:limit]
    units: list[CompilationUnit] = []
    for entry in entries:
        directory = entry.get("directory") or repo_root
        file_path = entry["file"]
        if not os.path.isabs(file_path):
            file_path = os.path.join(directory, file_path)
        arguments = entry.get("arguments")
        if arguments is None:
            command = shlex.split(entry.get("command", ""))
            arguments = command[1:] if command else []
        arguments = [str(arg) for arg in arguments]
        driver = entry.get("driver", "")
        if not driver and arguments and _looks_like_compiler(arguments[0]):
            driver = arguments[0]
            arguments = arguments[1:]
        units.append(
            CompilationUnit(
                id=ids.tu_id(file_path, directory, tuple(arguments)),
                file=os.path.abspath(file_path),
                directory=directory,
                repo_root=repo_root,
                arguments=tuple(arguments),
                language="c",
                source="compile_commands",
                driver=driver,
            )
        )
    return units


def _synthesized_units(limit: int) -> list[CompilationUnit]:
    repo_root = os.path.realpath(XV6)
    driver = _driver()
    files = sorted(XV6.glob("kernel/*.c"))[:limit]
    units: list[CompilationUnit] = []
    for path in files:
        rel = path.relative_to(XV6).as_posix()
        arguments = tuple(
            MAKEFILE_CFLAGS
            + [
                "-ffile-prefix-map=" + repo_root + "=.",
                "-c",
                "-o",
                rel[:-2] + ".o",
                "-MF",
                rel[:-2] + ".d",
                rel,
            ]
        )
        units.append(
            CompilationUnit(
                id=ids.tu_id(str(path), repo_root, arguments),
                file=str(path),
                directory=repo_root,
                repo_root=repo_root,
                arguments=arguments,
                language="c",
                source="synthesized",
                driver=driver,
            )
        )
    return units


def _load_units(limit: int) -> list[CompilationUnit]:
    if COMPDB.is_file():
        try:
            units = _units_from_compdb(limit)
        except (json.JSONDecodeError, KeyError, TypeError):
            units = []
        if units:
            return units
    return _synthesized_units(limit)


def test_xv6_kernel_smoke() -> None:
    _require_corpus()
    units = _load_units(TU_LIMIT)
    assert units, "no translation units discovered"

    parsed_units = []
    total_functions = 0
    total_calls = 0
    source_mode = units[0].source
    for unit in units:
        parsed = parse_compilation_unit(unit)
        parsed_units.append(parsed)
        total_functions += len(parsed.functions)
        total_calls += len(parsed.calls)
        # Every returned location must be repo-relative and inside the corpus.
        for record in [*parsed.functions, *parsed.calls]:
            assert not os.path.isabs(record.sloc.file)
            if record.sloc.file:
                assert (Path(unit.repo_root) / record.sloc.file).is_file()

    passed = [parsed for parsed in parsed_units if parsed.parse_ok]
    assert total_functions > 0, f"no functions extracted ({source_mode})"
    assert total_calls > 0, f"no calls extracted ({source_mode})"
    # Captured real compile_commands.json must parse cleanly; synthesized flags
    # may still miss the cross GCC internal include dir for <stdarg.h> TUs.
    if source_mode == "compile_commands":
        assert len(passed) == len(units), [p.error for p in parsed_units if not p.parse_ok]
    else:
        assert len(passed) >= max(1, int(len(units) * 0.75)), [
            (unit.file, parsed.error)
            for unit, parsed in zip(units, parsed_units)
            if not parsed.parse_ok
        ]

    # Detached results are plain data and survive pickling (worker -> store).
    results = [parsed.parse_result() for parsed in parsed_units]
    restored = pickle.loads(pickle.dumps(results))
    assert len(restored) == len(results)
    assert sum(len(result.functions) for result in restored) == total_functions


def test_xv6_header_declaration_index_under_main_scope() -> None:
    """task-25 item 4: header declarations stay indexable on a real kernel corpus.

    Under the large-corpus ``record_scope="main_file"``, the additive
    ``emit_header_declarations`` switch must recover declarations from repo
    headers (defs.h declares the kernel API) without recording header
    definitions or duplicating main-file definitions.
    """

    _require_corpus()
    units = _load_units(4)
    unit = units[0]
    plain = parse_compilation_unit(unit, ParseOptions(record_scope="main_file"))
    indexed = parse_compilation_unit(
        unit,
        ParseOptions(record_scope="main_file", emit_header_declarations=True),
    )
    assert plain.parse_ok and indexed.parse_ok
    assert len(indexed.functions) >= len(plain.functions)

    main_file = os.path.relpath(os.path.realpath(unit.file), os.path.realpath(unit.repo_root))
    main_file = main_file.replace(os.sep, "/")
    header_declarations = [
        function
        for function in indexed.functions
        if not function.is_definition and function.sloc.file != main_file
    ]
    assert header_declarations, "expected declarations from xv6 headers (defs.h/riscv.h)"
    for function in header_declarations:
        assert function.usr or function.name
        assert not function.is_definition
    # Deterministic and stable across reparses.
    again = parse_compilation_unit(
        unit,
        ParseOptions(record_scope="main_file", emit_header_declarations=True),
    )
    assert [f.id for f in indexed.functions] == [f.id for f in again.functions]
