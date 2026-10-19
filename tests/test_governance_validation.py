"""Governance checks must respect Git's symlink representation on Windows."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest


def _load_validator():
    source = Path(__file__).resolve().parents[1] / ".agents" / "validate.py"
    spec = importlib.util.spec_from_file_location("ca_governance_validator", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


validator = _load_validator()


def _git(root: Path, *args: str, input: bytes | None = None) -> bytes:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        input=input,
        capture_output=True,
        check=True,
    ).stdout


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "--quiet")
    (tmp_path / "AGENTS.md").write_text(".agents/AGENTS.md\n", encoding="utf-8")
    (tmp_path / ".agents").mkdir()
    (tmp_path / ".agents" / "AGENTS.md").write_text("# Management\n", encoding="utf-8")
    _git(tmp_path, "add", "AGENTS.md", ".agents/AGENTS.md")
    return tmp_path


def _checkout_alias(root: Path, target: bytes = b"AGENTS.md") -> Path:
    object_id = _git(root, "hash-object", "-w", "--stdin", input=target).decode().strip()
    relative = ".agents/CLAUDE.md"
    _git(root, "update-index", "--add", "--cacheinfo", f"120000,{object_id},{relative}")
    _git(root, "-c", "core.symlinks=false", "checkout-index", "--force", "--", relative)
    path = root / relative
    assert not path.is_symlink()
    return path


def _navigation_errors(root: Path) -> list[str]:
    errors: list[str] = []
    validator.check_directory_navigation(root, validator.project_files(root), errors)
    return errors


def test_git_symlink_placeholder_does_not_need_duplicate_index_entry(repository: Path):
    _checkout_alias(repository)

    assert _navigation_errors(repository) == []


def test_regular_claude_file_still_requires_directory_index(repository: Path):
    path = repository / ".agents" / "CLAUDE.md"
    path.write_bytes(b"AGENTS.md")
    _git(repository, "add", ".agents/CLAUDE.md")

    assert _navigation_errors(repository) == [
        "management file is absent from its directory index: .agents/CLAUDE.md"
    ]


@pytest.mark.parametrize("content", [b"custom instructions", b"AGENTS.md\n"])
def test_modified_placeholder_still_requires_directory_index(repository: Path, content: bytes):
    _checkout_alias(repository).write_bytes(content)

    assert _navigation_errors(repository) == [
        "management file is absent from its directory index: .agents/CLAUDE.md"
    ]


def test_indexed_symlink_must_target_same_directory_agents_file(repository: Path):
    _checkout_alias(repository, target=b"../AGENTS.md").write_bytes(b"AGENTS.md")

    assert _navigation_errors(repository) == [
        "management file is absent from its directory index: .agents/CLAUDE.md"
    ]
