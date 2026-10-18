"""Sloc path normalization tests: repo-relative POSIX paths only."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ca.analyze.astutil import SourceCache
try:  # pragma: no cover - import layout differs when tests is a package
    from test_analyze_common import REPO_ROOT, FakeParsedUnit, unit_for_fixture
except ImportError:  # pragma: no cover - e.g. server deployment with tests/__init__.py
    from tests.test_analyze_common import REPO_ROOT, FakeParsedUnit, unit_for_fixture

KERNEL_REPO = REPO_ROOT / ".tmp" / "linux" / "linux-6.12.113"
KERNEL_COMPDB = REPO_ROOT / ".tmp" / "corpus" / "linux" / "compile_commands.json"


def test_source_cache_resolves_against_repo_root_not_cwd():
    cache = SourceCache(str(REPO_ROOT))
    for raw in (
        "./tests/fixtures/df_kernel_paths.c",
        "tests/fixtures/df_kernel_paths.c",
        str(REPO_ROOT / "tests/fixtures/df_kernel_paths.c"),
    ):
        assert cache.repo_path(raw) == "tests/fixtures/df_kernel_paths.c"
    # Outside the repository: marked unknown instead of emitting an absolute
    # cwd-relative or machine path.
    assert cache.repo_path("/etc/hosts") == ""
    # Nonexistent but repo-relative inputs stay repo-relative (never absolute).
    assert cache.repo_path("does/not/exist.c") == "does/not/exist.c"


def test_all_fixture_slocs_are_relative_and_exist():
    result = unit_for_fixture("df_kernel_paths.c").analyze("path_fixture")
    assert result.branches and result.returns and result.resource_ops
    for bucket in (
        result.branches,
        result.assignments,
        result.returns,
        result.resource_ops,
    ):
        for record in bucket:
            file = record.sloc.file
            assert file, record
            assert not os.path.isabs(file), file
            assert "\\" not in file, file
            assert (REPO_ROOT / file).is_file(), file
    for block in result.cfg_blocks:
        assert not os.path.isabs(block.sloc.file)


@pytest.mark.skipif(
    not KERNEL_COMPDB.is_file() or not KERNEL_REPO.is_dir(),
    reason="Linux kernel corpus not produced yet",
)
def test_kernel_header_function_slocs_are_repo_relative():
    from ca.build import load_compilation_database
    from ca.parse import parse_compilation_unit
    from ca.analyze import analyze_function

    units = load_compilation_database(
        str(KERNEL_COMPDB), repo_root=str(KERNEL_REPO)
    )
    unit = next(u for u in units if u.file.endswith("kernel/cred.c"))
    parsed = parse_compilation_unit(unit)
    header_function = next(
        f
        for f in parsed.functions
        if f.sloc.file.startswith("include/") and f.is_definition
    )
    result = analyze_function(parsed, header_function)
    for bucket in (
        result.branches,
        result.assignments,
        result.returns,
        result.resource_ops,
    ):
        for record in bucket:
            file = record.sloc.file
            assert file and not os.path.isabs(file), (header_function.name, file)
            assert (KERNEL_REPO / file).is_file() or file.startswith("include/")
