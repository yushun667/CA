"""Pure-logic tests for the container-based Linux kernel corpus builder.

No container, network or 1.6 GB source tree is touched here: the module keeps
command construction, container-state parsing, checksum/path/metadata logic in
plain functions precisely so this test file can run anywhere.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ca.build import kernel_corpus as kc


def test_spec_paths_and_validation(tmp_path: Path) -> None:
    spec = kc.KernelCorpusSpec(version="6.12.113")
    assert spec.tag == "v6.12.113"
    assert spec.tarball_name == "linux-6.12.113.tar.xz"
    assert spec.source_dirname == "linux-6.12.113"
    assert spec.source_dir(tmp_path) == tmp_path / "linux-6.12.113"
    assert spec.compdb_path(tmp_path) == tmp_path / "compile_commands.json"
    assert spec.metadata_path(tmp_path) == tmp_path / "metadata.json"
    assert spec.container_workdir() == "/work/linux-6.12.113"
    assert "linux-6.12.113.tar.xz" in spec.tarball_url
    with pytest.raises(ValueError):
        kc.KernelCorpusSpec(version="serial")


def test_find_container_cli_reports_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setattr(kc, "_DEFAULT_CONTAINER_CLI_CANDIDATES", ())
    with pytest.raises(kc.ContainerUnavailableError) as excinfo:
        kc.find_container_cli()
    assert "container" in str(excinfo.value)
    assert "synthesized" in str(excinfo.value)
    with pytest.raises(kc.ContainerUnavailableError):
        kc.find_container_cli(str(tmp_path / "no-such-container"))


def test_container_commands(tmp_path: Path) -> None:
    spec = kc.KernelCorpusSpec(cpus=6, memory="6g", jobs=6)
    create = kc.container_create_command(spec, tmp_path, cli="container")
    assert create[:3] == ["container", "create", "--name"]
    assert "ca-kernel" in create
    assert create[create.index("-c") + 1] == "6"
    assert create[create.index("-m") + 1] == "6g"
    mount = create[create.index("--mount") + 1]
    assert mount == f"type=bind,source={tmp_path.resolve()},target=/work"
    assert create[-2:] == ["sleep", "infinity"]

    run = kc.container_exec_command(
        spec, ["make", "defconfig"], cli="container", workdir="/work/linux-6.12.113"
    )
    assert run == [
        "container",
        "exec",
        "-w",
        "/work/linux-6.12.113",
        "ca-kernel",
        "make",
        "defconfig",
    ]


def test_build_commands(tmp_path: Path) -> None:
    spec = kc.KernelCorpusSpec(jobs=4)
    assert kc.make_defconfig_command(spec) == [
        "make",
        "ARCH=arm64",
        "LLVM=1",
        "defconfig",
    ]
    build = kc.make_build_command(spec, ("kernel/", "mm/"))
    assert build == ["make", "-j4", "ARCH=arm64", "LLVM=1", "kernel/", "mm/"]
    assert kc.gen_compile_commands_command(spec)[:2] == [
        "python3",
        "scripts/clang-tools/gen_compile_commands.py",
    ]
    apt = kc.apt_install_command(spec)
    assert apt[0] == "bash"
    for package in ("clang", "lld", "llvm", "flex", "bison", "libelf-dev", "dwarves"):
        assert package in apt[-1]
    assert kc.format_command(["a b", "c"]) == "'a b' c"


def test_parse_container_list_real_shape() -> None:
    payload = json.dumps(
        [
            {"id": "buildkit", "status": {"state": "stopped"}},
            {"id": "ca-kernel", "status": {"state": "running"}},
            {"name": "flat", "state": "Running"},
        ]
    )
    assert kc.parse_container_list(payload) == {
        "buildkit": "stopped",
        "ca-kernel": "running",
        "flat": "running",
    }
    with pytest.raises(kc.KernelCorpusError):
        kc.parse_container_list("not json")


def test_expected_sha256_parsing() -> None:
    text = (
        "abc123   linux-6.12.113.tar.xz\n"
        "e09b6727e5c521253f0d41694900b9c38fb7604db7d35238a8197aa9f7de2428  "
        "*linux-6.12.113.tar.xz\n"
    )
    assert (
        kc.expected_sha256(text, "linux-6.12.113.tar.xz")
        == "e09b6727e5c521253f0d41694900b9c38fb7604db7d35238a8197aa9f7de2428"
    )
    assert kc.expected_sha256(text, "linux-6.6.1.tar.xz") is None


def test_file_sha256(tmp_path: Path) -> None:
    target = tmp_path / "blob"
    target.write_bytes(b"ca")
    digest = kc.file_sha256(target)
    assert len(digest) == 64
    assert all(character in "0123456789abcdef" for character in digest)
    assert kc.file_sha256(target) == digest


def test_is_kernel_checkout(tmp_path: Path) -> None:
    fake = tmp_path / "linux-x"
    fake.mkdir()
    (fake / "Makefile").write_text("VERSION = 6\nPATCHLEVEL = 12\n", encoding="utf-8")
    assert kc.is_kernel_checkout(fake) is True
    assert kc.is_kernel_checkout(tmp_path) is False


def test_fetch_kernel_source_reuses_checkout(tmp_path: Path) -> None:
    source = tmp_path / "linux-6.12.113"
    source.mkdir()
    (source / "Makefile").write_text("VERSION = 6\n", encoding="utf-8")
    spec = kc.KernelCorpusSpec()
    assert kc.fetch_kernel_source(spec, tmp_path) == source
    assert not (tmp_path / "linux-6.12.113.tar.xz").exists()


def test_normalize_database_paths(tmp_path: Path) -> None:
    database = tmp_path / "compile_commands.json"
    database.write_text(
        json.dumps(
            [
                {
                    "directory": "/work/linux-6.12.113",
                    "file": "/work/linux-6.12.113/kernel/a.c",
                    "command": "/usr/bin/clang -I./include -c kernel/a.c",
                },
                {
                    "directory": "/work/linux-6.12.113/fs",
                    "file": "/work/linux-6.12.113/fs/b.c",
                    "command": "clang -c fs/b.c",
                },
            ]
        ),
        encoding="utf-8",
    )
    host = tmp_path / "linux-6.12.113"
    host.mkdir()
    kc.normalize_database_paths(
        database, container_root="/work/linux-6.12.113", host_root=host
    )
    entries = json.loads(database.read_text(encoding="utf-8"))
    assert entries[0]["directory"] == str(host)
    assert entries[0]["file"] == str(host / "kernel" / "a.c")
    assert entries[0]["command"].startswith("/usr/bin/clang -I./include")
    assert entries[1]["directory"] == str(host / "fs")

    untouched = tmp_path / "other.json"
    untouched.write_text(json.dumps([{"directory": "/x", "file": "/x/a.c"}]), encoding="utf-8")
    with pytest.raises(kc.KernelCorpusError):
        kc.normalize_database_paths(untouched, container_root="/work", host_root=host)


def test_summarize_compilation_database(tmp_path: Path) -> None:
    database = tmp_path / "compile_commands.json"
    database.write_text(
        json.dumps(
            [
                {"directory": str(tmp_path), "file": "a.c", "command": "clang -c a.c"},
                {
                    "directory": str(tmp_path),
                    "file": "b.cpp",
                    "command": "clang++ -std=c++17 -c b.cpp",
                },
                {"directory": str(tmp_path), "file": "head.S", "command": "clang -c head.S"},
            ]
        ),
        encoding="utf-8",
    )
    summary = kc.summarize_compilation_database(database, repo_root=tmp_path)
    assert summary["tu_count"] == 2
    assert summary["raw_entries"] == 3
    assert summary["filtered_entries"] == 1
    assert summary["languages"] == {"c": 1, "c++": 1}
    assert summary["sources"] == {"compile_commands": 2}


def test_install_corpus_writes_metadata(tmp_path: Path) -> None:
    spec = kc.KernelCorpusSpec()
    database = tmp_path / "build" / "compile_commands.json"
    database.parent.mkdir()
    database.write_text("[]", encoding="utf-8")
    corpus = tmp_path / "corpus"
    installed = kc.install_corpus(
        spec, database, corpus, metadata={"container_image_digest": "sha256:x"}
    )
    assert Path(installed["compdb"]).is_file()
    metadata = json.loads(Path(installed["metadata"]).read_text(encoding="utf-8"))
    assert metadata["kernel_version"] == "6.12.113"
    assert metadata["kernel_tag"] == "v6.12.113"
    assert metadata["arch"] == "arm64"
    assert metadata["config"] == "defconfig"
    assert metadata["container_image_digest"] == "sha256:x"
    assert metadata["created_at"]


def test_dry_run_plans_without_container(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        kc,
        "find_container_cli",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not run")),
    )
    result = kc.build_linux_corpus(
        root=tmp_path / "linux",
        corpus_root=tmp_path / "corpus",
        dry_run=True,
    )
    assert result["dry_run"] is True
    planned = result["planned"]
    for key in (
        "create_container",
        "apt_install",
        "fetch_tarball",
        "defconfig",
        "build",
        "gen_compile_commands",
    ):
        assert key in planned and planned[key]
    assert "ARCH=arm64" in planned["defconfig"]
    assert "LLVM=1" in planned["build"]
    assert "gen_compile_commands.py" in planned["gen_compile_commands"]
    assert "linux-6.12.113.tar.xz" in planned["fetch_tarball"]
