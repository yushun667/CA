"""Load and normalize ``compile_commands.json`` into :class:`CompilationUnit`.

A compile command database is the preferred source of *real* build arguments:
it is produced by the build system itself (CMake, Meson, Bear, ``make``
``compile_commands.json`` targets, ...). This module never invents flags; it
only validates, normalizes paths and drops entries that are not C/C++ TUs.

Normalization rules (see also ``ca.core.schema.CompilationUnit``):

- ``file`` and ``directory`` become absolute paths.
- ``arguments`` keeps the exact post-driver argv from the database entry.
  The compiler executable (``argv[0]``) is stored in ``driver`` and removed
  from ``arguments``; ccache/distcc style prefixes are unwrapped one level.
- ``language`` is derived from the file extension.
- ``source`` is ``"compile_commands"``.
- ``@response-file`` arguments are expanded relative to the entry directory
  (CMake/Ninja put include paths there); missing files are kept verbatim.
- ``id`` comes from :func:`ca.core.ids.tu_id` over (file, directory, argv).

Entries that cannot be interpreted (missing file/command, bad JSON types,
non C/C++ source, empty command) are filtered out and counted; if nothing
survives, :class:`ValueError` explains why instead of silently returning an
empty list.
"""

from __future__ import annotations

import json
import os
import shlex
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping

from ca.core.ids import tu_id
from ca.core.schema import CompilationUnit

COMPILE_COMMANDS_NAME = "compile_commands.json"
SOURCE_COMPILE_COMMANDS = "compile_commands"

#: Source extensions we can hand to libclang as a translation unit.
_LANGUAGE_BY_SUFFIX = {
    ".c": "c",
    ".cc": "c++",
    ".cp": "c++",
    ".cpp": "c++",
    ".cxx": "c++",
    ".c++": "c++",
    ".C": "c++",
}

_COMPILER_WRAPPERS = frozenset({"ccache", "sccache", "distcc", "icecc"})

#: Subdirectories searched (in addition to the given directory itself) before
#: falling back to a pruned recursive search.
_COMMON_BUILD_DIRS = (
    "build",
    "build-debug",
    "build-release",
    "cmake-build-debug",
    "cmake-build-release",
    "out",
    "out/Debug",
    "out/Release",
)

_SEARCH_PRUNE = frozenset(
    {".git", ".hg", ".svn", ".venv", "venv", "__pycache__", "node_modules", ".tmp"}
)


