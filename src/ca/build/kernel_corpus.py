"""Reproducible Linux kernel compilation-database corpus via OCI containers.

macOS cannot build a Linux kernel natively (GNU Make 3.81, no ELF toolchain,
no libelf/pahole, no sudo). Apple's ``container`` CLI gives us a real Linux
(aarch64) environment without a Linux host, so this module documents and
automates the reproducible path:

1. create/start a persistent Ubuntu container with the corpus work dir bind
   mounted (``container create --name ca-kernel ... --mount ... sleep infinity``;
   this module never calls ``container system start``);
2. ``apt-get install`` the kernel build prerequisites (clang/lld/llvm, make,
   flex, bison, bc, libelf-dev, libssl-dev, dwarves, python3, git);
3. fetch a pinned kernel tarball into ``<root>/linux-<version>`` and verify its
   sha256 against kernel.org's ``sha256sums.asc``;
4. ``make LLVM=1 ARCH=arm64 defconfig`` + a subset build;
5. generate the database with the kernel's own
   ``scripts/clang-tools/gen_compile_commands.py`` from the real ``.cmd``
   files, then copy it plus a metadata record under
   ``.tmp/corpus/linux/`` (never committed).

Only the container-engine calls need the CLI; command construction, metadata,
checksum parsing and database summarising are pure functions and unit tested
without a container. When the ``container`` CLI is missing a
:class:`ContainerUnavailableError` explains what is required instead of
silently degrading. This is a *real* compilation database corpus: do not
replace a failed build with synthesized flags.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from ca.build.compdb import load_compilation_database, normalize_repo_root, write_compilation_database

KERNEL_TARBALL_URL = "https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-{version}.tar.xz"
KERNEL_SHA256SUMS_URL = "https://cdn.kernel.org/pub/linux/kernel/v6.x/sha256sums.asc"

UBUNTU_IMAGE = "docker.io/library/ubuntu:24.04"

#: Everything the kernel build (LLVM=1 + Kconfig + gen_compile_commands) needs.
DEFAULT_APT_PACKAGES = (
    "build-essential",
    "clang",
    "lld",
    "llvm",
    "make",
    "flex",
    "bison",
    "bc",
    "libssl-dev",
    "libelf-dev",
    "dwarves",
    "python3",
    "git",
    "ca-certificates",
    "xz-utils",
    "file",
)

#: Subset that is still representative (kernel core, allocator, VFS, net).
DEFAULT_BUILD_TARGETS = ("kernel/", "mm/", "fs/", "drivers/net/")

_DEFAULT_CONTAINER_CLI_CANDIDATES = ("/usr/local/bin/container",)


class ContainerUnavailableError(RuntimeError):
    """The OCI container CLI needed to build the kernel corpus is missing."""


class KernelCorpusError(RuntimeError):
    """Kernel source download/build/database generation failed."""


@dataclass(frozen=True)
class KernelCorpusSpec:
    """Pinned parameters for one Linux kernel corpus build."""

    version: str = "6.12.113"
    #: Immutable commit for the default version (``refs/tags/v6.12.113^{}``).
    commit: str = "7fdb7c1824a1f9979d648d7ae28ac446bcee9c6e"
    arch: str = "arm64"
    config: str = "defconfig"
    image: str = UBUNTU_IMAGE
    container_name: str = "ca-kernel"
    cpus: int = 8
    memory: str = "8g"
    mount_target: str = "/work"
    jobs: int = 8
    build_targets: tuple[str, ...] = DEFAULT_BUILD_TARGETS

    @property
    def tag(self) -> str:
        return f"v{self.version}"

    @property
    def tarball_name(self) -> str:
        return f"linux-{self.version}.tar.xz"

    @property
    def tarball_url(self) -> str:
        return KERNEL_TARBALL_URL.format(version=self.version)

    @property
    def source_dirname(self) -> str:
        return f"linux-{self.version}"

    def source_dir(self, root: str | os.PathLike[str]) -> Path:
        return Path(root) / self.source_dirname

    def compdb_path(self, corpus_root: str | os.PathLike[str]) -> Path:
        return Path(corpus_root) / "compile_commands.json"

    def metadata_path(self, corpus_root: str | os.PathLike[str]) -> Path:
        return Path(corpus_root) / "metadata.json"

    def container_workdir(self) -> str:
        """Absolute directory of the kernel source tree inside the container."""

        return f"{self.mount_target}/{self.source_dirname}"

    def __post_init__(self) -> None:
        if not self.version or not all(part.isdigit() for part in self.version.split(".")):
            raise ValueError(f"kernel version must look like 6.12.113, got {self.version!r}")
        if self.cpus < 1 or self.jobs < 1:
            raise ValueError("cpus and jobs must be positive")
        if not self.mount_target.startswith("/"):
            raise ValueError(f"mount_target must be absolute, got {self.mount_target!r}")
        if not self.image:
            raise ValueError("container image must not be empty")


# --------------------------------------------------------------------------- #
# Pure command construction (unit tested without a container)
# --------------------------------------------------------------------------- #


def find_container_cli(explicit: str | None = None) -> str:
    """Locate Apple's ``container`` CLI or raise a actionable error."""

    if explicit:
        resolved = shutil.which(explicit) or (explicit if Path(explicit).is_file() else None)
        if not resolved:
            raise ContainerUnavailableError(f"container CLI not found: {explicit}")
        return os.path.abspath(resolved)
    found = shutil.which("container")
    if found:
        return os.path.abspath(found)
    for candidate in _DEFAULT_CONTAINER_CLI_CANDIDATES:
        if Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise ContainerUnavailableError(
        "Apple `container` CLI not found. The Linux kernel corpus is built in a "
        "Linux container; install Apple container (macOS 26+) and make sure its "
        "apiserver is already running (`container ls` works). This module "
        "deliberately does not run `container system start` itself. "
        "Refusing to fall back to synthesized flags."
    )


