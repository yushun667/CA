"""Linux kernel .cmd reconstruction tests (owner: build)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ca.build import reconstruct_kernel_commands
from ca.core.ids import tu_id

FIXTURES = Path(__file__).resolve().parent / "fixtures"
KERNEL_FIXTURE = FIXTURES / "build_kernel"


@pytest.fixture(scope="module")
def units():
    return reconstruct_kernel_commands(KERNEL_FIXTURE, repo_root=KERNEL_FIXTURE)


def test_reconstructs_only_c_translation_units(units) -> None:
    by_name = {Path(unit.file).name: unit for unit in units}
    assert set(by_name) == {"core.c", "timer.c", "foo.c"}
    for unit in units:
        assert unit.source == "kernel_cmd"
        assert unit.language == "c"
        assert unit.directory == str(KERNEL_FIXTURE.resolve())
        assert unit.repo_root == str(KERNEL_FIXTURE.resolve())
        assert Path(unit.file).is_file()
        assert unit.id == tu_id(unit.file, unit.directory, unit.arguments)


def test_reconstructs_flags_and_strips_noise(units) -> None:
    core = {Path(unit.file).name: unit for unit in units}["core.c"]
    arguments = list(core.arguments)

    assert core.driver == "gcc"
    assert "-o" not in arguments
    assert "kernel/sched/core.o" not in arguments
    assert not any(argument.startswith("-Wp,-MMD,") for argument in arguments)
    assert "-D__KERNEL__" in arguments
    assert '-DKBUILD_MODNAME="sched"' in arguments
    assert "-I./include" in arguments
    assert "-I./arch/x86/include" in arguments
    assert "-isystem" in arguments
    # -include flags and their arguments survive.
    include_indexes = [i for i, argument in enumerate(arguments) if argument == "-include"]
    assert len(include_indexes) == 2
    assert arguments[include_indexes[0] + 1] == "include/linux/compiler-version.h"
    assert arguments[-1] == "kernel/sched/core.c"


def test_multiline_cmd_is_joined_and_driver_preserved(units) -> None:
    timer = {Path(unit.file).name: unit for unit in units}["timer.c"]
    assert timer.driver == "gcc"
    assert '-DKBUILD_MODNAME="timer"' in timer.arguments
    assert "kernel/time/timer.c" in timer.arguments


def test_legacy_cmd_filename_is_supported(units) -> None:
    foo = {Path(unit.file).name: unit for unit in units}["foo.c"]
    assert foo.driver == "clang"
    assert any(argument.startswith("-DKBUILD_MODNAME=") for argument in foo.arguments)


def test_missing_build_dir_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        reconstruct_kernel_commands(tmp_path / "nope", repo_root=tmp_path)


def test_directory_without_cmd_files_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError) as excinfo:
        reconstruct_kernel_commands(tmp_path, repo_root=tmp_path)
    assert "no C/C++ compile commands" in str(excinfo.value)
