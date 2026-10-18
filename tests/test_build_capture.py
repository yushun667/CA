"""Compiler-wrapper build capture tests (owner: build)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ca.build import BuildCaptureError, capture_build, load_compilation_database

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MAKE_PROJECT = FIXTURES / "build_make_proj"


def _write_script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _fake_compiler_dir(tmp_path: Path, *, exit_code: int = 0) -> tuple[Path, Path]:
    """A fake ``cc`` on PATH; returns (bin dir, executable path)."""

    bin_dir = tmp_path / "fakebin"
    compiler = _write_script(
        bin_dir / "cc",
        "#!/bin/sh\n"
        "sleep 0.05\n"
        f"exit {exit_code}\n",
    )
    return bin_dir, compiler


def _add_to_path(monkeypatch: pytest.MonkeyPatch, bin_dir: Path) -> None:
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))


def _project(tmp_path: Path, source: str = "int a(void) { return 1; }\n") -> Path:
    repo = tmp_path / "proj"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.c").write_text(source, encoding="utf-8")
    return repo


def test_capture_concurrent_invocations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir, compiler = _fake_compiler_dir(tmp_path)
    _add_to_path(monkeypatch, bin_dir)
    repo = _project(tmp_path)
    output = repo / "compile_commands.json"

    command = (
        "for i in 1 2 3 4 5 6 7 8; do "
        'cc -DFOO=1 -c src/a.c -o "a$i.o" & '
        "done; wait"
    )
    units = capture_build(command, output, repo, jobs=8)

    assert len(units) == 8
    assert output.is_file()
    for unit in units:
        assert unit.source == "capture"
        assert unit.language == "c"
        assert unit.file == str(repo / "src" / "a.c")
        assert unit.directory == str(repo.resolve())
        assert unit.repo_root == str(repo.resolve())
        assert unit.driver == str(compiler.resolve())
        assert "-DFOO=1" in unit.arguments
        assert any(argument.endswith(".o") for argument in unit.arguments)
        assert len(unit.id) > 3

    reloaded = load_compilation_database(output)
    assert {unit.id for unit in reloaded} == {unit.id for unit in units}

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert len(payload) == 8
    assert all(entry["arguments"][0] == str(compiler.resolve()) for entry in payload)


def test_capture_merges_duplicate_invocations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir, _ = _fake_compiler_dir(tmp_path)
    _add_to_path(monkeypatch, bin_dir)
    repo = _project(tmp_path)

    command = (
        "for i in 1 2 3 4 5 6; do "
        "cc -c src/a.c -o same.o & "
        "done; wait"
    )
    units = capture_build(command, repo / "compile_commands.json", repo)
    assert len(units) == 1


def test_capture_reports_failure_but_keeps_partial_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir, _ = _fake_compiler_dir(tmp_path, exit_code=3)
    _add_to_path(monkeypatch, bin_dir)
    repo = _project(tmp_path)
    output = repo / "compile_commands.json"

    with pytest.raises(BuildCaptureError) as excinfo:
        capture_build(["cc", "-c", "src/a.c", "-o", "a.o"], output, repo)

    error = excinfo.value
    assert error.returncode == 3
    assert error.captured == 1
    assert error.output == str(output.resolve())
    assert output.is_file()
    assert len(load_compilation_database(output)) == 1


def test_capture_honours_real_cc_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir, _ = _fake_compiler_dir(tmp_path)
    _add_to_path(monkeypatch, bin_dir)
    override = _write_script(tmp_path / "override-cc", "#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("CA_REAL_CC", str(override))
    repo = _project(tmp_path)

    units = capture_build(["cc", "-c", "src/a.c", "-o", "a.o"], repo / "cc.json", repo)
    assert len(units) == 1
    assert Path(units[0].driver).resolve() == override.resolve()


def test_wrapper_injects_and_records_extra_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ca.build import CaptureSession

    bin_dir, compiler = _fake_compiler_dir(tmp_path)
    _add_to_path(monkeypatch, bin_dir)
    repo = _project(tmp_path)

    with CaptureSession(
        repo, compilers={"cc": compiler}, extra_args={"cc": ("-mabi=lp64d",)}
    ) as session:
        completed = subprocess.run(
            ["cc", "-c", "src/a.c", "-o", "a.o"],
            cwd=repo,
            env=session.child_env(),
        )
        units = session.collect()

    assert completed.returncode == 0
    assert len(units) == 1
    assert units[0].driver == str(compiler.resolve())
    assert units[0].arguments[0] == "-mabi=lp64d"
    assert "src/a.c" in units[0].arguments


def test_capture_without_compiler_invocation_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir, _ = _fake_compiler_dir(tmp_path)
    _add_to_path(monkeypatch, bin_dir)
    repo = _project(tmp_path)
    with pytest.raises(ValueError) as excinfo:
        capture_build(["sh", "-c", "true"], repo / "compile_commands.json", repo)
    assert "captured no C/C++ translation units" in str(excinfo.value)


def test_capture_runs_real_make_and_is_idempotent(tmp_path: Path) -> None:
    project = tmp_path / "make_proj"
    shutil.copytree(MAKE_PROJECT, project)
    output = project / "compile_commands.json"

    first = capture_build(["make", "-j2"], output, project)
    assert len(first) == 2
    assert {Path(unit.file).name for unit in first} == {"hello.c", "util.c"}
    for unit in first:
        assert unit.source == "capture"
        assert unit.language == "c"
        assert "-Iinclude" in unit.arguments
        assert "-O2" in unit.arguments
        assert unit.arguments[-1].endswith(".c")
        assert Path(unit.file).is_file()

    first_payload = output.read_text(encoding="utf-8")
    second = capture_build(
        ["sh", "-c", "make clean >/dev/null 2>&1; make -j2"], output, project
    )
    assert [unit.id for unit in second] == [unit.id for unit in first]
    assert output.read_text(encoding="utf-8") == first_payload

    reloaded = load_compilation_database(output)
    assert {unit.id for unit in reloaded} == {unit.id for unit in first}
