"""Matched-version libclang backend tests (task P3-5, OpenHarmony acceptance).

The default backend is the libclang bundled with the pinned ``clang`` wheel.
For OpenHarmony the language features/headers were produced by OH clang 15, so
acceptance runs with a matched backend: OH's ``libclang.so.15`` plus the PyPI
``clang==15.0.6.1`` bindings (on ``PYTHONPATH``). Selection is explicit and
auditable through ``CA_LIBCLANG_LIBRARY``; nothing is hard-coded.

These tests only run when that environment variable is set (i.e. the whole
pytest process was started with the matched backend), otherwise they skip.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ca.core import ids
from ca.core.schema import CompilationUnit
from ca.parse import ParseOptions, parse_compilation_unit
from ca.parse import cursor_utils as cu

FIXTURES = Path(__file__).resolve().parent / "fixtures"
OH_SAMPLE = Path("/home/yu/CA_OH/corpus/openharmony/sample_20.json")

pytestmark = pytest.mark.skipif(
    not cu.libclang_library_override(),
    reason="matched libclang backend not configured (CA_LIBCLANG_LIBRARY)",
)


def _unit(filename: str, arguments: tuple[str, ...], repository: Path | None = None) -> CompilationUnit:
    file_path = (FIXTURES / filename).resolve()
    root = (repository or FIXTURES).resolve()
    return CompilationUnit(
        id=ids.tu_id(str(file_path), str(root), arguments),
        file=str(file_path),
        directory=str(root),
        repo_root=str(root),
        arguments=arguments,
        language="c" if filename.endswith(".c") else "c++",
        source="test",
    )


def test_backend_override_is_selected() -> None:
    assert cu.libclang_library_override().endswith((".so", ".so.15", ".dylib"))

    # The same parser contract works on the matched backend. The OH-built libclang
    # has a limited host-target support, so an OHOS target is used (the fixtures
    # include no system headers).
    parsed = parse_compilation_unit(
        _unit("ast_basic.c", ("-std=gnu99", "--target=aarch64-linux-ohos"))
    )
    assert parsed.parse_ok is True, parsed.error
    assert {"callee", "caller", "add"} <= {function.name for function in parsed.functions}


def test_matched_backend_cpp_fixture() -> None:
    parsed = parse_compilation_unit(
        _unit("ast_fnptr.cpp", ("-std=c++17", "--target=aarch64-linux-ohos"))
    )
    assert parsed.parse_ok is True, parsed.error
    qualified = {record.qualified_name or record.name for record in parsed.types}
    assert "ast_cpp_fnptr::callback_t" in qualified


@pytest.mark.skipif(not OH_SAMPLE.is_file(), reason="OH sample_20.json corpus not present")
def test_oh_sample_with_matched_backend() -> None:
    """Real OH argv on the matched backend: flag compat must not fail the TU."""

    from ca.build import load_compilation_database

    repo = os.environ.get("CA_OH_REPO", "/home/yu/CA_OH/openharmony")
    units = load_compilation_database(str(OH_SAMPLE), repo)[:5]
    parsed_units = [parse_compilation_unit(unit, ParseOptions()) for unit in units]
    assert len(parsed_units) == len(units)
    # Missing *generated* headers may still fail individual TUs; option
    # compatibility itself must not add errors: no unknown-argument diagnostics.
    for parsed in parsed_units:
        messages = [record.message for record in parsed.diagnostics if record.severity in ("error", "fatal")]
        assert not any(
            "unknown argument" in message or "unknown warning option" in message
            for message in messages
        ), messages[:3]
    assert any(parsed.parse_ok for parsed in parsed_units), [
        parsed.error for parsed in parsed_units
    ]
