"""Offline indexing vs incremental update planning (watcher/daemon skeleton).

``ca index`` is the offline full pass; ``ca update`` re-parses only the
translation units affected by changed files and refreshes the database. This
module holds the pure planning helpers shared by both the CLI and a future
daemon; the file-system watcher itself is intentionally not implemented in this
phase (design: docs/incremental.md).

Header dependency awareness (task-38): when the index contains ``tu_includes``
records, a changed header affects only the TUs that directly include it. The
caller passes those TU ids into :func:`resolve_dependent_units`; without
dependency data the plan falls back to the old full-cache-rebuild behaviour
(``fallback_full=True``, warning emitted by the CLI).

A daemon is expected to:

1. watch the repository for source/header writes (inotify on Linux, FSEvents on
   macOS) and debounce bursts (~500 ms) into one batch;
2. call :func:`plan_changes` to map changed paths to compilation units;
3. run ``ca update --changed-files ...`` (or :func:`update_command`) and repeat;
4. fall back to a full ``ca index`` when a header/build file changed, because
   header dependency discovery is out of scope for the per-TU cache.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from ca.core.schema import CompilationUnit

_SOURCE_SUFFIXES = {".c", ".cc", ".cpp", ".cxx", ".c++"}
_HEADER_SUFFIXES = {".h", ".hh", ".hpp", ".hxx", ".inc"}


@dataclass
class WatchPlan:
    """Result of mapping changed files onto compilation units."""

    repo_root: str
    changed_files: tuple[str, ...]
    affected: tuple[str, ...]
    header_change: bool
    total_units: int
    errors: tuple[str, ...] = field(default_factory=tuple)
    header_files: tuple[str, ...] = field(default_factory=tuple)
    dependency_aware: bool = False
    fallback_full: bool = False

    @property
    def affected_count(self) -> int:
        return len(self.affected)

    def to_dict(self) -> dict[str, object]:
        return {
            "repo_root": self.repo_root,
            "changed_files": list(self.changed_files),
            "header_change": self.header_change,
            "total_units": self.total_units,
            "affected_count": self.affected_count,
            "affected": list(self.affected),
            "header_files": list(self.header_files),
            "dependency_aware": self.dependency_aware,
            "fallback_full": self.fallback_full,
            "errors": list(self.errors),
        }


def normalize_changed_files(
    paths: Iterable[str | os.PathLike[str]], repo_root: str | os.PathLike[str]
) -> tuple[list[Path], list[str]]:
    """Resolve changed paths against the repo root; return (paths, errors)."""

    root = Path(repo_root).resolve()
    normalized: list[Path] = []
    errors: list[str] = []
    for raw in paths:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate.absolute()
        if not resolved.is_file():
            errors.append(f"changed file not found: {resolved}")
            continue
        normalized.append(resolved)
    return normalized, errors


def plan_changes(
    compilation_units: Sequence[CompilationUnit],
    changed_files: Iterable[str | os.PathLike[str]],
    repo_root: str | os.PathLike[str],
) -> WatchPlan:
    """Map changed files to affected TUs.

    A changed source file affects the TU whose compile command lists it. Any
    changed path that is not a unit source (typically a header or a build file)
    is treated as a repository-wide change: ``header_change=True`` and every TU
    is affected, because header dependency discovery is not implemented.
    """

    root = Path(repo_root).resolve()
    units = list(compilation_units)
    normalized, errors = normalize_changed_files(changed_files, root)
    unit_by_file: dict[str, CompilationUnit] = {}
    for unit in units:
        try:
            unit_by_file[str(Path(unit.file).resolve())] = unit
        except OSError:
            unit_by_file[str(Path(unit.file))] = unit

    affected: list[str] = []
    changed_labels: list[str] = []
    header_change = False
    for path in normalized:
        try:
            relative = str(path.relative_to(root))
        except ValueError:
            relative = str(path)
        changed_labels.append(relative)
        matched = unit_by_file.get(str(path))
        if matched is not None:
            if matched.file not in affected:
                affected.append(matched.file)
        elif path.suffix.lower() in _HEADER_SUFFIXES or path.suffix == "":
            header_change = True
        else:
            # A changed source file that is not part of the compile database is
            # still a repository-wide change for safety.
            header_change = True
    header_files = tuple(
        label
        for label in changed_labels
        if Path(label).suffix.lower() in _HEADER_SUFFIXES
    )
    if header_change:
        affected = [unit.file for unit in units]
    return WatchPlan(
        repo_root=str(root),
        changed_files=tuple(changed_labels),
        affected=tuple(affected),
        header_change=header_change,
        total_units=len(units),
        errors=tuple(errors),
        header_files=header_files,
    )


def resolve_dependent_units(
    plan: WatchPlan,
    compilation_units: Sequence[CompilationUnit],
    dependent_tu_ids: Iterable[str],
    *,
    has_dependency_data: bool = False,
) -> WatchPlan:
    """Restrict a header-change plan to TUs that include the changed headers.

    ``dependent_tu_ids`` comes from the index (``tu_includes``).  The plan is
    only rewritten when every changed non-source path is a header-like file;
    build files or unknown paths still force the old full rebuild.  An empty
    dependency set (old database / parser without include facts) leaves the plan
    as the full fallback with ``fallback_full=True``.
    """

    if not plan.header_change:
        return plan
    labels = list(plan.changed_files)
    if not labels:
        return plan
    header_like = {
        label
        for label in labels
        if Path(label).suffix.lower() in _HEADER_SUFFIXES
    }
    if any(label not in header_like for label in labels):
        # A build file or a source file missing from the compile database: keep
        # the conservative full rebuild.
        plan.fallback_full = True
        return plan
    unit_by_id = {unit.id: unit for unit in compilation_units}
    affected = [
        unit_by_id[tu_id].file
        for tu_id in dependent_tu_ids
        if tu_id in unit_by_id
    ]
    if not affected:
        if not has_dependency_data:
            # The index predates include facts (or the parser did not emit
            # them): fall back conservatively to the old full rebuild.
            plan.fallback_full = True
            return plan
        # Dependency data exists and no TU includes the changed headers: this
        # update is a no-op (unrelated header), not a full rebuild.
        plan.affected = ()
        plan.dependency_aware = True
        plan.fallback_full = False
        return plan
    plan.affected = tuple(sorted(dict.fromkeys(affected)))
    plan.dependency_aware = True
    plan.fallback_full = False
    return plan


def update_command(plan: WatchPlan, *, repo: str, db: str, compdb: str | None = None) -> list[str]:
    """Build the ``ca update`` command line a daemon would execute."""

    command = ["ca", "update", "--repo", repo, "--db", db]
    if compdb:
        command.extend(["--compdb", compdb])
    command.append("--changed-files")
    command.extend(plan.changed_files)
    return command


def snapshot(repo_root: str | os.PathLike[str], suffixes: Iterable[str] | None = None) -> dict[str, tuple[int, float]]:
    """mtime/size snapshot for a polling watcher skeleton.

    Polling is a portable fallback for systems where native file events are not
    wired yet; the daemon design prefers inotify/FSEvents and uses this only for
    periodic reconciliation.
    """

    root = Path(repo_root)
    wanted = set(suffixes) if suffixes else (_SOURCE_SUFFIXES | _HEADER_SUFFIXES)
    state: dict[str, tuple[int, float]] = {}
    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in {".git", ".tmp", "build", "out", "node_modules"}]
        for filename in filenames:
            if Path(filename).suffix.lower() not in wanted:
                continue
            path = Path(directory) / filename
            try:
                stat = path.stat()
            except OSError:
                continue
            state[str(path)] = (stat.st_size, stat.st_mtime)
    return state


def diff_snapshots(
    old: dict[str, tuple[int, float]], new: dict[str, tuple[int, float]]
) -> list[str]:
    """Changed/created/deleted paths between two snapshots (sorted)."""

    changed = [path for path, state in new.items() if old.get(path) != state]
    deleted = [path for path in old if path not in new]
    return sorted(changed + deleted)
