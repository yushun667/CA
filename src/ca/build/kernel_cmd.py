"""Reconstruct real compile commands from a Linux kernel build tree.

Kbuild writes one ``.cmd`` file per target (``kernel/sched/.core.o.cmd`` in
modern kernels, ``kernel/sched/core.o.cmd`` in older ones). Each contains a
line::

    cmd_kernel/sched/core.o := gcc -Wp,-MMD,kernel/sched/.core.o.d -nostdinc \
        -isystem ... -D__KERNEL__ -include ... -c -o kernel/sched/core.o \
        kernel/sched/core.c

The line is a faithful record of what Kbuild actually ran, so it is a real
compile command. This module turns those lines back into argv: it removes
``-o``/dependency-file bookkeeping and ``VAR=value`` environment noise, keeps
include/define/``-include`` flags and the source file, and only returns entries
whose source is a C/C++ file (assembly ``.S`` entries and shell snippets are
counted as skipped).
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path
from typing import Iterator

from ca.build.compdb import (
    infer_repo_root,
    language_for_path,
    normalize_repo_root,
)
from ca.core.ids import tu_id
from ca.core.schema import CompilationUnit

SOURCE_KERNEL_CMD = "kernel_cmd"

_CMD_LINE = re.compile(r"^\s*cmd_(?P<target>\S+)\s*(?::=|=)\s*(?P<command>.*)$")
_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

#: Standalone flags that only exist to drive dependency-file writing.
_DROP_FLAGS = frozenset({"-M", "-MM", "-MD", "-MMD", "-MP", "-MG"})
#: Flags whose following token is dependency bookkeeping.
_DROP_FLAG_VALUES = frozenset({"-MF", "-MT", "-MQ"})
#: Kbuild dependency side effect: ``-Wp,-MMD,<dir>/.foo.o.d``.
_DEP_PREFIX = "-Wp,-MMD,"


def iter_cmd_files(build_dir: str | os.PathLike[str]) -> Iterator[Path]:
    """Yield every Kbuild ``.cmd`` file under ``build_dir`` (deterministic)."""

    for dirpath, dirnames, filenames in os.walk(build_dir):
        dirnames.sort()
        for filename in sorted(filenames):
            if filename.endswith(".cmd"):
                yield Path(dirpath) / filename


def _clean_arguments(argv: list[str]) -> list[str]:
    cleaned: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "-o":
            index += 2
            continue
        if token in _DROP_FLAG_VALUES:
            index += 2
            continue
        if token in _DROP_FLAGS or token.startswith(_DEP_PREFIX):
            index += 1
            continue
        if _ENV_ASSIGNMENT.match(token):
            index += 1
            continue
        cleaned.append(token)
        index += 1
    return cleaned


def _find_source(arguments: list[str], build_dir: Path, repo_root: Path) -> Path | None:
    for token in reversed(arguments):
        if not token or token.startswith("-"):
            continue
        if language_for_path(token) is None:
            continue
        candidate = Path(token)
        if candidate.is_absolute():
            return Path(os.path.normpath(candidate))
        for base in (build_dir, repo_root):
            resolved = base / candidate
            if resolved.is_file():
                return Path(os.path.normpath(os.path.abspath(resolved)))
        return Path(os.path.normpath(os.path.abspath(build_dir / candidate)))
    return None


def _command_text(cmd_path: Path) -> list[str]:
    try:
        text = cmd_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    # Kbuild may continue very long lines with a trailing backslash.
    text = text.replace("\\\n", " ")
    return text.splitlines()


def reconstruct_kernel_commands(
    build_dir: str | os.PathLike[str],
    repo_root: str | os.PathLike[str],
) -> list[CompilationUnit]:
    """Rebuild :class:`CompilationUnit` list from kernel ``.cmd`` files.

    ``build_dir`` is the kernel build tree that contains the ``.cmd`` files
    (in-tree or an ``O=`` output directory); ``repo_root`` is the kernel source
    root used to resolve relative source/include paths.

    Raises :class:`FileNotFoundError` when ``build_dir`` does not exist and
    :class:`ValueError` when no C/C++ command can be reconstructed.
    """

    build_path = Path(normalize_repo_root(build_dir))
    if not build_path.is_dir():
        raise FileNotFoundError(f"kernel build directory not found: {build_path}")
    root = normalize_repo_root(repo_root)

    units: list[CompilationUnit] = []
    seen: set[str] = set()
    cmd_files = 0
    commands = 0
    skipped = 0
    for cmd_path in iter_cmd_files(build_path):
        cmd_files += 1
        try:
            relative = cmd_path.relative_to(build_path)
        except ValueError:  # pragma: no cover - walk guarantees this
            relative = Path(cmd_path.name)
        for line in _command_text(cmd_path):
            match = _CMD_LINE.match(line)
            if not match:
                continue
            commands += 1
            try:
                argv = shlex.split(match.group("command"))
            except ValueError:
                skipped += 1
                continue
            if not argv:
                skipped += 1
                continue
            arguments = _clean_arguments(argv)
            if not arguments:
                skipped += 1
                continue
            source = _find_source(arguments, build_path, Path(root))
            if source is None or language_for_path(source) is None:
                skipped += 1
                continue
            driver = arguments[0]
            compile_arguments = tuple(arguments[1:])
            directory = str(build_path)
            unit = CompilationUnit(
                id=tu_id(str(source), directory, compile_arguments),
                file=str(source),
                directory=directory,
                repo_root=root,
                arguments=compile_arguments,
                language=language_for_path(source) or "c",
                source=SOURCE_KERNEL_CMD,
                driver=driver,
            )
            if unit.id in seen:
                continue
            seen.add(unit.id)
            units.append(unit)

    if not units:
        raise ValueError(
            f"no C/C++ compile commands reconstructed from {build_path} "
            f"(scanned {cmd_files} .cmd file(s), {commands} cmd_ line(s), "
            f"skipped {skipped})"
        )
    units.sort(key=lambda unit: (unit.file, unit.arguments))
    return units


def default_repo_root(build_dir: str | os.PathLike[str]) -> str:
    """Convenience: infer the repository root above a kernel build dir."""

    return infer_repo_root(build_dir)


__all__ = [
    "SOURCE_KERNEL_CMD",
    "default_repo_root",
    "iter_cmd_files",
    "reconstruct_kernel_commands",
]
