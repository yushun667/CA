"""``ca watch``: polling incremental index daemon.

One cycle: take a size+mtime snapshot of the repository, diff it against the
persisted state (``<db>.watch.json``), debounce bursts into one change set, map
the change set onto translation units (``ca.cli.watcher.plan_changes``) and run
the existing incremental update path in-process. The database is rebuilt from
the parse cache, so unchanged TUs are cache hits.

Safety properties:

* single instance per database (``<db>.watch.lock`` + ``LOCK_NB``; a second
  watcher exits with code 2);
* the snapshot state only advances after a successful update, so failures are
  retried on the next cycle;
* new/deleted files that are not reflected in the compile database make the
  cycle report ``compdb_stale`` and skip the update (the compile database must
  be regenerated; pollers cannot synthesize one);
* SIGINT/SIGTERM stop the loop after the current cycle (exit 0) and release the
  lock; per-cycle exceptions are recorded and never kill the daemon.
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from ca.cli.watcher import diff_snapshots, snapshot

STATE_VERSION = 1
_SOURCE_SUFFIXES = {".c", ".cc", ".cpp", ".cxx", ".c++"}
_HEADER_SUFFIXES = {".h", ".hh", ".hpp", ".hxx", ".inc"}


@dataclass
class WatchConfig:
    repo: str
    db: str
    compdb: str | None = None
    interval: float = 5.0
    debounce: float = 2.0
    jobs: int = 8
    layer: str = "deep"
    analyze_scope: str = "main"
    record_scope: str = "auto"
    cache_dir: str | None = None
    once: bool = False
    json_output: bool = False
    changed_files: list[str] = field(default_factory=list)
    state_file: str | None = None

    @property
    def state_path(self) -> Path:
        return Path(self.state_file or (str(self.db) + ".watch.json"))

    @property
    def lock_path(self) -> Path:
        return Path(str(self.db) + ".watch.lock")


@dataclass
class WatchCycleResult:
    cycle: int = 0
    baseline: bool = False
    changed: list[str] = field(default_factory=list)
    header_change: bool = False
    affected_count: int = 0
    updated: bool = False
    skipped: bool = False
    skip_reason: str = ""
    compdb_stale: bool = False
    cache_hits: int = 0
    cache_misses: int = 0
    db_size: int | None = None
    deep_invalidated: bool = False
    elapsed_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cycle": self.cycle,
            "baseline": self.baseline,
            "changed": list(self.changed),
            "changed_count": len(self.changed),
            "header_change": self.header_change,
            "affected_count": self.affected_count,
            "updated": self.updated,
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
            "compdb_stale": self.compdb_stale,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "db_size": self.db_size,
            "deep_invalidated": self.deep_invalidated,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "errors": list(self.errors),
        }


class WatchLockedError(RuntimeError):
    """Another watcher already holds this database's lock."""

    def __init__(self, path: Path) -> None:
        super().__init__(f"watch already running for {path}")
        self.path = path


class _WatchLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._file: Any = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self.path, "a+")
        try:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._file.close()
            self._file = None
            raise WatchLockedError(self.path) from None
        self._file.seek(0)
        self._file.truncate()
        self._file.write(f"{os.getpid()}\n")
        self._file.flush()

    def release(self) -> None:
        if self._file is None:
            return
        try:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None


# --------------------------------------------------------------------------- #
# State persistence / snapshots
# --------------------------------------------------------------------------- #


def _snapshot_to_json(snap: dict[str, tuple[int, float]]) -> dict[str, list[float]]:
    return {path: [int(state[0]), float(state[1])] for path, state in snap.items()}


def _snapshot_from_json(payload: Any) -> dict[str, tuple[int, float]]:
    if not isinstance(payload, dict):
        return {}
    result: dict[str, tuple[int, float]] = {}
    for path, state in payload.items():
        try:
            result[str(path)] = (int(state[0]), float(state[1]))
        except (TypeError, ValueError, IndexError):
            continue
    return result


def load_state(config: WatchConfig) -> dict[str, tuple[int, float]] | None:
    path = config.state_path
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != STATE_VERSION:
        return None
    if str(payload.get("repo") or "") != str(Path(config.repo).expanduser().resolve()):
        return None
    return _snapshot_from_json(payload.get("snapshot"))


def save_state(config: WatchConfig, snap: dict[str, tuple[int, float]]) -> None:
    path = config.state_path
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": STATE_VERSION,
        "repo": str(Path(config.repo).expanduser().resolve()),
        "compdb": str(Path(config.compdb).expanduser().resolve()) if config.compdb else "",
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "snapshot": _snapshot_to_json(snap),
    }
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    os.replace(temporary, path)


def _relative(config: WatchConfig, path: str) -> str:
    try:
        return str(Path(path).resolve().relative_to(Path(config.repo).expanduser().resolve()))
    except (OSError, ValueError):
        return path