class _BadEntry(Exception):
    """Internal: one database entry cannot become a compilation unit."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# --------------------------------------------------------------------------- #
# Path / language helpers
# --------------------------------------------------------------------------- #


def language_for_path(path: str | os.PathLike[str]) -> str | None:
    """Return ``"c"``/``"c++"`` for a source path, else ``None``."""

    return _LANGUAGE_BY_SUFFIX.get(Path(path).suffix)


def is_c_family(path: str | os.PathLike[str]) -> bool:
    return language_for_path(path) is not None


def normalize_repo_root(repo_root: str | os.PathLike[str]) -> str:
    return os.path.abspath(os.fspath(repo_root))


def infer_repo_root(start: str | os.PathLike[str]) -> str:
    """Find the closest ancestor (inclusive) that contains ``.git``.

    Falls back to the start directory itself when no repository marker exists,
    so fixtures outside a Git checkout still load with a stable root.
    """

    path = Path(start).resolve()
    if path.is_file():
        path = path.parent
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            return str(candidate)
    return str(path)


# --------------------------------------------------------------------------- #
# Locating and reading the database
# --------------------------------------------------------------------------- #


def _resolve_compdb_path(path: str | os.PathLike[str]) -> Path:
    requested = Path(path)
    if requested.is_file():
        return requested.resolve()
    if not requested.is_dir():
        raise FileNotFoundError(f"compile_commands.json not found: {requested}")

    direct = requested / COMPILE_COMMANDS_NAME
    if direct.is_file():
        return direct.resolve()

    for relative in _COMMON_BUILD_DIRS:
        candidate = requested / relative / COMPILE_COMMANDS_NAME
        if candidate.is_file():
            return candidate.resolve()

    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(requested):
        dirnames[:] = sorted(d for d in dirnames if d not in _SEARCH_PRUNE)
        if COMPILE_COMMANDS_NAME in filenames:
            found.append(Path(dirpath) / COMPILE_COMMANDS_NAME)
    if not found:
        raise FileNotFoundError(
            f"compile_commands.json not found under {requested}; "
            "generate one (cmake -DCMAKE_EXPORT_COMPILE_COMMANDS=ON, "
            "meson compile, bear -- make, or ca.build.capture_build)"
        )
    if len(found) > 1:
        listing = ", ".join(str(item) for item in found[:5])
        raise ValueError(
            f"multiple compile_commands.json candidates under {requested}: {listing}"
        )
    return found[0].resolve()


def _read_entries(compdb_path: Path) -> list[Any]:
    try:
        data = json.loads(compdb_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise OSError(f"cannot read compilation database {compdb_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in compilation database {compdb_path}: {exc}") from exc
    if not isinstance(data, list):
        raise ValueError(
            f"compilation database {compdb_path} must be a JSON array of entries, "
            f"got {type(data).__name__}"
        )
    return data


# --------------------------------------------------------------------------- #
# Entry -> CompilationUnit
# --------------------------------------------------------------------------- #


def _as_argv(entry: Mapping[str, Any]) -> list[str]:
    raw_arguments = entry.get("arguments")
    if raw_arguments is not None:
        if not isinstance(raw_arguments, (list, tuple)) or not raw_arguments:
            raise _BadEntry("empty-arguments")
        argv: list[str] = []
        for token in raw_arguments:
            if not isinstance(token, str):
                raise _BadEntry("non-string-argument")
            argv.append(token)
        return argv

    command = entry.get("command")
    if command is None:
        raise _BadEntry("missing-command")
    if not isinstance(command, str):
        raise _BadEntry("non-string-command")
    if not command.strip():
        raise _BadEntry("empty-command")
    try:
        argv = shlex.split(command)
    except ValueError:
        raise _BadEntry("unparsable-command") from None
    if not argv:
        raise _BadEntry("empty-command")
    return argv


def _unwrap_driver(argv: list[str]) -> list[str]:
    """Drop one ccache/distcc style prefix, keeping the real compiler first."""

    while len(argv) > 1 and Path(argv[0]).name in _COMPILER_WRAPPERS:
        argv = argv[1:]
    return argv


def _entry_directory(entry: Mapping[str, Any], base_dir: Path) -> Path:
    raw_directory = entry.get("directory")
    if raw_directory is not None and not isinstance(raw_directory, str):
        raise _BadEntry("non-string-directory")
    if raw_directory:
        directory = Path(raw_directory)
        if not directory.is_absolute():
            directory = base_dir / directory
    else:
        raw_file = entry.get("file")
        if isinstance(raw_file, str) and os.path.isabs(raw_file):
            directory = Path(raw_file).parent
        else:
            directory = base_dir
    return Path(os.path.abspath(directory))


_MAX_RESPONSE_FILE_DEPTH = 8


def _expand_response_files(
    argv: Iterable[str], directory: Path, depth: int = 0
) -> list[str]:
    """Expand ``@file`` argument tokens relative to the compile directory.

    CMake/Ninja response files are common for include flags; libclang cannot
    interpret them, so they must become ordinary argv entries. Missing or
    unreadable files are left untouched; nested response files are bounded.
    """

    expanded: list[str] = []
    for token in argv:
        if (
            not token.startswith("@")
            or len(token) == 1
            or depth >= _MAX_RESPONSE_FILE_DEPTH
        ):
            expanded.append(token)
            continue
        path = Path(token[1:])
        if not path.is_absolute():
            path = directory / path
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            expanded.append(token)
            continue
        try:
            nested = shlex.split(content)
        except ValueError:
            expanded.append(token)
            continue
        expanded.extend(_expand_response_files(nested, directory, depth + 1))
    return expanded


def _entry_to_unit(
    entry: Any,
    *,
    base_dir: Path,
    repo_root: str,
) -> CompilationUnit:
    if not isinstance(entry, Mapping):
        raise _BadEntry("entry-not-an-object")
    raw_file = entry.get("file")
    if not isinstance(raw_file, str) or not raw_file.strip():
        raise _BadEntry("missing-file")

    directory = _entry_directory(entry, base_dir)
    source = Path(raw_file)
    if not source.is_absolute():
        source = directory / source
    source = Path(os.path.normpath(os.path.abspath(source)))

    language = language_for_path(source)
    if language is None:
        suffix = source.suffix or "<none>"
        raise _BadEntry(f"unsupported-language:{suffix}")

    argv = _unwrap_driver(_as_argv(entry))
    if not argv:
        raise _BadEntry("empty-command")
    driver = argv[0]
    arguments = tuple(_expand_response_files(argv[1:], directory))
    directory_str = str(directory)
    return CompilationUnit(
        id=tu_id(str(source), directory_str, arguments),
        file=str(source),
        directory=directory_str,
        repo_root=repo_root,
        arguments=arguments,
        language=language,
        source=SOURCE_COMPILE_COMMANDS,
        driver=driver,
    )


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def load_compilation_database(
    path: str | os.PathLike[str],
    repo_root: str | os.PathLike[str] | None = None,
) -> list[CompilationUnit]:
    """Load ``compile_commands.json`` from a file or a directory containing it.

    ``repo_root`` defaults to the closest ancestor of the database (or of the
    given directory) that contains ``.git``; fixtures without Git metadata fall
    back to the database directory.

    Raises :class:`FileNotFoundError` when the database cannot be located,
    :class:`ValueError` when it is malformed or contains no usable C/C++ entry.
    """

    compdb_path = _resolve_compdb_path(path)
    if repo_root is not None:
        root = normalize_repo_root(repo_root)
    else:
        root = infer_repo_root(compdb_path)
    entries = _read_entries(compdb_path)

    units: list[CompilationUnit] = []
    seen: set[str] = set()
    filtered: dict[str, int] = {}
    for entry in entries:
        try:
            unit = _entry_to_unit(entry, base_dir=compdb_path.parent, repo_root=root)
        except _BadEntry as exc:
            filtered[exc.reason] = filtered.get(exc.reason, 0) + 1
            continue
        if unit.id in seen:
            continue
        seen.add(unit.id)
        units.append(unit)

    if not units:
        reasons = ", ".join(f"{reason}x{count}" for reason, count in sorted(filtered.items()))
        detail = f"filtered: {reasons}" if reasons else "database is empty"
        raise ValueError(
            f"no parseable C/C++ translation units in {compdb_path} ({detail})"
        )
    return units


def units_to_entries(units: Iterable[CompilationUnit]) -> list[dict[str, Any]]:
    """Serialize units back to standard cdbb entries (compiler as ``argv[0]``)."""

    entries: list[dict[str, Any]] = []
    for unit in units:
        argv = list(unit.arguments)
        if unit.driver:
            argv.insert(0, unit.driver)
        entries.append(
            {
                "directory": unit.directory,
                "file": unit.file,
                "arguments": argv,
            }
        )
    return entries


def write_compilation_database(
    units: Iterable[CompilationUnit],
    path: str | os.PathLike[str],
) -> Path:
    """Atomically write units as a standard ``compile_commands.json`` file."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    entries = units_to_entries(units)
    payload = json.dumps(entries, indent=2, ensure_ascii=False) + "\n"
    fd, temporary = tempfile.mkstemp(
        prefix=".compile_commands-", suffix=".json", dir=str(destination.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(temporary, destination)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return destination.resolve()


__all__ = [
    "COMPILE_COMMANDS_NAME",
    "SOURCE_COMPILE_COMMANDS",
    "infer_repo_root",
    "is_c_family",
    "language_for_path",
    "load_compilation_database",
    "normalize_repo_root",
    "units_to_entries",
    "write_compilation_database",
]
