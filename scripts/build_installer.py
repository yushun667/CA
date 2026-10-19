#!/usr/bin/env python3
"""Package a native PyInstaller onedir bundle; never cross-compile binaries."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
import zipfile


ROOT = Path(__file__).resolve().parents[1]


def target_name(system: str, machine: str) -> tuple[str, str]:
    systems = {"Windows": "windows", "Linux": "linux", "Darwin": "macos"}
    machines = {"amd64": "x64", "x86_64": "x64", "aarch64": "arm64", "arm64": "arm64"}
    if system not in systems or machine.lower() not in machines:
        raise ValueError(f"Unsupported native build target: {system}/{machine}")
    target = systems[system], machines[machine.lower()]
    if target == ("windows", "arm64"):
        raise ValueError("Windows arm64 is not part of the tested binary matrix")
    return target


def project_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as stream:
        version = tomllib.load(stream)["project"]["version"]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Installers require a numeric major.minor.patch project version")
    return version


def copy_bundle(bundle: Path, destination: Path) -> None:
    shutil.copytree(bundle, destination, symlinks=True)


def stage_linux(bundle: Path, stage: Path, version: str, arch: str) -> None:
    copy_bundle(bundle, stage / "opt" / "ca")
    commands = stage / "usr" / "bin"
    commands.mkdir(parents=True)
    (commands / "ca").symlink_to("../../opt/ca/ca")
    applications = stage / "usr" / "share" / "applications"
    applications.mkdir(parents=True)
    shutil.copy2(ROOT / "scripts" / "ca.desktop", applications / "ca.desktop")
    icons = stage / "usr" / "share" / "icons" / "hicolor" / "scalable" / "apps"
    icons.mkdir(parents=True)
    shutil.copy2(ROOT / "src" / "ca" / "ui" / "static" / "favicon.svg", icons / "ca.svg")
    control = stage / "DEBIAN"
    control.mkdir()
    installed_kib = (sum(p.stat().st_size for p in stage.rglob("*") if p.is_file()) + 1023) // 1024
    (control / "control").write_text(
        f"Package: ca-analyzer\nVersion: {version}\nSection: devel\nPriority: optional\n"
        f"Architecture: {'amd64' if arch == 'x64' else 'arm64'}\n"
        "Maintainer: CA contributors <noreply@github.com>\n"
        f"Installed-Size: {installed_kib}\n"
        "Depends: libc6 (>= 2.35), libstdc++6, zlib1g\n"
        "Recommends: xdg-utils\nHomepage: https://github.com/yushun667/CA\n"
        "Description: OS source analysis with CLI, MCP and a local web interface\n"
        " Includes its Python, libclang and DuckDB runtime.\n",
        encoding="utf-8",
    )


def stage_macos(bundle: Path, stage: Path, version: str) -> Path:
    app = stage / "Applications" / "CA.app"
    contents = app / "Contents"
    copy_bundle(bundle, contents / "Resources" / "ca")
    launchers = contents / "MacOS"
    launchers.mkdir(parents=True)
    launcher = launchers / "CA"
    shutil.copy2(ROOT / "scripts" / "ca-macos-launcher.sh", launcher)
    launcher.chmod(0o755)
    with (contents / "Info.plist").open("wb") as stream:
        plistlib.dump({
            "CFBundleIdentifier": "io.github.yushun667.ca",
            "CFBundleName": "CA",
            "CFBundleDisplayName": "CA",
            "CFBundleExecutable": "CA",
            "CFBundlePackageType": "APPL",
            "CFBundleShortVersionString": version,
            "CFBundleVersion": version,
            "NSHighResolutionCapable": True,
        }, stream)
    commands = stage / "usr" / "local" / "bin"
    commands.mkdir(parents=True)
    (commands / "ca").symlink_to("/Applications/CA.app/Contents/Resources/ca/ca")
    return app


def create_portable(bundle: Path, output: Path, stem: str, system: str, app: Path | None = None) -> Path:
    if system == "windows":
        artifact = output / f"{stem}-portable.zip"
        with zipfile.ZipFile(artifact, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for source in sorted(bundle.rglob("*")):
                if source.is_file():
                    archive.write(source, Path("ca") / source.relative_to(bundle))
        return artifact
    artifact = output / f"{stem}-portable.tar.gz"
    with tarfile.open(artifact, "w:gz", dereference=False) as archive:
        if system == "macos":
            if app is None:
                raise ValueError("macOS portable packaging requires an application bundle")
            archive.add(app, arcname="CA.app")
            command = tarfile.TarInfo("ca")
            command.type = tarfile.SYMTYPE
            command.linkname = "CA.app/Contents/Resources/ca/ca"
            archive.addfile(command)
        else:
            archive.add(bundle, arcname="ca")
    return artifact


def find_iscc(explicit: str | None) -> str:
    if explicit and not Path(explicit).is_file():
        raise RuntimeError(f"Inno Setup compiler does not exist: {explicit}")
    candidates = [explicit, shutil.which("ISCC.exe"), shutil.which("iscc")]
    for variable in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        base = os.environ.get(variable)
        if base:
            candidates.append(str(Path(base) / "Inno Setup 6" / "ISCC.exe"))
            candidates.append(str(Path(base) / "Programs" / "Inno Setup 6" / "ISCC.exe"))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    raise RuntimeError("Inno Setup 6 compiler not found; install it or supply --iscc PATH")


def macos_component_plist(stage: Path) -> Path:
    """Keep upgrades at the same /Applications path as the installed CLI link."""
    destination = stage.parent / "components.plist"
    subprocess.run(["pkgbuild", "--analyze", "--root", str(stage), str(destination)], check=True)
    with destination.open("rb") as stream:
        components = plistlib.load(stream)
    if not isinstance(components, list) or not any(
        isinstance(item, dict) and item.get("RootRelativeBundlePath") == "Applications/CA.app"
        for item in components
    ):
        raise RuntimeError("pkgbuild did not identify the expected Applications/CA.app bundle")

    def fix_locations(items: list[dict]) -> None:
        for component in items:
            component["BundleIsRelocatable"] = False
            component["BundleOverwriteAction"] = "upgrade"
            fix_locations(component.get("ChildBundles", []))

    fix_locations(components)
    with destination.open("wb") as stream:
        plistlib.dump(components, stream)
    return destination


def build_native_installer(bundle: Path, output: Path, stem: str, system: str,
                           arch: str, version: str, stage: Path, iscc: str | None) -> Path:
    if system == "windows":
        artifact = output / f"{stem}-setup.exe"
        subprocess.run([
            find_iscc(iscc), f"/DBundleDir={bundle}", f"/DAppVersion={version}",
            f"/DOutputDir={output}", f"/DOutputBase={stem}-setup",
            str(ROOT / "scripts" / "ca-installer.iss"),
        ], check=True)
    elif system == "linux":
        artifact = output / f"{stem}.deb"
        subprocess.run(["dpkg-deb", "--root-owner-group", "--build", str(stage), str(artifact)], check=True)
    else:
        artifact = output / f"{stem}.pkg"
        components = macos_component_plist(stage)
        subprocess.run([
            "pkgbuild", "--root", str(stage), "--identifier", "io.github.yushun667.ca",
            "--version", version, "--install-location", "/",
            "--component-plist", str(components), str(artifact),
        ], check=True)
    if not artifact.is_file():
        raise RuntimeError(f"Installer compiler did not produce {artifact}")
    return artifact


def write_checksums(artifacts: list[Path], output: Path) -> Path:
    destination = output / "SHA256SUMS"
    rows = []
    for artifact in sorted(artifacts):
        with artifact.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        rows.append(f"{digest}  {artifact.name}\n")
    destination.write_text("".join(rows), encoding="utf-8")
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, default=ROOT / "dist" / "ca")
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "installers")
    parser.add_argument("--iscc", help="Windows Inno Setup 6 compiler path")
    parser.add_argument("--portable-only", action="store_true", help="Skip the native installer compiler")
    args = parser.parse_args(argv)
    system, arch = target_name(platform.system(), platform.machine())
    bundle, output = args.bundle.resolve(), args.output.resolve()
    executable = bundle / ("ca.exe" if system == "windows" else "ca")
    if not executable.is_file() or not (bundle / "_internal").is_dir():
        parser.error(f"Expected a complete native onedir bundle at {bundle}; run scripts/build_binary.py first")
    if output == bundle or bundle in output.parents:
        parser.error("Installer output must be outside the input bundle")
    version = project_version()
    output.mkdir(parents=True, exist_ok=True)
    stem = f"ca-{version}-{system}-{arch}"
    with tempfile.TemporaryDirectory(prefix="ca-installer-") as temporary:
        stage = Path(temporary) / "stage"
        app = None
        if system == "linux":
            stage_linux(bundle, stage, version, arch)
        elif system == "macos":
            app = stage_macos(bundle, stage, version)
        artifacts = [create_portable(bundle, output, stem, system, app)]
        if not args.portable_only:
            artifacts.append(build_native_installer(bundle, output, stem, system, arch, version, stage, args.iscc))
    artifacts.append(write_checksums(artifacts, output))
    for artifact in artifacts:
        print(artifact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
