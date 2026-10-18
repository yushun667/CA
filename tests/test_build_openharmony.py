"""Pure-logic tests for the OpenHarmony corpus helpers.

No server, Docker or 30 GB checkout is touched: manifest parsing, compdb
filtering, directory statistics, command construction and metadata writing are
plain functions.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ca.build import openharmony as oh
from ca.build import load_compilation_database

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MANIFEST_FIXTURE = FIXTURES / "build_openharmony_manifest.xml"
COMPDB_FIXTURE = FIXTURES / "build_openharmony_compdb.json"


def _fixture_entries(tmp_path: Path) -> list[dict]:
    out_dir = tmp_path / "out" / "rk3588"
    out_dir.mkdir(parents=True)
    entries = json.loads(COMPDB_FIXTURE.read_text(encoding="utf-8"))
    for entry in entries:
        if isinstance(entry, dict) and entry.get("directory") == "OUT":
            entry["directory"] = str(out_dir)
    return entries


def test_parse_repo_manifest() -> None:
    projects, includes = oh.parse_repo_manifest(MANIFEST_FIXTURE.read_text(encoding="utf-8"))
    assert includes == ["ohos/standard/default.xml"]
    assert len(projects) == 4
    by_path = {project.path: project for project in projects}
    assert by_path["base"].name == "openharmony/base"
    assert by_path["base"].groups == ("default", "ohos:standard")
    assert by_path["base"].revision.startswith("2222")
    assert by_path["vendor/hihope"].name == "openharmony/vendor_hihope"
    assert oh.project_distribution(projects) == {
        "base": 1,
        "build": 1,
        "third_party/musl": 1,
        "vendor/hihope": 1,
    }


def test_parse_repo_manifest_rejects_invalid_xml() -> None:
    with pytest.raises(oh.OpenHarmonyCorpusError):
        oh.parse_repo_manifest("<manifest><project name='x'></manifest>")


def test_parse_assignments() -> None:
    text = (
        "# comment\n"
        'ohos_version = "OpenHarmony-6.1.0.1"\n'
        "product_name = 'opi5plus'\n"
        "api_version = 12;\n"
        "not an assignment\n"
    )
    assert oh.parse_assignments(text) == {
        "ohos_version": "OpenHarmony-6.1.0.1",
        "product_name": "opi5plus",
        "api_version": "12",
    }


def test_filter_compilation_database(tmp_path: Path) -> None:
    result = oh.filter_compilation_database(_fixture_entries(tmp_path))
    assert result.raw_count == 10
    assert result.tu_count == 4
    assert result.filtered_count == 6
    assert result.filtered == {
        "entry-not-an-object": 1,
        "missing-command": 1,
        "duplicate": 1,
        "unsupported-language:.S": 1,
        "unsupported-language:.h": 1,
        "unsupported-language:.o": 1,
    }
    names = sorted(Path(entry["file"]).name for entry in result.kept)
    assert names == ["dup.c", "init_main.c", "ipc_skeleton.cpp", "printf.c"]
    assert all(Path(entry["file"]).is_absolute() for entry in result.kept)
    assert result.summary()["filtered_reasons"]["unsupported-language:.S"] == 1


def test_filter_rejects_non_array() -> None:
    with pytest.raises(oh.OpenHarmonyCorpusError):
        oh.filter_compilation_database({"not": "a list"})


def test_directory_and_language_distribution(tmp_path: Path) -> None:
    result = oh.filter_compilation_database(_fixture_entries(tmp_path))
    distribution = oh.directory_distribution(result.kept, tmp_path)
    assert distribution == {
        "base/startup": 2,
        "foundation/communication": 1,
        "third_party/musl": 1,
    }
    summary = oh.summarize_compdb(result.kept)
    assert summary["tu_count"] == 4
    assert summary["languages"] == {"c": 3, "c++": 1}
    assert summary["suffixes"] == {".c": 3, ".cpp": 1}


def test_command_construction(tmp_path: Path) -> None:
    gn = oh.prebuilt_tool(tmp_path, "gn")
    ninja = oh.prebuilt_tool(tmp_path, "ninja")
    assert str(gn).endswith("prebuilts/build-tools/linux-x86/bin/gn")
    assert oh.ninja_compdb_command(ninja, tmp_path / "out") == [
        str(ninja),
        "-C",
        str(tmp_path / "out"),
        "-t",
        "compdb",
    ]
    assert oh.gn_gen_command(gn, "out/rk3588", gn_args="is_debug=false") == [
        str(gn),
        "gen",
        "out/rk3588",
        "--args=is_debug=false",
        "--export-compile-commands",
    ]
    docker = oh.docker_command("android12-rk3588-builder:latest", tmp_path, ["bash", "-lc", "gn --version"])
    assert docker[:2] == ["docker", "run"]
    assert "--rm" in docker
    assert f"{tmp_path.resolve()}:{tmp_path.resolve()}" in docker
    assert docker[-3:] == ["bash", "-lc", "gn --version"]


def test_tool_versions_without_prebuilts(tmp_path: Path) -> None:
    versions = oh.tool_versions(tmp_path)
    assert versions["gn_exists"] == "False"
    assert versions["ninja_exists"] == "False"
    assert versions["gn"] == ""


def test_target_out_dir(tmp_path: Path) -> None:
    target = oh.OpenHarmonyTarget()
    assert target.out_name == "rk3588"
    assert target.out_dir(tmp_path) == tmp_path / "out" / "rk3588"
    assert (target.product, target.board, target.os_level) == ("opi5plus", "rk3588", "standard")


def test_build_metadata_and_writes(tmp_path: Path) -> None:
    target = oh.OpenHarmonyTarget()
    result = oh.filter_compilation_database(_fixture_entries(tmp_path))
    metadata = oh.build_metadata(
        target=target,
        source_root=tmp_path,
        raw_count=result.raw_count,
        entries=result.kept,
        filtered=result.filtered,
        version="OpenHarmony-6.1.0.1",
        manifest_commit="manifest-sha",
        tools={"gn": "2023", "ninja": "1.12.1"},
        commands={"gn_gen": "gn gen out/rk3588", "ninja_compdb": "ninja -t compdb"},
        timings={"gn_seconds": 1.234},
    )
    assert metadata["ecosystem"] == "openharmony"
    assert metadata["product"] == "opi5plus"
    assert metadata["manifest_commit"] == "manifest-sha"
    assert metadata["compdb"]["raw_entries"] == 10
    assert metadata["compdb"]["tu_count"] == 4
    assert metadata["compdb"]["filtered_entries"] == 6
    assert metadata["compdb"]["languages"] == {"c": 3, "c++": 1}
    assert metadata["directory_distribution"] == {
        "base/startup": 2,
        "foundation/communication": 1,
        "third_party/musl": 1,
    }
    assert metadata["timings_seconds"] == {"gn_seconds": 1.23}

    compdb_path = tmp_path / "corpus" / "compile_commands.json"
    metadata_path = tmp_path / "corpus" / "metadata.json"
    oh.write_compilation_database(result.kept, compdb_path)
    oh.write_metadata(metadata, metadata_path)
    written = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert written["compdb"]["tu_count"] == 4
    units = load_compilation_database(compdb_path, repo_root=tmp_path)
    assert len(units) == 4
    assert {unit.language for unit in units} == {"c", "c++"}
    assert all(unit.source == "compile_commands" for unit in units)


def test_select_unique_commands_prefers_arm64_non_test(tmp_path: Path) -> None:
    source = tmp_path / "foundation" / "foo"
    entries = [
        {
            "directory": str(tmp_path / "out"),
            "file": str(source / "bar.cpp"),
            "command": "clang++ --target=x86_64-linux-gnu -DOHOS_STANDARD_SYSTEM -c bar.cpp -o host.o",
        },
        {
            "directory": str(tmp_path / "out"),
            "file": str(source / "bar.cpp"),
            "command": "clang++ --target=aarch64-linux-ohos -DOHOS_STANDARD_SYSTEM -c bar.cpp -o device.o",
        },
        {
            "directory": str(tmp_path / "out"),
            "file": str(source / "bar.cpp"),
            "command": "clang++ --target=aarch64-linux-ohos -c bar.cpp",
        },
        {
            "directory": str(tmp_path / "out"),
            "file": str(tmp_path / "out" / "gen" / "generated.c"),
            "command": "clang -c generated.c",
        },
        {
            "directory": str(tmp_path / "out"),
            "file": str(tmp_path / "prebuilts" / "tool.c"),
            "command": "clang -c tool.c",
        },
    ]
    selection = oh.select_unique_commands(entries, source_root=tmp_path)
    assert selection.stats["tu_count"] == 1
    assert selection.stats["unique_files"] == 1
    assert selection.stats["files_with_variants"] == 1
    assert selection.stats["dropped_variants"] == 2
    assert selection.stats["dropped_generated_entries"] == 2
    chosen = selection.entries[0]
    assert chosen["variants_total"] == 3
    assert chosen["variants_dropped"] == 2
    assert "aarch64-linux-ohos" in chosen["command"]
    assert chosen["command"].endswith("-c bar.cpp")


def test_select_unique_commands_keeps_test_files_when_needed(tmp_path: Path) -> None:
    source = tmp_path / "src"
    entry = {
        "directory": str(tmp_path / "out"),
        "file": str(source / "test" / "unittest" / "x.cpp"),
        "command": "clang++ --target=aarch64-linux-ohos -c x.cpp",
    }
    selection = oh.select_unique_commands([entry], source_root=source)
    assert selection.stats["tu_count"] == 1
    assert selection.stats["by_top1"] == {"test": 1}


def test_write_unique_compdb_roundtrip(tmp_path: Path) -> None:
    entry = {
        "directory": str(tmp_path / "out"),
        "file": str(tmp_path / "base" / "a.cc"),
        "command": "clang++ --target=aarch64-linux-ohos -c a.cc -o a.o",
    }
    selection = oh.select_unique_commands([entry], source_root=tmp_path)
    path = oh.write_unique_compdb(selection, tmp_path / "unique.json")
    units = load_compilation_database(path, repo_root=tmp_path)
    assert len(units) == 1
    assert units[0].file == str(tmp_path / "base" / "a.cc")


def test_shard_plan_maps_paths(tmp_path: Path) -> None:
    entries = [
        {"file": str(tmp_path / "foundation" / "bundlemanager" / "a.c")},
        {"file": str(tmp_path / "foundation" / "arkui" / "b.cpp")},
        {"file": str(tmp_path / "foundation" / "ability" / "c.cpp")},
        {"file": str(tmp_path / "base" / "d.c")},
        {"file": str(tmp_path / "third_party" / "e.cc")},
        {"file": str(tmp_path / "drivers" / "f.c")},
        {"file": str(tmp_path / "kernel" / "g.c")},
        {"file": str(tmp_path / "misc" / "h.c")},
    ]
    shards, counts = oh.shard_plan(entries, tmp_path)
    assert counts == {"S1": 2, "S2": 1, "S3": 0, "S4": 1, "S5": 1, "S6": 0, "S7": 2, "S8": 1}
    assert sum(counts.values()) == len(entries)
    assert shards["S8"][0]["file"].endswith("h.c")


def test_shard_for_path_fallback() -> None:
    assert oh.shard_for_path("/nowhere/x.c", "/repo") == "S8"
    assert oh.shard_for_path("/repo/foundation/ability/x.c", "/repo") == "S1"
