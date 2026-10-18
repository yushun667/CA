"""compile_commands.json loading/normalization tests (owner: build)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from ca.build import (
    infer_repo_root,
    language_for_path,
    load_compilation_database,
    units_to_entries,
    write_compilation_database,
)
from ca.core.ids import tu_id

FIXTURES = Path(__file__).resolve().parent / "fixtures"
COMPDB_FIXTURE = FIXTURES / "build_compile_commands.json"


def _copy_fixture(tmp_path: Path, relative: str = "compile_commands.json") -> Path:
    destination = tmp_path / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(COMPDB_FIXTURE, destination)
    return destination


def test_language_for_path() -> None:
    assert language_for_path("a/b.c") == "c"
    assert language_for_path("a/b.C") == "c++"
    assert language_for_path("b.cpp") == "c++"
    assert language_for_path("b.cxx") == "c++"
    assert language_for_path("b.S") is None
    assert language_for_path("b.o") is None


def test_load_from_file_normalizes_entries(tmp_path: Path) -> None:
    compdb = _copy_fixture(tmp_path)
    root = str(tmp_path.resolve())
    units = load_compilation_database(compdb, repo_root=tmp_path)

    assert len(units) == 4
    by_file = {Path(unit.file).name: unit for unit in units}
    assert set(by_file) == {"main.c", "util.cpp", "wrapped.c", "no_directory.c"}

    main = by_file["main.c"]
    assert main.file == str(tmp_path / "src" / "main.c")
    assert main.directory == root
    assert main.repo_root == root
    assert main.source == "compile_commands"
    assert main.language == "c"
    assert main.driver == "/usr/bin/cc"
    assert main.arguments == (
        "-Iinclude",
        "-DDEBUG=1",
        "-c",
        "-o",
        "build/main.o",
        "src/main.c",
    )
    assert main.id == tu_id(main.file, main.directory, main.arguments)

    cpp = by_file["util.cpp"]
    assert cpp.language == "c++"
    assert cpp.driver == "/usr/bin/c++"
    assert cpp.arguments[0] == "-std=c++17"
    assert cpp.arguments[1:3] == ("-I", "include")

    # ccache prefix is unwrapped: the real driver is kept, wrapper dropped.
    wrapped = by_file["wrapped.c"]
    assert wrapped.driver == "/usr/bin/clang"
    assert wrapped.arguments[0] == "-O2"

    # Missing "directory" falls back to the database directory.
    no_directory = by_file["no_directory.c"]
    assert no_directory.directory == root


def test_filtered_bad_entries_are_skipped(tmp_path: Path) -> None:
    compdb = _copy_fixture(tmp_path)
    units = load_compilation_database(compdb, repo_root=tmp_path)
    assert {Path(unit.file).name for unit in units} == {
        "main.c",
        "util.cpp",
        "wrapped.c",
        "no_directory.c",
    }
    # .S/.o files, missing file, missing command and non-object entries dropped.
    assert all(not unit.file.endswith(".S") for unit in units)
    assert all(not unit.file.endswith(".o") for unit in units)


def test_load_from_directory_discovers_compdb(tmp_path: Path) -> None:
    _copy_fixture(tmp_path, "compile_commands.json")
    units = load_compilation_database(tmp_path, repo_root=tmp_path)
    assert len(units) == 4


def test_load_from_directory_finds_common_build_dir(tmp_path: Path) -> None:
    _copy_fixture(tmp_path, "build/compile_commands.json")
    units = load_compilation_database(tmp_path, repo_root=tmp_path)
    assert len(units) == 4
    assert units[0].directory == str(tmp_path.resolve() / "build")


def test_repo_root_inferred_from_git_marker(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    _copy_fixture(tmp_path, "build/compile_commands.json")
    units = load_compilation_database(tmp_path / "build")
    assert units[0].repo_root == str(tmp_path.resolve())
    assert infer_repo_root(tmp_path / "build") == str(tmp_path.resolve())


def test_repo_root_falls_back_to_compdb_directory(tmp_path: Path) -> None:
    _copy_fixture(tmp_path, "build/compile_commands.json")
    units = load_compilation_database(tmp_path / "build" / "compile_commands.json")
    assert units[0].repo_root == str((tmp_path / "build").resolve())


def test_command_string_form_uses_shlex(tmp_path: Path) -> None:
    compdb = tmp_path / "compile_commands.json"
    compdb.write_text(
        json.dumps(
            [
                {
                    "directory": str(tmp_path),
                    "file": "quoted.c",
                    "command": "cc -DFOO='a b' -DPATH=\"/usr/local\" -c quoted.c",
                }
            ]
        ),
        encoding="utf-8",
    )
    units = load_compilation_database(compdb, repo_root=tmp_path)
    assert len(units) == 1
    assert units[0].driver == "cc"
    assert units[0].arguments == ("-DFOO=a b", "-DPATH=/usr/local", "-c", "quoted.c")


def test_duplicate_entries_are_deduplicated(tmp_path: Path) -> None:
    entry = {
        "directory": str(tmp_path),
        "file": "dup.c",
        "arguments": ["cc", "-c", "dup.c"],
    }
    compdb = tmp_path / "compile_commands.json"
    compdb.write_text(json.dumps([entry, dict(entry)]), encoding="utf-8")
    units = load_compilation_database(compdb, repo_root=tmp_path)
    assert len(units) == 1


def test_no_parseable_entry_raises_with_reason(tmp_path: Path) -> None:
    compdb = tmp_path / "compile_commands.json"
    compdb.write_text(
        json.dumps(
            [
                {"directory": ".", "file": "boot.S", "command": "cc -c boot.S"},
                {"directory": ".", "file": "lib.o", "command": "cc -c lib.o"},
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as excinfo:
        load_compilation_database(compdb, repo_root=tmp_path)
    message = str(excinfo.value)
    assert "no parseable C/C++ translation units" in message
    assert "unsupported-language:.S" in message


def test_empty_database_raises(tmp_path: Path) -> None:
    compdb = tmp_path / "compile_commands.json"
    compdb.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError):
        load_compilation_database(compdb, repo_root=tmp_path)


def test_missing_database_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_compilation_database(tmp_path / "compile_commands.json")
    with pytest.raises(FileNotFoundError):
        load_compilation_database(tmp_path)


def test_multiple_candidates_raises(tmp_path: Path) -> None:
    _copy_fixture(tmp_path, "generated_a/compile_commands.json")
    _copy_fixture(tmp_path, "generated_b/compile_commands.json")
    with pytest.raises(ValueError) as excinfo:
        load_compilation_database(tmp_path)
    assert "multiple" in str(excinfo.value)


def test_tu_id_is_stable_across_loads_and_round_trip(tmp_path: Path) -> None:
    compdb = _copy_fixture(tmp_path)
    first = load_compilation_database(compdb, repo_root=tmp_path)
    second = load_compilation_database(compdb, repo_root=tmp_path)
    assert [unit.id for unit in first] == [unit.id for unit in second]

    round_trip = tmp_path / "roundtrip" / "compile_commands.json"
    write_compilation_database(first, round_trip)
    reloaded = load_compilation_database(round_trip, repo_root=tmp_path)
    assert [unit.id for unit in reloaded] == [unit.id for unit in first]
    assert [unit.arguments for unit in reloaded] == [unit.arguments for unit in first]
    assert all(unit.source == "compile_commands" for unit in reloaded)

    entries = units_to_entries(first)
    assert entries[0]["arguments"][0] == first[0].driver


def test_compile_commands_expands_response_files(tmp_path: Path) -> None:
    """CMake/Ninja include response files must become ordinary arguments."""

    build = tmp_path / "build"
    build.mkdir()
    source = tmp_path / "src" / "unit.c"
    source.parent.mkdir()
    source.write_text("int main(void) { return 0; }\n", encoding="utf-8")
    rsp = build / "CMakeFiles" / "obj.dir" / "includes_C.rsp"
    rsp.parent.mkdir(parents=True)
    rsp.write_text("-I" + str(tmp_path / "include") + " -DTEST_MACRO=1\n", encoding="utf-8")
    compdb = build / "compile_commands.json"
    compdb.write_text(
        json.dumps(
            [
                {
                    "directory": str(build),
                    "file": str(source),
                    "arguments": [
                        "clang",
                        "-c",
                        str(source),
                        "@CMakeFiles/obj.dir/includes_C.rsp",
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )

    units = load_compilation_database(str(compdb), str(tmp_path))

    assert len(units) == 1
    assert "-I" + str(tmp_path / "include") in units[0].arguments
    assert "-DTEST_MACRO=1" in units[0].arguments
    assert "@CMakeFiles/obj.dir/includes_C.rsp" not in units[0].arguments


def test_compile_commands_keeps_missing_response_file(tmp_path: Path) -> None:
    """A missing response file is preserved instead of silently dropped."""

    source = tmp_path / "unit.c"
    source.write_text("int main(void) { return 0; }\n", encoding="utf-8")
    compdb = tmp_path / "compile_commands.json"
    compdb.write_text(
        json.dumps(
            [
                {
                    "directory": str(tmp_path),
                    "file": str(source),
                    "arguments": ["clang", "-c", str(source), "@missing.rsp"],
                }
            ]
        ),
        encoding="utf-8",
    )

    units = load_compilation_database(str(compdb), str(tmp_path))

    assert "@missing.rsp" in units[0].arguments
