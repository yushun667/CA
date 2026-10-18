"""Include dependency contract tests (task-38, parser side).

Frozen contract:
- ``ParseResult.include_files``: the TU main file's **direct includes**, repo
  relative POSIX, repo-local files only, sorted/deduped, main file excluded.
- ``ParseResult.include_edges``: every repo-local include edge observed in the
  TU as ``(parent_file, included_file)`` (includes header->header edges).
- Both are TU-level facts: filled for ``record_scope="all"`` and
  ``"main_file"``, cached via the picklable ``ParseResult``; empty when
  ``ParseOptions.collect_include_files=False`` or the TU failed to load.
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


def test_direct_includes_and_repo_edges() -> None:
    parsed = parse_compilation_unit(make_unit("ast_includes_main.c"))
    assert parsed.parse_ok is True, parsed.error
    # Direct includes of the main file only (repo-local); <stddef.h> lives
    # outside repo_root and is therefore not monitorable/included.
    assert parsed.include_files == ("ast_includes_a.h",)
    # Header->header edge a->b is recorded even though b is not a direct include
    # of the main file.
    assert parsed.include_edges == (
        ("ast_includes_a.h", "ast_includes_b.h"),
        ("ast_includes_main.c", "ast_includes_a.h"),
    )
    assert "ast_includes_main.c" not in parsed.include_files


def test_include_facts_are_record_scope_independent() -> None:
    unit = make_unit("ast_includes_main.c")
    all_scope = parse_compilation_unit(unit, ParseOptions())
    main_scope = parse_compilation_unit(
        unit, ParseOptions(record_scope="main_file", emit_header_types=False)
    )
    assert all_scope.include_files == main_scope.include_files
    assert all_scope.include_edges == main_scope.include_edges


def test_include_collection_can_be_disabled() -> None:
    parsed = parse_compilation_unit(
        make_unit("ast_includes_main.c"),
        ParseOptions(collect_include_files=False),
    )
    assert parsed.include_files == ()
    assert parsed.include_edges == ()


def test_include_facts_survive_parse_result_pickle() -> None:
    parsed = parse_compilation_unit(make_unit("ast_includes_main.c"))
    restored = pickle.loads(pickle.dumps(parsed.parse_result()))
    assert restored.include_files == parsed.include_files
    assert restored.include_edges == parsed.include_edges


def test_missing_main_file_has_empty_include_facts() -> None:
    missing = FIXTURES / "does_not_exist.c"
    unit = CompilationUnit(
        id=ids.tu_id(str(missing), str(FIXTURES), ()),
        file=str(missing),
        directory=str(FIXTURES),
        repo_root=str(FIXTURES),
    )
    parsed = parse_compilation_unit(unit)
    assert parsed.parse_ok is False
    assert parsed.include_files == () and parsed.include_edges == ()
    result = parsed.parse_result()
    assert result.include_files == () and result.include_edges == ()


def test_header_declaration_mode_keeps_include_facts() -> None:
    # The task-25 declaration switch does not interact with dependency facts.
    parsed = parse_compilation_unit(
        make_unit("ast_includes_main.c"),
        ParseOptions(record_scope="main_file", emit_header_declarations=True),
    )
    assert parsed.include_files == ("ast_includes_a.h",)
    assert len(parsed.include_edges) == 2


def test_include_path_excludes_filter_repo_toolchains() -> None:
    # Default excludes target vendored toolchain trees (OH prebuilts/) and do
    # not affect ordinary project headers.
    parsed = parse_compilation_unit(make_unit("ast_includes_main.c"))
    assert parsed.include_files == ("ast_includes_a.h",)
    # Excluding a project header removes it from direct includes and removes its
    # outgoing edge (a -> b), while the main-file edge source is dropped too.
    filtered = parse_compilation_unit(
        make_unit("ast_includes_main.c"),
        ParseOptions(include_path_excludes=("ast_includes_a.h",)),
    )
    assert filtered.include_files == ()
    assert filtered.include_edges == ()


def test_include_edges_limit_truncates_with_note() -> None:
    parsed = parse_compilation_unit(
        make_unit("ast_includes_main.c"),
        ParseOptions(include_edges_limit=1),
    )
    assert parsed.include_files == ("ast_includes_a.h",)
    assert len(parsed.include_edges) == 1
    notes = [record.message for record in parsed.diagnostics if record.severity == "note"]
    assert any("include edges truncated" in note for note in notes)

    unlimited = parse_compilation_unit(
        make_unit("ast_includes_main.c"), ParseOptions(include_edges_limit=0)
    )
    assert len(unlimited.include_edges) == 2
    assert not any(
        "include edges truncated" in record.message for record in unlimited.diagnostics
    )
