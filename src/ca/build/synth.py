"""Explicit fallback compilation units for repositories without build info.

The produced units are *guesses*: they only carry the language's default
driver and whatever ``extra_args`` the caller supplied. They are therefore
always marked ``source="synthesized"``; consumers that care about real build
fidelity must check the source field and never treat these as captured flags.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Sequence

from ca.build.compdb import normalize_repo_root
from ca.core.ids import tu_id
from ca.core.schema import CompilationUnit

SOURCE_SYNTHESIZED = "synthesized"

_LANGUAGE_ALIASES = {
    "c": "c",
    "c++": "c++",
    "cpp": "c++",
    "cxx": "c++",
    "cc": "c++",
}

_DRIVER_BY_LANGUAGE = {"c": "cc", "c++": "c++"}


def _normalize_language(language: str) -> str:
    normalized = _LANGUAGE_ALIASES.get(str(language).strip().lower())
    if normalized is None:
        raise ValueError(
            f"unsupported language for synthesis: {language!r}; expected 'c' or 'c++'"
        )
    return normalized


def synthesize_for_files(
    files: str | os.PathLike[str] | Iterable[str | os.PathLike[str]],
    repo_root: str | os.PathLike[str],
    language: str = "c",
    extra_args: Sequence[str] = (),
) -> list[CompilationUnit]:
    """Build explicit ``source="synthesized"`` units for ``files``.

    ``files`` is one path or an iterable of paths, absolute or relative to
    ``repo_root``; ``extra_args`` are appended verbatim to every unit. No flags
    are invented beyond the language's default driver: the whole point is that
    callers can see this is a degradation.
    """

    root = normalize_repo_root(repo_root)
    if not os.path.isdir(root):
        raise FileNotFoundError(f"repo_root is not a directory: {root}")
    normalized_language = _normalize_language(language)
    arguments = tuple(str(argument) for argument in extra_args)
    driver = _DRIVER_BY_LANGUAGE[normalized_language]

    if isinstance(files, (str, os.PathLike)):
        requested = [files]
    else:
        requested = list(files)

    units: list[CompilationUnit] = []
    seen: set[str] = set()
    for requested_file in requested:
        source = Path(os.fspath(requested_file))
        if not source.is_absolute():
            source = Path(root) / source
        source = Path(os.path.normpath(os.path.abspath(source)))
        unit = CompilationUnit(
            id=tu_id(str(source), root, arguments),
            file=str(source),
            directory=root,
            repo_root=root,
            arguments=arguments,
            language=normalized_language,
            source=SOURCE_SYNTHESIZED,
            driver=driver,
        )
        if unit.id in seen:
            continue
        seen.add(unit.id)
        units.append(unit)
    return units


__all__ = ["SOURCE_SYNTHESIZED", "synthesize_for_files"]
