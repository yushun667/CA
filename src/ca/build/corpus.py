"""Benchmark/corpus acquisition for real compilation units.

Everything here writes only below ``.tmp/`` (git-ignored): the xv6 source
checkout goes to ``.tmp/corpus/xv6-riscv`` and its captured compile database is
``.tmp/corpus/xv6-riscv/compile_commands.json``. Nothing is committed.

xv6 is a small kernel whose ``Makefile`` builds with a RISC-V cross compiler.
The Makefile resolves ``CC`` from an absolute ``TOOLPREFIX``, which would
bypass a ``PATH`` shim, so :func:`build_xv6_compdb` passes
``CC=<wrapper>/cc`` on the make command line while keeping ``TOOLPREFIX`` real
for ``ld``/``objcopy``/``objdump``. Compilation is what we need; link failures
(if any) do not invalidate the captured database.
"""

from __future__ import annotations

import os
import subprocess
import sys
import warnings
from pathlib import Path
from typing import Sequence

from ca.build.capture import CaptureSession
from ca.build.compdb import load_compilation_database
from ca.core.schema import CompilationUnit

XV6_REPO_URL = "https://github.com/mit-pdos/xv6-riscv.git"
#: Pinned upstream commit (matches the checkout shipped under ``.tmp/``).
XV6_REVISION = "f780d7850765d74cbdcd1fff4b822d8829f7c9bd"

DEFAULT_XV6_DIR = Path(".tmp") / "corpus" / "xv6-riscv"

_GIT_TIMEOUT_SECONDS = 300
_MAKE_TIMEOUT_SECONDS = 1200


def _default_repo_root() -> Path:
    # src/ca/build/corpus.py -> build -> ca -> src -> repository root
    return Path(__file__).resolve().parents[3]


def xv6_path(repo_root: str | os.PathLike[str] | None = None) -> Path:
    """Default xv6 checkout location (``<repo>/.tmp/corpus/xv6-riscv``)."""

    root = Path(repo_root).resolve() if repo_root is not None else _default_repo_root()
    return root / DEFAULT_XV6_DIR


def _is_xv6_checkout(path: Path) -> bool:
    return (path / "Makefile").is_file() and (path / "kernel").is_dir()


