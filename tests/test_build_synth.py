"""synthesize_for_files explicit-degradation tests (owner: build)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ca.build import synthesize_for_files
from ca.core.ids import tu_id


def test_synthesized_units_are_marked_and_normalized(tmp_path: Path) -> None:
    (tmp_path / "a.c").write_text("int a(void) { return 0; }\n", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.c").write_text("int b(void) { return 0; }\n", encoding="utf-8")

    units = synthesize_for_files(["a.c", tmp_path / "sub" / "b.c"], repo_root=tmp_path)

    assert len(units) == 2
    for unit in units:
        assert unit.source == "synthesized"
        assert unit.source != "compile_commands"
        assert unit.language == "c"
        assert unit.driver == "cc"
        assert unit.arguments == ()
        assert unit.directory == str(tmp_path.resolve())
        assert unit.repo_root == str(tmp_path.resolve())
        assert Path(unit.file).is_absolute()
        assert unit.id == tu_id(unit.file, unit.directory, unit.arguments)
    assert {Path(unit.file).name for unit in units} == {"a.c", "b.c"}


def test_accepts_a_single_path_and_extra_args(tmp_path: Path) -> None:
    units = synthesize_for_files(
        "only.c", repo_root=tmp_path, language="c++", extra_args=("-std=c++17", "-I.")
    )
    assert len(units) == 1
    assert units[0].source == "synthesized"
    assert units[0].language == "c++"
    assert units[0].driver == "c++"
    assert units[0].arguments == ("-std=c++17", "-I.")


def test_language_aliases() -> None:
    units = synthesize_for_files(["x.cpp"], repo_root=Path.cwd(), language="cpp")
    assert units[0].language == "c++"


def test_unknown_language_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        synthesize_for_files(["x.c"], repo_root=tmp_path, language="rust")


def test_ids_are_stable_and_deduplicated(tmp_path: Path) -> None:
    first = synthesize_for_files(["a.c", "a.c"], repo_root=tmp_path)
    second = synthesize_for_files([tmp_path / "a.c"], repo_root=tmp_path)
    assert len(first) == 1
    assert first[0].id == second[0].id