def container_create_command(
    spec: KernelCorpusSpec,
    source_root: str | os.PathLike[str],
    *,
    cli: str | None = None,
) -> list[str]:
    """``container create`` argv for the persistent build container."""

    executable = cli or "container"
    return [
        executable,
        "create",
        "--name",
        spec.container_name,
        "-c",
        str(spec.cpus),
        "-m",
        spec.memory,
        "--mount",
        f"type=bind,source={os.path.abspath(source_root)},target={spec.mount_target}",
        spec.image,
        "sleep",
        "infinity",
    ]


def container_exec_command(
    spec: KernelCorpusSpec,
    argv: Sequence[str],
    *,
    cli: str | None = None,
    workdir: str | None = None,
) -> list[str]:
    executable = cli or "container"
    command = [executable, "exec"]
    if workdir:
        command += ["-w", workdir]
    command += [spec.container_name, *argv]
    return command


def apt_install_command(spec: KernelCorpusSpec) -> list[str]:
    """Install the kernel build prerequisites inside the Ubuntu container."""

    packages = " ".join(DEFAULT_APT_PACKAGES)
    script = (
        "export DEBIAN_FRONTEND=noninteractive; "
        "apt-get update -qq && "
        f"apt-get install -y -qq --no-install-recommends {packages}"
    )
    return ["bash", "-lc", script]


def make_defconfig_command(spec: KernelCorpusSpec) -> list[str]:
    return ["make", f"ARCH={spec.arch}", "LLVM=1", spec.config]


def make_build_command(
    spec: KernelCorpusSpec,
    targets: Sequence[str] | None = None,
) -> list[str]:
    selected = tuple(targets) if targets is not None else spec.build_targets
    return ["make", f"-j{spec.jobs}", f"ARCH={spec.arch}", "LLVM=1", *selected]


def gen_compile_commands_command(spec: KernelCorpusSpec) -> list[str]:
    return [
        "python3",
        "scripts/clang-tools/gen_compile_commands.py",
        "-d",
        ".",
        "-o",
        "compile_commands.json",
    ]


def format_command(argv: Sequence[str]) -> str:
    return " ".join(shlex.quote(str(token)) for token in argv)


# --------------------------------------------------------------------------- #
# Container/state helpers
# --------------------------------------------------------------------------- #