def _run_git(arguments: Sequence[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:  # pragma: no cover - git is a hard prerequisite
        raise RuntimeError("git is required to fetch corpus sources") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(f"git {' '.join(arguments)} failed: {detail}")
    return completed


def _checkout_revision(checkout: Path, revision: str) -> None:
    try:
        _run_git(["cat-file", "-e", f"{revision}^{{commit}}"], cwd=checkout)
    except RuntimeError:
        _run_git(["fetch", "--quiet", "origin", revision], cwd=checkout)
    _run_git(["checkout", "--quiet", "--detach", revision], cwd=checkout)


def fetch_xv6(dest: str | os.PathLike[str] | None = None) -> Path:
    """Clone xv6-riscv at :data:`XV6_REVISION` into ``dest`` (idempotent).

    An existing checkout is reused as-is. When the destination is missing we
    prefer a local seed checkout (``<repo>/.tmp/xv6-riscv``) over the network
    so the fetch works offline, then verify the pinned revision.
    """

    checkout = Path(dest) if dest is not None else xv6_path()
    checkout = Path(os.path.abspath(checkout))
    if checkout.exists():
        if _is_xv6_checkout(checkout):
            return checkout
        if (checkout / ".git").exists():
            return checkout
        raise FileExistsError(
            f"{checkout} exists but does not look like an xv6-riscv checkout"
        )

    checkout.parent.mkdir(parents=True, exist_ok=True)
    seed = _default_repo_root() / ".tmp" / "xv6-riscv"
    source = str(seed) if _is_xv6_checkout(seed) else XV6_REPO_URL
    _run_git(["clone", "--quiet", source, str(checkout)])
    if (checkout / ".git").is_dir():
        _checkout_revision(checkout, XV6_REVISION)
    return checkout


def _find_toolchain(toolchain_dir: str | os.PathLike[str]) -> tuple[Path, str]:
    """Locate ``<bin dir, prefix>`` for a RISC-V GCC toolchain.

    Accepts either the toolchain root (with a ``bin/`` child) or a nested
    extraction directory; chooses a ``*-gcc`` whose sibling ``*-objdump``
    exists and prefers ``riscv`` flavours.
    """

    root = Path(toolchain_dir).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"toolchain directory not found: {root}")
    candidates: list[Path] = []
    for search_root in (root, *sorted(p for p in root.iterdir() if p.is_dir())):
        bin_dir = search_root / "bin"
        if not bin_dir.is_dir():
            continue
        for gcc in sorted(bin_dir.iterdir()):
            name = gcc.name
            if gcc.is_file() and name.endswith("-gcc"):
                prefix = name[: -len("gcc")]
                if (bin_dir / f"{prefix}objdump").is_file():
                    candidates.append(gcc)
    if not candidates:
        raise FileNotFoundError(
            f"no *-gcc/*-objdump cross compiler pair under {root} (yet?)"
        )
    candidates.sort(key=lambda item: (0 if "riscv" in item.name else 1, len(item.name)))
    chosen = candidates[0]
    return chosen.parent, chosen.name[: -len("gcc")]


def build_xv6_compdb(
    toolchain_dir: str | os.PathLike[str],
    dest: str | os.PathLike[str] | None = None,
    *,
    jobs: int = 8,
) -> list[CompilationUnit]:
    """Build xv6 with the capture wrappers, write its compile database.

    ``toolchain_dir`` is the RISC-V GCC toolchain root (containing ``bin/``).
    ``dest`` is either the xv6 checkout directory or the output
    ``compile_commands.json`` path; it defaults to
    :func:`xv6_path`/``compile_commands.json``.

    Toolchain detail: the xPack ``riscv-none-elf`` driver defaults to the RV32
    (``ilp32``) ABI, while xv6 is RV64. The GCC wrapper therefore injects
    ``-mabi=lp64d``; the flag is recorded in the captured argv and passed to
    the real compiler, so the database stays truthful about what ran
    (``source="capture"``). Wrappers are installed for the whole
    ``TOOLPREFIX`` tool set; ``ld`` also gets ``-m elf64lriscv`` because that
    driver defaults to ELF32 too, while ``objcopy``/``objdump`` detect the
    format from the object file.

    Targets are ``kernel/kernel`` plus every user program object (compile-only
    for user space). Returns the captured units, also written to disk. Raises
    :class:`RuntimeError` when the compiler cannot run or no C TU was captured;
    a nonzero make exit is reported as a warning because the compile database
    may already be complete.
    """

    if isinstance(jobs, bool) or not isinstance(jobs, int) or jobs < 1:
        raise ValueError(f"jobs must be a positive integer, got {jobs!r}")

    if dest is None:
        checkout = xv6_path()
        output: Path | None = None
    else:
        requested = Path(dest)
        if requested.suffix == ".json":
            checkout = xv6_path()
            output = requested
        else:
            checkout = requested
            output = None
    checkout = fetch_xv6(checkout)
    compdb_path = output if output is not None else checkout / "compile_commands.json"
    compdb_path = Path(os.path.abspath(compdb_path))

    bin_dir, prefix = _find_toolchain(toolchain_dir)
    cross_cc = bin_dir / f"{prefix}gcc"
    version = subprocess.run(
        [str(cross_cc), "--version"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if version.returncode != 0:
        raise RuntimeError(
            f"cross compiler {cross_cc} cannot run on this host: "
            f"{(version.stderr or version.stdout).strip()}"
        )

    # Wrap the whole TOOLPREFIX tool set; only the C driver needs the ABI fix.
    compilers: dict[str, str] = {}
    for tool in ("gcc", "ld", "objcopy", "objdump"):
        real_tool = bin_dir / f"{prefix}{tool}"
        if real_tool.is_file():
            compilers[f"{prefix}{tool}"] = str(real_tool)
    if f"{prefix}gcc" not in compilers:
        raise RuntimeError(f"toolchain has no usable {prefix}gcc: {bin_dir}")
    # The same driver default applies to binutils: riscv-none-elf-ld defaults
    # to elf32, so ask it for elf64lriscv explicitly.
    driver_args = {
        f"{prefix}gcc": ("-mabi=lp64d",),
        f"{prefix}ld": ("-m", "elf64lriscv"),
    }

    user_objects = [
        f"user/{source.stem}.o" for source in sorted((checkout / "user").glob("*.c"))
    ]
    with CaptureSession(
        checkout,
        compilers=compilers,
        extra_args=driver_args,
    ) as session:
        make_command = [
            "make",
            f"TOOLPREFIX={session.bin_dir}/{prefix}",
            f"-j{jobs}",
            "-k",
            "kernel/kernel",
            *user_objects,
        ]
        completed = subprocess.run(
            make_command,
            cwd=str(checkout),
            env=session.child_env(),
            timeout=_MAKE_TIMEOUT_SECONDS,
        )
        units = session.collect()
        if not units:
            raise RuntimeError(
                "xv6 build produced no C translation units "
                f"(make exit code {completed.returncode}); check the toolchain"
            )
        session.write_compdb(compdb_path, units)
        print(
            f"[xv6] make exit={completed.returncode}; "
            f"compiler invocations={session.recorded_invocations}, "
            f"C translation units={len(units)}, "
            f"non-TU invocations filtered={session.skipped_invocations}",
            file=sys.stderr,
        )
        if completed.returncode != 0:
            warnings.warn(
                f"xv6 make exited with code {completed.returncode}; "
                f"{len(units)} compile units were captured before the failure "
                "(link steps are not required for the corpus)",
                RuntimeWarning,
                stacklevel=2,
            )

    # Round-trip the written database so the corpus is known-loadable.
    reloaded = load_compilation_database(compdb_path)
    if len(reloaded) != len(units):
        warnings.warn(
            f"xv6 database round-trip changed unit count: "
            f"{len(units)} -> {len(reloaded)}",
            RuntimeWarning,
            stacklevel=2,
        )
    return units


#: Findings from the macOS feasibility study for a real Linux kernel compdb.
#: See the build module report for the full command/limit discussion.
LINUX_KERNEL_COMPDB_NOTES = """\
macOS host (arm64, macOS 26.5, no sudo, no Linux container runtime):
generating a real Linux kernel compile_commands.json natively is NOT practical.
Measured host state / blockers:
- /usr/bin/make is GNU Make 3.81; the kernel requires GNU make >= 3.82.
- No llvm-config, ld.lld, llvm-objcopy, pahole or libelf (LLVM=1 unbuildable);
  Homebrew is present but the environment forbids `brew install` (no sudo).
- Apple clang targets darwin only; kernel host tools (scripts/) assume ELF/Linux
  and `make prepare` needs the full source tree (~1.5 GB) which is not present;
  fetching it over the measured ~160 KB/s link would exceed the time box.
- No docker/podman/colima/lima, so no local OCI-container shortcut there.
Alternative that works on this macOS host: Apple `container` runs a real Linux
aarch64 VM. `ca.build.kernel_corpus` automates the reproducible workflow
(persistent bind-mounted container, pinned tarball, `make LLVM=1 ARCH=arm64`,
kernel `gen_compile_commands.py`, container->host path rewrite) and writes
`.tmp/corpus/linux/compile_commands.json` + `metadata.json`; kernel v6.12.113
on arm64 yielded >1600 C TUs this way. Full-tree kernel builds should still be
done on Linux CI.
Recommended CI commands (Ubuntu, clang/LLVM installed):
  sudo apt-get install -y build-essential flex bison bc libelf-dev libssl-dev \\
      clang llvm lld
  make LLVM=1 ARCH=x86_64 defconfig
  make LLVM=1 ARCH=x86_64 -j"$(nproc)" compile_commands.json
  # or, after a normal build:
  scripts/clang-tools/gen_compile_commands.py -d . -o compile_commands.json
`compile_commands.json` then feeds ca.build.load_compilation_database directly.
For a smaller local stand-in use xv6 (build_xv6_compdb, fixed revision, captured
from a real cross-compile) or any CMake project exported with
-DCMAKE_EXPORT_COMPILE_COMMANDS=ON.
"""

__all__ = [
    "DEFAULT_XV6_DIR",
    "LINUX_KERNEL_COMPDB_NOTES",
    "XV6_REPO_URL",
    "XV6_REVISION",
    "build_xv6_compdb",
    "fetch_xv6",
    "xv6_path",
]
