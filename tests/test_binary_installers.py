"""Distribution layouts preserve runtime assets and expose CLI/web entry points."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import plistlib
import tarfile
import zipfile

import pytest


_SPEC = importlib.util.spec_from_file_location(
    "ca_build_installer", Path(__file__).resolve().parents[1] / "scripts" / "build_installer.py"
)
assert _SPEC and _SPEC.loader
installer = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(installer)


def bundle_at(directory: Path, windows: bool = False) -> Path:
    bundle = directory / "runtime"
    assets = bundle / "_internal" / "ca" / "ui" / "static"
    assets.mkdir(parents=True)
    (assets / "index.html").write_text("<html>CA</html>", encoding="utf-8")
    (bundle / ("ca.exe" if windows else "ca")).write_bytes(b"native-runtime")
    (bundle / ("ca.exe" if windows else "ca")).chmod(0o755)
    return bundle


def require_symlinks(directory: Path) -> None:
    if os.name == "nt":
        pytest.skip("Package layouts require POSIX symlink resolution and executable bits; native POSIX CI covers them")
    target = directory / "link-target"
    target.write_text("test", encoding="utf-8")
    link = directory / "link-test"
    try:
        link.symlink_to(target.name)
    except OSError:
        pytest.skip("Host does not permit symlinks; native POSIX CI tests the package layouts")
    link.unlink()
    target.unlink()


@pytest.mark.parametrize("system,machine,expected", [
    ("Windows", "AMD64", ("windows", "x64")),
    ("Linux", "x86_64", ("linux", "x64")),
    ("Linux", "aarch64", ("linux", "arm64")),
    ("Darwin", "arm64", ("macos", "arm64")),
    ("Darwin", "x86_64", ("macos", "x64")),
])
def test_native_platform_names(system: str, machine: str, expected: tuple[str, str]) -> None:
    assert installer.target_name(system, machine) == expected


@pytest.mark.parametrize("system,machine", [("Windows", "ARM64"), ("Linux", "i686"), ("FreeBSD", "amd64")])
def test_unverified_targets_fail_explicitly(system: str, machine: str) -> None:
    with pytest.raises(ValueError, match="target|matrix"):
        installer.target_name(system, machine)


def test_windows_portable_contains_complete_bundle_and_checksums(tmp_path: Path) -> None:
    bundle = bundle_at(tmp_path, windows=True)
    archive_path = installer.create_portable(bundle, tmp_path, "ca-0.1.0-windows-x64", "windows")
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.read("ca/ca.exe") == b"native-runtime"
        assert archive.read("ca/_internal/ca/ui/static/index.html") == b"<html>CA</html>"
    checksums = installer.write_checksums([archive_path], tmp_path)
    import hashlib
    assert checksums.read_text(encoding="utf-8") == (
        f"{hashlib.sha256(archive_path.read_bytes()).hexdigest()}  {archive_path.name}\n"
    )


def test_linux_package_layout_exposes_cli_and_desktop_without_user_data(tmp_path: Path) -> None:
    require_symlinks(tmp_path)
    bundle = bundle_at(tmp_path)
    (bundle / "_internal" / "libalias.so").symlink_to("ca/ui/static/index.html")
    stage = tmp_path / "stage"
    installer.stage_linux(bundle, stage, "1.2.3", "x64")
    command = stage / "usr" / "bin" / "ca"
    assert command.is_symlink()
    assert command.read_bytes() == b"native-runtime"
    assert (stage / "opt" / "ca" / "_internal" / "libalias.so").is_symlink()
    desktop = (stage / "usr" / "share" / "applications" / "ca.desktop").read_text(encoding="utf-8")
    assert "Exec=/opt/ca/ca desktop" in desktop
    control = (stage / "DEBIAN" / "control").read_text(encoding="utf-8")
    assert "Version: 1.2.3\n" in control
    assert "Architecture: amd64\n" in control
    assert not (stage / "home").exists()
    archive = installer.create_portable(bundle, tmp_path, "linux", "linux")
    with tarfile.open(archive) as packed:
        assert packed.getmember("ca/_internal/libalias.so").issym()
        assert packed.getmember("ca/ca").mode & 0o111


def test_macos_app_and_portable_use_same_runtime_for_cli_and_web(tmp_path: Path) -> None:
    require_symlinks(tmp_path)
    bundle = bundle_at(tmp_path)
    stage = tmp_path / "stage"
    app = installer.stage_macos(bundle, stage, "1.2.3")
    with (app / "Contents" / "Info.plist").open("rb") as stream:
        metadata = plistlib.load(stream)
    assert metadata["CFBundleVersion"] == "1.2.3"
    assert metadata["CFBundleExecutable"] == "CA"
    launcher = app / "Contents" / "MacOS" / "CA"
    assert launcher.stat().st_mode & 0o111
    assert 'Resources/ca/ca" desktop "$@"' in launcher.read_text(encoding="utf-8")
    assert str((stage / "usr" / "local" / "bin" / "ca").readlink()) == (
        "/Applications/CA.app/Contents/Resources/ca/ca"
    )
    archive = installer.create_portable(bundle, tmp_path, "macos", "macos", app)
    with tarfile.open(archive) as packed:
        command = packed.getmember("ca")
        assert command.issym()
        assert command.linkname == "CA.app/Contents/Resources/ca/ca"
        assert packed.extractfile("CA.app/Contents/Resources/ca/ca").read() == b"native-runtime"
    assert not (stage / "Users").exists()


def test_missing_bundle_fails_before_creating_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(installer.platform, "system", lambda: "Windows")
    monkeypatch.setattr(installer.platform, "machine", lambda: "AMD64")
    output = tmp_path / "output"
    with pytest.raises(SystemExit, match="2"):
        installer.main(["--bundle", str(tmp_path / "missing"), "--output", str(output)])
    assert not output.exists()


def test_portable_only_does_not_need_native_compiler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundle = bundle_at(tmp_path, windows=True)
    output = tmp_path / "output"
    monkeypatch.setattr(installer.platform, "system", lambda: "Windows")
    monkeypatch.setattr(installer.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(installer, "find_iscc", lambda _: pytest.fail("No native compiler should run"))
    assert installer.main(["--bundle", str(bundle), "--output", str(output), "--portable-only"]) == 0
    assert len(list(output.glob("*-portable.zip"))) == 1
    assert (output / "SHA256SUMS").is_file()


def test_output_cannot_be_inside_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundle = bundle_at(tmp_path, windows=True)
    monkeypatch.setattr(installer.platform, "system", lambda: "Windows")
    monkeypatch.setattr(installer.platform, "machine", lambda: "AMD64")
    with pytest.raises(SystemExit, match="2"):
        installer.main(["--bundle", str(bundle), "--output", str(bundle / "recursive"), "--portable-only"])


def test_explicit_missing_compiler_is_reported(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="does not exist"):
        installer.find_iscc(str(tmp_path / "missing.exe"))


def test_macos_package_upgrade_keeps_app_at_cli_link_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    output = tmp_path / "output"
    output.mkdir()
    calls = []

    def pkgbuild(command: list[str], *, check: bool) -> None:
        assert check
        calls.append(command)
        if "--analyze" in command:
            with Path(command[-1]).open("wb") as stream:
                plistlib.dump([{
                    "RootRelativeBundlePath": "Applications/CA.app",
                    "BundleIsRelocatable": True,
                    "BundleOverwriteAction": "update",
                    "ChildBundles": [{
                        "RootRelativeBundlePath": "Applications/CA.app/Contents/Resources/ca/_internal/Python.framework",
                        "BundleIsRelocatable": True,
                    }],
                }], stream)
        else:
            components_path = Path(command[command.index("--component-plist") + 1])
            assert components_path.parent == stage.parent
            assert not components_path.is_relative_to(stage)
            with components_path.open("rb") as stream:
                components = plistlib.load(stream)
            app = components[0]
            assert app["RootRelativeBundlePath"] == "Applications/CA.app"
            assert app["BundleIsRelocatable"] is False
            assert app["BundleOverwriteAction"] == "upgrade"
            assert app["ChildBundles"][0]["BundleIsRelocatable"] is False
            Path(command[-1]).write_bytes(b"native-pkg")

    monkeypatch.setattr(installer.subprocess, "run", pkgbuild)
    artifact = installer.build_native_installer(stage, output, "macos", "macos", "arm64", "1.2.3", stage, None)
    assert artifact.read_bytes() == b"native-pkg"
    assert len(calls) == 2
    assert calls[1][calls[1].index("--install-location") + 1] == "/"


def test_macos_package_rejects_missing_app_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()

    def analyze(command: list[str], *, check: bool) -> None:
        with Path(command[-1]).open("wb") as stream:
            plistlib.dump([], stream)

    monkeypatch.setattr(installer.subprocess, "run", analyze)
    with pytest.raises(RuntimeError, match="expected Applications/CA.app"):
        installer.macos_component_plist(stage)