def _debounced_snapshot(
    config: WatchConfig,
    initial: dict[str, tuple[int, float]],
    debounce: float,
) -> dict[str, tuple[int, float]]:
    """Wait until writes are quiet for ``debounce`` seconds; return the snapshot."""

    if debounce <= 0:
        return initial
    latest = initial
    quiet_since = time.monotonic()
    deadline = time.monotonic() + max(1.0, debounce * 6)
    while time.monotonic() < deadline:
        time.sleep(max(0.02, min(0.2, debounce)))
        current = snapshot(config.repo)
        if current == latest:
            if time.monotonic() - quiet_since >= debounce:
                return current
        else:
            latest = current
            quiet_since = time.monotonic()
    return latest


# --------------------------------------------------------------------------- #
# One cycle
# --------------------------------------------------------------------------- #


def _compdb_mismatch_messages(
    config: WatchConfig, changed_paths: Sequence[str]
) -> list[str]:
    """Find added/deleted files the compile database cannot know about."""

    messages: list[str] = []
    for raw in changed_paths:
        path = Path(raw)
        suffix = path.suffix.lower()
        if suffix not in (_SOURCE_SUFFIXES | _HEADER_SUFFIXES):
            continue
        if not path.is_file():
            messages.append(
                f"deleted file is not in the compile database (regenerate it):"
                f" {_relative(config, raw)}"
            )
    return messages


def _bad_source_messages(
    config: WatchConfig, changed_paths: Sequence[str], units: Sequence[Any]
) -> list[str]:
    """Added source files that are absent from the compile database."""

    unit_sources = set()
    for unit in units:
        try:
            unit_sources.add(str(Path(unit.file).resolve()))
        except OSError:
            unit_sources.add(str(unit.file))
    messages: list[str] = []
    for raw in changed_paths:
        path = Path(raw)
        if not path.is_file() or path.suffix.lower() not in _SOURCE_SUFFIXES:
            continue
        if str(path.resolve()) not in unit_sources:
            messages.append(
                f"new source file is not in the compile database (regenerate it):"
                f" {_relative(config, raw)}"
            )
    return messages


def _emit_result(config: WatchConfig, result: WatchCycleResult) -> None:
    if config.json_output:
        print(json.dumps(result.to_dict(), ensure_ascii=False), flush=True)
        return
    state = "updated" if result.updated else ("skipped" if result.skipped else "no-change")
    print(
        f"[watch] cycle={result.cycle} changed={len(result.changed)}"
        f" header={result.header_change} affected={result.affected_count}"
        f" {state} cache={result.cache_hits}/{result.cache_hits + result.cache_misses}"
        f" elapsed={result.elapsed_seconds:.2f}s",
        file=sys.stderr,
        flush=True,
    )
    for message in result.errors:
        print(f"[watch] error: {message}", file=sys.stderr, flush=True)


def run_cycle(
    config: WatchConfig,
    cycle: int,
    previous: dict[str, tuple[int, float]] | None,
    *,
    update_runner: Callable[..., Any] | None = None,
) -> tuple[WatchCycleResult, dict[str, tuple[int, float]] | None]:
    """Run one poll cycle; returns (result, snapshot_to_persist)."""

    from ca.cli.commands import run_update

    runner = update_runner or run_update
    started = time.perf_counter()
    result = WatchCycleResult(cycle=cycle)
    current = snapshot(config.repo)
    if previous is None:
        result.baseline = True
        result.elapsed_seconds = time.perf_counter() - started
        return result, current

    first_changed = diff_snapshots(previous, current)
    if not first_changed:
        result.elapsed_seconds = time.perf_counter() - started
        return result, previous

    latest = _debounced_snapshot(config, current, config.debounce)
    changed_paths = diff_snapshots(previous, latest)
    result.changed = [_relative(config, path) for path in changed_paths]
    errors = _compdb_mismatch_messages(config, changed_paths)
    if errors:
        result.errors.extend(errors)
        result.skipped = True
        result.compdb_stale = True
        result.skip_reason = "compdb_stale"
        result.elapsed_seconds = time.perf_counter() - started
        return result, latest

    from ca.build import load_compilation_database
    from ca.pipeline import find_compilation_database

    compdb = find_compilation_database(
        Path(config.repo).expanduser().resolve(), config.compdb
    )
    if compdb is None:
        result.errors.append("cannot find a compile database; generate one first")
        result.skipped = True
        result.skip_reason = "no_compdb"
        result.elapsed_seconds = time.perf_counter() - started
        return result, previous
    units = list(load_compilation_database(str(compdb), str(Path(config.repo).expanduser().resolve())))
    new_source_errors = _bad_source_messages(config, changed_paths, units)
    if new_source_errors:
        result.errors.extend(new_source_errors)
        result.skipped = True
        result.compdb_stale = True
        result.skip_reason = "compdb_stale"
        result.elapsed_seconds = time.perf_counter() - started
        return result, latest

    messages: list[str] = []
    update = runner(
        repo=config.repo,
        db=config.db,
        compdb=str(compdb),
        changed_files=result.changed,
        jobs=config.jobs,
        layer=config.layer,
        analyze_scope=config.analyze_scope,
        record_scope=config.record_scope,
        cache_dir=config.cache_dir,
        print_summary=False,
        emit=messages.append,
    )
    result.affected_count = int(getattr(getattr(update, "plan", None), "affected_count", 0))
    result.header_change = bool(getattr(getattr(update, "plan", None), "header_change", False))
    cache_stats = dict(getattr(update, "cache_stats", {}) or {})
    result.cache_hits = int(cache_stats.get("hits", 0) or 0)
    result.cache_misses = int(cache_stats.get("misses", 0) or 0)
    result.db_size = getattr(update, "db_size", None)
    result.updated = bool(getattr(update, "ok", False)) and result.affected_count > 0
    result.deep_invalidated = result.updated
    exit_code = int(getattr(update, "exit_code", 1))
    if exit_code != 0:
        result.errors.append(str(getattr(update, "error", "") or "update failed"))
        for message in messages:
            if "error" in message.lower() or "warning" in message.lower():
                result.errors.append(message)
        result.elapsed_seconds = time.perf_counter() - started
        # Keep the previous snapshot so the same change set is retried.
        return result, previous
    if result.affected_count == 0:
        result.skipped = True
        result.skip_reason = "no_affected_tus"
        result.elapsed_seconds = time.perf_counter() - started
        return result, latest
    result.elapsed_seconds = time.perf_counter() - started
    return result, latest


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #


