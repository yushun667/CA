"""Corpus helpers tests (owner: build); no network access."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ca.build.corpus import _find_toolchain, fetch_xv6, xv6_path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_xv6_path_defaults_under_tmp_corpus() -> None:
    expected = REPO_ROOT / ".tmp" / "corpus" / "xv6-riscv"
    assert xv6_path() == expected
    assert xv6_path(REPO_ROOT) == expected


def test_xv6_path_custom_repo_root(tmp_path: Path) -> None:
    assert xv6_path(tmp_path) == tmp_path / ".tmp" / "corpus" / "xv6-riscv"


def test_fetch_xv6_reuses_existing_checkout(tmp_path: Path) -> None:
    checkout = tmp_path / "xv6-riscv"
    (checkout / "kernel").mkdir(parents=True)
    (checkout / "Makefile").write_text("all:\n", encoding="utf-8")
    assert fetch_xv6(checkout) == checkout


def test_fetch_xv6_rejects_unrelated_directory(tmp_path: Path) -> None:
    junk = tmp_path / "junk"
    junk.mkdir()
    (junk / "README").write_text("not xv6\n", encoding="utf-8")
    with pytest.raises(FileExistsError):
        fetch_xv6(junk)


def _fake_toolchain(root: Path, prefix: str) -> Path:
    bin_dir = root / "xpack-riscv-none-elf-gcc-15.2.0-1" / "bin"
    bin_dir.mkdir(parents=True)
    for suffix in ("gcc", "objdump", "ld"):
        tool = bin_dir / f"{prefix}{suffix}"
        tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        tool.chmod(0o755)
    return bin_dir


def test_find_toolchain_locates_gcc_objdump_pair(tmp_path: Path) -> None:
    bin_dir = _fake_toolchain(tmp_path, "riscv-none-elf-")
    found_bin, prefix = _find_toolchain(tmp_path)
    assert found_bin == bin_dir
    assert prefix == "riscv-none-elf-"


def test_find_toolchain_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        _find_toolchain(tmp_path)


def test_build_xv6_compdb_with_fake_checkout_and_toolchain(tmp_path: Path) -> None:
    """Hermetic end-to-end check of the capture-based corpus builder."""

    from ca.build.corpus import build_xv6_compdb

    checkout = tmp_path / "mini-xv6"
    (checkout / "kernel").mkdir(parents=True)
    (checkout / "user").mkdir()
    (checkout / "kernel" / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    (checkout / "user" / "cat.c").write_text("int cat(void) { return 0; }\n", encoding="utf-8")
    (checkout / "Makefile").write_text(
        "CC := $(TOOLPREFIX)gcc\n"
        "LD := $(TOOLPREFIX)ld\n"
        "kernel/kernel: kernel/main.o\n"
        "\t$(LD) -o $@ $<\n"
        "kernel/%.o: kernel/%.c\n"
        "\t$(CC) -DFOO=1 -c -o $@ $<\n"
        "user/%.o: user/%.c\n"
        "\t$(CC) -DFOO=1 -c -o $@ $<\n",
        encoding="utf-8",
    )

    fake_root = tmp_path / "toolchain"
    bin_dir = fake_root / "xpack" / "bin"
    bin_dir.mkdir(parents=True)
    for suffix in ("gcc", "objdump", "ld"):
        tool = bin_dir / f"riscv-none-elf-{suffix}"
        tool.write_text(
            '#!/bin/sh\nif [ "$1" = "--version" ]; then echo fake; exit 0; fi\nexit 0\n',
            encoding="utf-8",
        )
        tool.chmod(0o755)

    units = build_xv6_compdb(fake_root, dest=checkout, jobs=2)
    assert {Path(unit.file).name for unit in units} == {"main.c", "cat.c"}
    # The RV32-default cross driver gets -mabi=lp64d injected *and recorded*.
    for unit in units:
        assert "-mabi=lp64d" in unit.arguments
    compdb = checkout / "compile_commands.json"
    assert compdb.is_file()
    from ca.build import load_compilation_database

    reloaded = load_compilation_database(compdb)
    assert {unit.id for unit in reloaded} == {unit.id for unit in units}
    assert all(unit.source == "capture" for unit in units)


def test_linux_kernel_notes_document_ci_path() -> None:
    from ca.build.corpus import LINUX_KERNEL_COMPDB_NOTES

    assert "make LLVM=1" in LINUX_KERNEL_COMPDB_NOTES
    assert "gen_compile_commands.py" in LINUX_KERNEL_COMPDB_NOTES
    assert "macOS" in LINUX_KERNEL_COMPDB_NOTES