def parse_container_list(output: str) -> dict[str, str]:
    """Map container name -> state from ``container ls --format json`` output."""

    try:
        data = json.loads(output)
    except json.JSONDecodeError as exc:
        raise KernelCorpusError(f"cannot parse `container ls` output: {exc}") from exc
    if isinstance(data, dict):
        data = data.get("containers", [])
    if not isinstance(data, list):
        raise KernelCorpusError("unexpected `container ls --format json` payload")
    states: dict[str, str] = {}
    for item in data:
        if not isinstance(item, Mapping):
            continue
        name = item.get("name") or item.get("Name")
        if not name:
            name = item.get("id") or item.get("ID")
        if not name and isinstance(item.get("configuration"), Mapping):
            name = item["configuration"].get("id")
        state = item.get("state") or item.get("State")
        if not state and isinstance(item.get("status"), Mapping):
            state = item["status"].get("state")
        if name:
            states[str(name)] = str(state or "").lower()
    return states


def _run(
    argv: Sequence[str],
    *,
    log_path: Path | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    if log_path is None:
        return subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log:
        log.write(format_command(argv) + "\n")
        log.flush()
        completed = subprocess.run(
            list(argv),
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
    return completed


def container_image_digest(spec: KernelCorpusSpec, *, cli: str | None = None) -> str:
    """Return the local image digest/id for the container image."""

    executable = cli or find_container_cli()
    completed = _run([executable, "image", "inspect", spec.image])
    if completed.returncode != 0:
        raise KernelCorpusError(
            f"cannot inspect container image {spec.image}: {completed.stderr.strip()}"
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise KernelCorpusError(f"cannot parse image inspect output: {exc}") from exc
    items = payload if isinstance(payload, list) else [payload]
    if not items or not isinstance(items[0], Mapping):
        raise KernelCorpusError("empty container image inspect output")
    item = items[0]
    digest = item.get("id") or item.get("digest") or ""
    if not digest and isinstance(item.get("configuration"), Mapping):
        descriptor = item["configuration"].get("descriptor", {})
        if isinstance(descriptor, Mapping):
            digest = descriptor.get("digest", "")
    if not digest:
        raise KernelCorpusError("container image inspect did not report a digest")
    return str(digest)


def container_kernel_version(spec: KernelCorpusSpec, *, cli: str | None = None) -> str:
    executable = cli or find_container_cli()
    completed = _run([executable, "exec", spec.container_name, "uname", "-r"])
    if completed.returncode != 0:
        raise KernelCorpusError(f"container is not running: {completed.stderr.strip()}")
    return completed.stdout.strip()


def ensure_container(
    spec: KernelCorpusSpec,
    source_root: str | os.PathLike[str],
    *,
    cli: str | None = None,
) -> dict[str, object]:
    """Create the persistent container if needed and make sure it is running."""

    executable = cli or find_container_cli()
    listing = _run([executable, "ls", "--all", "--format", "json"])
    if listing.returncode != 0:
        raise KernelCorpusError(
            "cannot list containers; is the Apple container apiserver running? "
            f"`container ls` stderr: {listing.stderr.strip()}"
        )
    states = parse_container_list(listing.stdout)
    created = spec.container_name not in states
    if created:
        create = _run(container_create_command(spec, source_root, cli=executable))
        if create.returncode != 0:
            raise KernelCorpusError(
                f"container create failed: {create.stderr.strip() or create.stdout.strip()}"
            )
    if states.get(spec.container_name) != "running":
        started = _run([executable, "start", spec.container_name])
        if started.returncode != 0:
            raise KernelCorpusError(f"container start failed: {started.stderr.strip()}")
    return {
        "created": created,
        "name": spec.container_name,
        "kernel": container_kernel_version(spec, cli=executable),
    }


def container_exec(
    spec: KernelCorpusSpec,
    argv: Sequence[str],
    *,
    cli: str | None = None,
    workdir: str | None = None,
    log_path: Path | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    executable = cli or find_container_cli()
    command = container_exec_command(spec, argv, cli=executable, workdir=workdir)
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("w", encoding="utf-8") as log:
            log.write(format_command(command) + "\n")
            log.flush()
            return subprocess.run(
                command, stdout=log, stderr=subprocess.STDOUT, text=True, timeout=timeout
            )
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout)


# --------------------------------------------------------------------------- #
# Kernel source acquisition
# --------------------------------------------------------------------------- #


def is_kernel_checkout(path: str | os.PathLike[str]) -> bool:
    makefile = Path(path) / "Makefile"
    return makefile.is_file() and "VERSION =" in makefile.read_text(
        encoding="utf-8", errors="replace"
    )


def expected_sha256(sha256sums_text: str, tarball_name: str) -> str | None:
    """Extract the expected digest for ``tarball_name`` from kernel.org output."""

    for line in sha256sums_text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].lstrip("*") == tarball_name:
            digest = parts[0].strip().lower()
            if len(digest) == 64:
                return digest
    return None


def file_sha256(path: str | os.PathLike[str], *, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _fetch_url(url: str, destination: Path, *, workers: int = 8) -> None:
    """Range-parallel download; falls back to a single stream if unsupported."""

    head = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(head, timeout=60) as response:
        total = response.headers.get("Content-Length")
        if response.headers.get("Accept-Ranges", "").lower() != "bytes" or not total:
            seed = urllib.request.Request(url)
            with urllib.request.urlopen(seed, timeout=120) as seed_response:
                destination.write_bytes(seed_response.read())
            return
        size = int(total)
    if workers <= 1 or size < 2 * workers * 1024:
        request = urllib.request.Request(url)
        with urllib.request.urlopen(request, timeout=120) as response:
            destination.write_bytes(response.read())
        return
    chunk = (size + workers - 1) // workers
    parts = destination.parent / f".{destination.name}.parts"
    parts.mkdir(parents=True, exist_ok=True)

    def download(index: int) -> Path:
        start = index * chunk
        end = min(start + chunk - 1, size - 1)
        part = parts / f"part.{index:02d}"
        request = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end}"})
        with urllib.request.urlopen(request, timeout=180) as response:
            part.write_bytes(response.read())
        if part.stat().st_size != end - start + 1:
            raise KernelCorpusError(f"short range download for part {index}")
        return part

    with ThreadPoolExecutor(max_workers=workers) as pool:
        downloaded = list(pool.map(download, range(workers)))
    with destination.open("wb") as output:
        for part in downloaded:
            output.write(part.read_bytes())
            part.unlink()
    shutil.rmtree(parts, ignore_errors=True)


def fetch_kernel_source(
    spec: KernelCorpusSpec,
    root: str | os.PathLike[str],
    *,
    downloads: int = 8,
    timeout: float | None = 1800,
) -> Path:
    """Ensure ``<root>/linux-<version>`` exists, downloading/verifying if needed."""

    root_path = Path(root)
    source = spec.source_dir(root_path)
    if is_kernel_checkout(source):
        return source
    root_path.mkdir(parents=True, exist_ok=True)
    tarball = root_path / spec.tarball_name
    if not tarball.is_file():
        _fetch_url(spec.tarball_url, tarball, workers=downloads)
    sha256sums = root_path / "sha256sums.asc"
    try:
        with urllib.request.urlopen(KERNEL_SHA256SUMS_URL, timeout=60) as response:
            sha256sums.write_bytes(response.read())
        expected = expected_sha256(
            sha256sums.read_text(encoding="utf-8", errors="replace"), spec.tarball_name
        )
    except OSError:
        expected = None
    actual = file_sha256(tarball)
    if expected and actual != expected:
        raise KernelCorpusError(
            f"kernel tarball checksum mismatch for {tarball}: {actual} != {expected}"
        )
    source.mkdir(parents=True, exist_ok=False)
    try:
        with tarfile.open(tarball, "r:xz") as archive:
            archive.extractall(source, filter="data")  # type: ignore[arg-type]
    except BaseException:
        shutil.rmtree(source, ignore_errors=True)
        raise
    # Tarballs contain a single top-level linux-<version>/ directory.
    children = [child for child in source.iterdir() if child.is_dir()]
    if len(children) == 1 and is_kernel_checkout(children[0]):
        staged = children[0]
        for item in staged.iterdir():
            item.rename(source / item.name)
        staged.rmdir()
    if not is_kernel_checkout(source):
        raise KernelCorpusError(f"extracted tarball is not a kernel tree: {source}")
    return source


# --------------------------------------------------------------------------- #
# Build + compdb generation
# --------------------------------------------------------------------------- #


def prepare_container(
    spec: KernelCorpusSpec,
    *,
    cli: str | None = None,
    log_path: Path | None = None,
    timeout: float | None = 1800,
) -> dict[str, object]:
    """apt-get install the build prerequisites inside the container."""

    executable = cli or find_container_cli()
    completed = container_exec(
        spec,
        apt_install_command(spec),
        cli=executable,
        log_path=log_path,
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise KernelCorpusError(
            f"container apt-get install failed (exit {completed.returncode}); "
            f"see {log_path or 'stdout'}"
        )
    return {"returncode": 0, "log": str(log_path) if log_path else ""}


def build_kernel(
    spec: KernelCorpusSpec,
    source: str | os.PathLike[str],
    *,
    targets: Sequence[str] | None = None,
    log_dir: str | os.PathLike[str] | None = None,
    cli: str | None = None,
) -> dict[str, object]:
    """Run defconfig + subset build inside the container, returning commands/timings."""

    executable = cli or find_container_cli()
    source_path = Path(source)
    if not is_kernel_checkout(source_path):
        raise KernelCorpusError(f"not a kernel source tree: {source_path}")
    logs = Path(log_dir) if log_dir is not None else source_path / "ca-build-logs"
    defconfig_started = time.monotonic()
    defconfig = container_exec(
        spec,
        make_defconfig_command(spec),
        cli=executable,
        workdir=spec.container_workdir(),
        log_path=logs / "defconfig.log",
    )
    if defconfig.returncode != 0:
        raise KernelCorpusError(
            f"kernel defconfig failed (exit {defconfig.returncode}); "
            f"see {logs / 'defconfig.log'}"
        )
    defconfig_seconds = time.monotonic() - defconfig_started

    build_argv = make_build_command(spec, targets)
    build_started = time.monotonic()
    build = container_exec(
        spec,
        build_argv,
        cli=executable,
        workdir=spec.container_workdir(),
        log_path=logs / "build.log",
    )
    build_seconds = time.monotonic() - build_started
    if build.returncode != 0:
        raise KernelCorpusError(
            f"kernel build failed (exit {build.returncode}); "
            f"see {logs / 'build.log'}"
        )
    return {
        "defconfig_command": format_command(
            container_exec_command(
                spec, make_defconfig_command(spec), cli="container", workdir=spec.container_workdir()
            )
        ),
        "build_command": format_command(
            container_exec_command(
                spec, build_argv, cli="container", workdir=spec.container_workdir()
            )
        ),
        "defconfig_seconds": round(defconfig_seconds, 2),
        "build_seconds": round(build_seconds, 2),
        "targets": list(targets) if targets is not None else list(spec.build_targets),
        "logs": {"defconfig": str(logs / "defconfig.log"), "build": str(logs / "build.log")},
    }


def generate_compile_database(
    spec: KernelCorpusSpec,
    source: str | os.PathLike[str],
    *,
    cli: str | None = None,
    log_dir: str | os.PathLike[str] | None = None,
) -> Path:
    """Run the kernel's own ``gen_compile_commands.py`` and return the database path."""

    executable = cli or find_container_cli()
    source_path = Path(source)
    logs = Path(log_dir) if log_dir is not None else source_path / "ca-build-logs"
    completed = container_exec(
        spec,
        gen_compile_commands_command(spec),
        cli=executable,
        workdir=spec.container_workdir(),
        log_path=logs / "gen_compile_commands.log",
    )
    if completed.returncode != 0:
        raise KernelCorpusError(
            f"gen_compile_commands.py failed (exit {completed.returncode}); "
            f"see {logs / 'gen_compile_commands.log'}"
        )
    database = source_path / "compile_commands.json"
    if not database.is_file():
        raise KernelCorpusError(
            "gen_compile_commands.py reported success but wrote no compile_commands.json"
        )
    return database


def normalize_database_paths(
    database: str | os.PathLike[str],
    *,
    container_root: str,
    host_root: str | os.PathLike[str],
) -> Path:
    """Rewrite container-absolute paths in a kernel compdb to host paths.

    ``gen_compile_commands.py`` runs inside the container and resolves
    ``directory``/``file`` to ``/work/linux-x.y.z/...``. The corpus is consumed
    on the host, so rewrite that prefix to the host source tree. Relative
    operands inside ``command`` (``-I./include``, the source path) are already
    portable and are left untouched.
    """

    path = Path(database)
    container_prefix = container_root.rstrip("/")
    prefix = container_prefix + "/"
    host_prefix = str(Path(host_root).resolve()).rstrip("/")
    replacement = host_prefix + "/"
    entries = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(entries, list):
        raise KernelCorpusError(f"kernel compile database is not a JSON array: {path}")
    rewritten = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for key in ("directory", "file", "command", "output"):
            value = entry.get(key)
            if not isinstance(value, str):
                continue
            if value == container_prefix:
                entry[key] = host_prefix
                rewritten += 1
            elif prefix in value:
                entry[key] = value.replace(prefix, replacement)
                rewritten += 1
    fd, temporary = tempfile.mkstemp(prefix=".compile_commands-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(entries, handle, indent=2, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    if rewritten == 0:
        raise KernelCorpusError(
            f"no container paths ({prefix}) found in {path}; was the database "
            "generated inside the mounted build tree?"
        )
    return path


def summarize_compilation_database(
    compdb_path: str | os.PathLike[str],
    repo_root: str | os.PathLike[str],
) -> dict[str, object]:
    """Load the generated database with the CA loader and summarise it."""

    raw_entries = json.loads(Path(compdb_path).read_text(encoding="utf-8"))
    if not isinstance(raw_entries, list):
        raise KernelCorpusError(f"kernel compile database is not a JSON array: {compdb_path}")
    units = load_compilation_database(compdb_path, repo_root=repo_root)
    languages: dict[str, int] = {}
    sources: dict[str, int] = {}
    for unit in units:
        languages[unit.language] = languages.get(unit.language, 0) + 1
        sources[unit.source] = sources.get(unit.source, 0) + 1
    return {
        "tu_count": len(units),
        "raw_entries": len(raw_entries),
        "filtered_entries": len(raw_entries) - len(units),
        "languages": dict(sorted(languages.items())),
        "sources": dict(sorted(sources.items())),
    }


# --------------------------------------------------------------------------- #
# Corpus installation + metadata
# --------------------------------------------------------------------------- #


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def install_corpus(
    spec: KernelCorpusSpec,
    database: str | os.PathLike[str],
    corpus_root: str | os.PathLike[str],
    *,
    metadata: Mapping[str, object],
) -> dict[str, str]:
    """Copy the compdb into the corpus dir and write ``metadata.json``."""

    corpus = Path(corpus_root)
    corpus.mkdir(parents=True, exist_ok=True)
    destination = spec.compdb_path(corpus)
    shutil.copy2(database, destination)
    payload = dict(metadata)
    payload.setdefault("kernel_version", spec.version)
    payload.setdefault("kernel_tag", spec.tag)
    payload.setdefault("arch", spec.arch)
    payload.setdefault("config", spec.config)
    payload.setdefault("image", spec.image)
    payload.setdefault("created_at", _utc_now())
    metadata_path = spec.metadata_path(corpus)
    fd, temporary = tempfile.mkstemp(
        prefix=".metadata-", suffix=".json", dir=str(metadata_path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(temporary, metadata_path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return {"compdb": str(destination.resolve()), "metadata": str(metadata_path.resolve())}


def build_linux_corpus(
    *,
    root: str | os.PathLike[str],
    corpus_root: str | os.PathLike[str] | None = None,
    spec: KernelCorpusSpec | None = None,
    targets: Sequence[str] | None = None,
    skip_prepare: bool = False,
    skip_build: bool = False,
    dry_run: bool = False,
    cli: str | None = None,
) -> dict[str, object]:
    """End-to-end reproducible pipeline used to materialise the kernel corpus.

    ``root`` is the host directory bind-mounted into the container (kernel
    tarball/source/build tree live there); ``corpus_root`` receives the final
    ``compile_commands.json`` + ``metadata.json`` (default: ``root``).
    ``dry_run`` only returns the planned commands (no container/network), which
    keeps orchestration testable and lets CI print the exact recipe.
    """

    kernel_spec = spec or KernelCorpusSpec()
    root_path = Path(root).resolve()
    corpus = Path(corpus_root) if corpus_root is not None else root_path
    source = kernel_spec.source_dir(root_path)
    logs = root_path / "ca-build-logs"
    workdir = kernel_spec.container_workdir()
    planned = {
        "create_container": format_command(
            container_create_command(kernel_spec, root_path, cli="container")
        ),
        "apt_install": format_command(
            container_exec_command(
                kernel_spec, apt_install_command(kernel_spec), cli="container", workdir=workdir
            )
        ),
        "fetch_tarball": (
            f"curl -L -o {root_path / kernel_spec.tarball_name} {kernel_spec.tarball_url}"
        ),
        "defconfig": format_command(
            container_exec_command(
                kernel_spec, make_defconfig_command(kernel_spec), cli="container", workdir=workdir
            )
        ),
        "build": format_command(
            container_exec_command(
                kernel_spec,
                make_build_command(kernel_spec, targets),
                cli="container",
                workdir=workdir,
            )
        ),
        "gen_compile_commands": format_command(
            container_exec_command(
                kernel_spec,
                gen_compile_commands_command(kernel_spec),
                cli="container",
                workdir=workdir,
            )
        ),
        "corpus_root": str(corpus.resolve()),
    }
    if dry_run:
        return {"dry_run": True, "spec": kernel_spec.__dict__, "planned": planned}

    executable = cli or find_container_cli()
    container_info = ensure_container(kernel_spec, root_path, cli=executable)
    if not skip_prepare:
        prepare_container(kernel_spec, cli=executable, log_path=logs / "apt-install.log")
    source_path = fetch_kernel_source(kernel_spec, root_path)
    build_info: dict[str, object] = {"skipped": True}
    if skip_build:
        if not (source_path / "compile_commands.json").is_file():
            raise KernelCorpusError(
                "skip_build=True but the source tree has no compile_commands.json"
            )
    else:
        build_info = build_kernel(
            kernel_spec,
            source_path,
            targets=targets,
            log_dir=logs,
            cli=executable,
        )
        build_info["skipped"] = False
    database = generate_compile_database(
        kernel_spec, source_path, cli=executable, log_dir=logs
    )
    database = normalize_database_paths(
        database,
        container_root=kernel_spec.container_workdir(),
        host_root=source_path,
    )
    summary = summarize_compilation_database(database, source_path)
    digest = container_image_digest(kernel_spec, cli=executable)
    tarball = root_path / kernel_spec.tarball_name
    metadata: dict[str, object] = {
        "kernel_version": kernel_spec.version,
        "kernel_tag": kernel_spec.tag,
        "kernel_commit": kernel_spec.commit,
        "source_url": kernel_spec.tarball_url,
        "tarball_sha256": file_sha256(tarball) if tarball.is_file() else "",
        "container_image": kernel_spec.image,
        "container_image_digest": digest,
        "container_name": kernel_spec.container_name,
        "container_kernel": container_info.get("kernel", ""),
        "arch": kernel_spec.arch,
        "config": kernel_spec.config,
        "build_targets": (
            list(targets) if targets is not None else list(kernel_spec.build_targets)
        ),
        "compdb": summary,
        "build": build_info,
        "commands": {
            "create_container": planned["create_container"],
            "apt_install": planned["apt_install"],
            "fetch_tarball": planned["fetch_tarball"],
            "defconfig": planned["defconfig"],
            "build": planned["build"],
            "gen_compile_commands": planned["gen_compile_commands"],
        },
        "generated_at": _utc_now(),
    }
    installed = install_corpus(kernel_spec, database, corpus, metadata=metadata)
    return {
        "dry_run": False,
        "spec": kernel_spec.__dict__,
        "source_dir": str(source),
        "installed": installed,
        "metadata": metadata,
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Tiny CLI: ``python -m ca.build.kernel_corpus --root .tmp/corpus/linux``."""

    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=".tmp/corpus/linux", help="working/corpus root")
    parser.add_argument("--version", default=KernelCorpusSpec().version)
    parser.add_argument("--arch", default="arm64")
    parser.add_argument("--jobs", type=int, default=8)
    parser.add_argument("--cpus", type=int, default=8)
    parser.add_argument("--container-name", default="ca-kernel")
    parser.add_argument(
        "--container-root",
        default=".tmp/linux",
        help="host directory bind-mounted into the container (contains source/build tree)",
    )
    parser.add_argument("--target", action="append", default=None)
    parser.add_argument("--skip-prepare", action="store_true")
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    spec = KernelCorpusSpec(
        version=args.version,
        arch=args.arch,
        jobs=args.jobs,
        cpus=args.cpus,
        container_name=args.container_name,
    )
    result = build_linux_corpus(
        root=args.container_root,
        corpus_root=args.root,
        spec=spec,
        targets=tuple(args.target) if args.target else None,
        skip_prepare=args.skip_prepare,
        skip_build=args.skip_build,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