def _run_manual_batch(
    config: WatchConfig, update_runner: Callable[..., Any] | None = None
) -> int:
    from ca.cli.commands import run_update

    runner = update_runner or run_update
    messages: list[str] = []
    update = runner(
        repo=config.repo,
        db=config.db,
        compdb=config.compdb,
        changed_files=config.changed_files,
        jobs=config.jobs,
        layer=config.layer,
        analyze_scope=config.analyze_scope,
        record_scope=config.record_scope,
        cache_dir=config.cache_dir,
        print_summary=config.json_output,
        emit=messages.append,
    )
    result = WatchCycleResult(cycle=1)
    result.affected_count = int(getattr(getattr(update, "plan", None), "affected_count", 0))
    result.header_change = bool(getattr(getattr(update, "plan", None), "header_change", False))
    result.changed = list(config.changed_files)
    cache_stats = dict(getattr(update, "cache_stats", {}) or {})
    result.cache_hits = int(cache_stats.get("hits", 0) or 0)
    result.cache_misses = int(cache_stats.get("misses", 0) or 0)
    result.db_size = getattr(update, "db_size", None)
    result.updated = bool(getattr(update, "ok", False))
    result.elapsed_seconds = float(getattr(update, "elapsed_seconds", 0.0) or 0.0)
    if int(getattr(update, "exit_code", 1)) != 0:
        result.errors.append(str(getattr(update, "error", "") or "update failed"))
    _emit_result(config, result)
    return int(getattr(update, "exit_code", 1))


def run_watch(
    config: WatchConfig, update_runner: Callable[..., Any] | None = None
) -> int:
    """Entry point used by the CLI; returns the process exit code."""

    if not Path(config.repo).expanduser().is_dir():
        print(f"error: repository not found: {config.repo}", file=sys.stderr)
        return 1
    if config.interval < 0 or config.debounce < 0:
        print("error: --interval/--debounce must be >= 0", file=sys.stderr)
        return 1
    if config.jobs < 1:
        print("error: --jobs must be >= 1", file=sys.stderr)
        return 1
    Path(config.db).expanduser().parent.mkdir(parents=True, exist_ok=True)
    lock = _WatchLock(config.lock_path)
    try:
        lock.acquire()
    except WatchLockedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    stop = False

    def _stop(_signum: int, _frame: Any) -> None:
        nonlocal stop
        stop = True

    previous_handlers = {}
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                previous_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, _stop)
            except (ValueError, OSError):  # pragma: no cover - non-main thread
                pass
        if config.changed_files:
            return _run_manual_batch(config, update_runner)
        if config.once:
            previous = load_state(config)
            result, manifest = run_cycle(
                config, 1, previous, update_runner=update_runner
            )
            if manifest is not None:
                save_state(config, manifest)
            _emit_result(config, result)
            if result.skipped and result.skip_reason in ("compdb_stale", "no_affected_tus"):
                return 0
            return 0 if not result.errors else 1
        previous = load_state(config)
        if previous is None:
            baseline, manifest = run_cycle(
                config, 1, None, update_runner=update_runner
            )
            save_state(config, manifest or {})
            _emit_result(config, baseline)
            previous = manifest or {}
        cycle = 1
        while not stop:
            cycle += 1
            result, manifest = run_cycle(
                config, cycle, previous, update_runner=update_runner
            )
            if manifest is not None:
                save_state(config, manifest)
                previous = manifest
            _emit_result(config, result)
            if stop:
                break
            deadline = time.monotonic() + max(0.05, config.interval)
            while not stop and time.monotonic() < deadline:
                time.sleep(min(0.2, max(0.01, deadline - time.monotonic())))
        return 0
    finally:
        for signum, handler in previous_handlers.items():
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):  # pragma: no cover
                pass
        lock.release()
