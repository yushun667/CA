"""Multi-process indexing pipeline: parse/analyze in workers, write in the parent.

The pipeline owns orchestration only. Stage modules are imported lazily so that
an incomplete installation reports a per-TU failure instead of an import-time
crash, and so the CLI can show a useful error when a component is missing.

Execution model
---------------

* The parent deduplicates translation units, applies ``max_tus``, opens a
  :class:`ca.store.SqliteStore` and is the only writer.
* Each worker process parses one TU with ``ca.parse`` and analyzes every
  function definition that still has a live Clang cursor. A worker keeps a
  process-local ``seen`` set so functions defined in headers are analyzed at
  most once per worker; duplicates across workers are collapsed by the store.
* Results stream back as picklable :class:`ca.core.schema.TuWorkResult` objects
  and are written in batches. A bad TU is recorded and skipped, never fatal.

``IndexSummary`` reports per-stage CPU seconds summed across workers
(``parse_seconds``/``analyze_seconds``), parent-side write seconds
(``write_seconds``) and end-to-end wall time (``total_seconds``).
"""

from __future__ import annotations

import hashlib
import os
import pickle
import resource
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator, Sequence

from ca.core.schema import (
    SCHEMA_VERSION,
    TOOL_VERSION,
    CompilationUnit,
    IndexSummary,
    ParseResult,
    TuWorkResult,
)

__all__ = [
    "run_index",
    "index_repository",
    "find_compilation_database",
    "repository_source_files",
]

ProgressCallback = Callable[[int, int, TuWorkResult], None]

_MAX_ERROR_CHARS = 800
ANALYZE_SCOPES = ("main", "all", "none")
RECORD_SCOPES = ("auto", "all", "main_file")
TRANSPORT_FILTERS = ("none", "declarations")
# Above this TU count `record_scope=auto` switches to main-file-only extract,
# matching the default main analyze scope on large corpora (OpenHarmony scale).
AUTO_MAIN_FILE_TU_THRESHOLD = 200
# Bump when the on-disk cache payload layout changes; the key also contains the
# schema/tool versions, so stale caches are simply missed, never misread.
_CACHE_FORMAT = 1
# Deterministic worker chunks: more chunks give finer progress and better load
# balance, fewer chunks reduce repeated analysis of header definitions.
_CHUNKS_PER_WORKER = 2
# Cap chunk size so one slow/OOM-killed TU cannot delay an entire large chunk:
# progress/commit granularity and straggler impact stay bounded on huge shards.
_MAX_CHUNK_UNITS = 16
_POOL_RETRY_LIMIT = 2
_MAX_REPORTED_ERRORS = 100
# libclang AST/translation-unit memory accumulates inside a worker when the
# same process parses many large TUs. Recycle workers by default after a small
# number of chunk tasks (one chunk = at most _MAX_CHUNK_UNITS TUs); operators can
# tune via CA_MAX_TASKS_PER_CHILD (0/negative disables recycling).
_DEFAULT_POOL_RECYCLE_CHUNKS = 2
_DEFAULT_WORKER_RSS_WARN_MB = 6144

# Function ids analyzed by this process. Header definitions repeat across TUs;
# skip re-analysis once a worker has already paid for them.
_WORKER_SEEN: set[str] = set()


# --------------------------------------------------------------------------- #
# Worker side
# --------------------------------------------------------------------------- #


def _warn(message: str) -> None:
    """Operational warning on stderr (visible in shard/daemon logs)."""

    print(f"[pipeline] {message}", file=sys.stderr, flush=True)


def _describe_exception(exc: BaseException) -> str:
    message = f"{type(exc).__name__}: {exc}".strip()
    if len(message) > _MAX_ERROR_CHARS:
        message = message[: _MAX_ERROR_CHARS - 3] + "..."
    return message


def _failed_result(tu: CompilationUnit, exc: BaseException) -> TuWorkResult:
    error = _describe_exception(exc)
    return TuWorkResult(parse=ParseResult(tu=tu, parse_ok=False, error=error), error=error)


def _analysis_error_text(errors: list[str]) -> str:
    if not errors:
        return ""
    head = f"{len(errors)} function analysis error(s): {errors[0]}"
    return head[: _MAX_ERROR_CHARS]


@dataclass
class _UnitOutcome:
    """Worker result plus per-TU accounting used by diagnostics/benchmarks."""

    result: TuWorkResult
    cached: bool = False
    definitions_total: int = 0
    definitions_analyzed: int = 0
    pickle_bytes: int = 0
    transmit_bytes: int = 0


def _cache_path(cache_dir: Path, key: str) -> Path:
    return cache_dir / key[:2] / f"{key}.pkl"


def _unit_cache_key(
    tu: CompilationUnit, analyze_scope: str = "main", record_scope: str = "all"
) -> str:
    """Content hash of the TU source plus its exact compile arguments.

    Header contents are intentionally *not* hashed: a header change does not
    invalidate cached TUs, so after changing a header run a full index without
    ``cache_dir`` (documented limitation).
    """

    digest = hashlib.sha256()
    parts = (
        b"ca-tu-cache-v1",
        TOOL_VERSION.encode("utf-8"),
        SCHEMA_VERSION.encode("utf-8"),
        tu.file.encode("utf-8", "surrogatepass"),
        tu.language.encode("utf-8", "surrogatepass"),
        tu.driver.encode("utf-8", "surrogatepass"),
        analyze_scope.encode("utf-8", "surrogatepass"),
        record_scope.encode("utf-8", "surrogatepass"),
    )
    for part in parts:
        digest.update(part)
        digest.update(b"\x00")
    for argument in tu.arguments:
        digest.update(argument.encode("utf-8", "surrogatepass"))
        digest.update(b"\x00")
    try:
        with Path(tu.file).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        digest.update(b"<source-unreadable>")
    return digest.hexdigest()


def _cache_load(cache_dir: Path, key: str, tu: CompilationUnit) -> _UnitOutcome | None:
    """Return a cached outcome, or ``None`` for a miss/corrupt/stale entry."""

    started = time.perf_counter()
    path = _cache_path(cache_dir, key)
    try:
        payload = pickle.loads(path.read_bytes())
        if not isinstance(payload, dict) or payload.get("format") != _CACHE_FORMAT:
            return None
        if payload.get("key") != key or payload.get("tool") != TOOL_VERSION:
            return None
        parse = payload["parse"]
        analyses = payload["analyses"]
        if not isinstance(parse, ParseResult):
            return None
    except FileNotFoundError:
        return None
    except Exception:
        # Corrupt or truncated cache entry: drop it and parse normally.
        try:
            path.unlink()
        except OSError:
            pass
        return None
    parse.tu = tu  # cached records must point at the current unit object
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    parse.elapsed_ms = max(elapsed_ms, 0.001)
    for analysis in analyses:
        analysis.elapsed_ms = 0.0
    return _UnitOutcome(
        TuWorkResult(parse=parse, analyses=list(analyses)), cached=True
    )


def _cache_store(cache_dir: Path, key: str, result: TuWorkResult) -> None:
    """Best-effort atomic cache write; failures never fail the TU."""

    payload = {
        "format": _CACHE_FORMAT,
        "key": key,
        "tool": TOOL_VERSION,
        "schema": SCHEMA_VERSION,
        "file": result.parse.tu.file,
        "parse": result.parse,
        "analyses": result.analyses,
    }
    path = _cache_path(cache_dir, key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temporary.write_bytes(pickle.dumps(payload, protocol=4))
        os.replace(temporary, path)
    except OSError:
        pass


def _main_file_relative(tu: CompilationUnit) -> str:
    """Repository-relative POSIX path of the TU's main source file."""

    try:
        return Path(tu.file).resolve().relative_to(Path(tu.repo_root).resolve()).as_posix()
    except (ValueError, OSError):
        return ""


def _is_main_definition(function: Any, tu: CompilationUnit, main_relative: str) -> bool:
    """True when a definition lives in the TU's own source file (not a header)."""

    if main_relative and function.sloc.file == main_relative:
        return True
    try:
        return Path(str(function.sloc.file)).resolve() == Path(tu.file).resolve()
    except OSError:
        return False


def _apply_transport_filter(
    parse: ParseResult, transport_filter: str
) -> tuple[ParseResult, int, int]:
    """Return (possibly filtered parse result, transmitted bytes, raw bytes).

    ``declarations`` drops declaration FunctionRecords from the cross-process
    payload. Call sites keep names/USRs, so unresolved callees are still
    represented by store placeholders; symbol rows for pure declarations are
    intentionally sacrificed (lossy, opt-in transport saving).
    """

    raw_bytes = len(pickle.dumps(parse, protocol=4))
    if transport_filter == "declarations":
        parse.functions = [record for record in parse.functions if record.is_definition]
    transmit_bytes = (
        len(pickle.dumps(parse, protocol=4))
        if transport_filter != "none"
        else raw_bytes
    )
    return parse, transmit_bytes, raw_bytes


def _chunk_units(
    units: Sequence[CompilationUnit], jobs: int
) -> tuple[int, list[list[CompilationUnit]]]:
    workers = max(1, min(int(jobs), len(units)))
    by_workers = workers * _CHUNKS_PER_WORKER
    by_cap = (len(units) + _MAX_CHUNK_UNITS - 1) // _MAX_CHUNK_UNITS
    chunk_count = max(workers, min(len(units), max(by_workers, by_cap)))
    chunks: list[list[CompilationUnit]] = [[] for _ in range(chunk_count)]
    for index, unit in enumerate(units):
        chunks[index % chunk_count].append(unit)
    return workers, [chunk for chunk in chunks if chunk]


def _header_declarations_enabled() -> bool:
    """Whether header function declarations are emitted for main-file scope.

    Default on (bounded by ``header_declaration_limit``); ``run_index`` may force
    it via the ``CA_HEADER_DECLARATIONS`` environment variable so worker
    processes (spawn or fork) inherit the choice.
    """

    raw = os.environ.get("CA_HEADER_DECLARATIONS")
    if raw is None:
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def _parse_options(record_scope: str) -> Any:
    """Build parser options for the requested record scope (lazy import)."""

    if record_scope != "main_file":
        return None
    from ca.parse import ParseOptions

    kwargs: dict[str, Any] = {"record_scope": "main_file"}
    fields = getattr(ParseOptions, "__dataclass_fields__", {})
    if "emit_header_declarations" in fields:
        kwargs["emit_header_declarations"] = _header_declarations_enabled()
        if "header_declaration_limit" in fields:
            kwargs["header_declaration_limit"] = 2000
    return ParseOptions(**kwargs)


def _analyze_unit(
    tu: CompilationUnit,
    cache_dir: str | None = None,
    analyze_scope: str = "main",
    transport_filter: str = "none",
    measure_transport: bool = False,
    record_scope: str = "all",
) -> _UnitOutcome:
    """Worker entry point: parse one TU, then analyze its function definitions.

    Runs inside a subprocess, so every object it returns must be picklable
    (``ParseResult``/``AnalysisResult``/``TuWorkResult`` only).
    """

    cache_path: Path | None = None
    cache_key = ""
    if cache_dir and analyze_scope != "all":
        cache_path = Path(cache_dir)
        cache_key = _unit_cache_key(tu, analyze_scope, record_scope)
        cached = _cache_load(cache_path, cache_key, tu)
        if cached is not None:
            return cached

    parse_started = time.perf_counter()
    try:
        from ca.parse import parse_compilation_unit
    except Exception as exc:  # pragma: no cover - depends on installation state
        return _UnitOutcome(_failed_result(tu, exc))
    analyze_function = None
    stage_error = ""
    try:
        from ca.analyze import analyze_function
    except Exception as exc:  # analysis missing: still persist parse records
        stage_error = f"analyze stage unavailable: {_describe_exception(exc)}"

    try:
        parsed = parse_compilation_unit(tu, _parse_options(record_scope))
    except Exception as exc:
        return _UnitOutcome(_failed_result(tu, exc))

    parse_seconds = time.perf_counter() - parse_started
    analyses = []
    analysis_errors: list[str] = []
    main_relative = _main_file_relative(tu)
    definitions_total = 0
    if analyze_function is None or analyze_scope == "none":
        function_iter = ()
    else:
        function_iter = parsed.functions
    for function in function_iter:
        if not getattr(function, "is_definition", False):
            continue
        definitions_total += 1
        if analyze_scope == "main" and not _is_main_definition(
            function, tu, main_relative
        ):
            continue
        function_id = getattr(function, "id", "")
        if not function_id or function_id in _WORKER_SEEN:
            continue
        try:
            cursor = parsed.definition_cursor(function_id)
        except Exception:
            cursor = None
        if cursor is None:
            continue
        _WORKER_SEEN.add(function_id)
        try:
            analyses.append(analyze_function(parsed, function))
        except Exception as exc:
            analysis_errors.append(f"{function.name}: {_describe_exception(exc)}")

    try:
        parse_result = parsed.parse_result()
    except Exception as exc:
        return _UnitOutcome(_failed_result(tu, exc))

    # Measured wall time is authoritative; the parser's own timing is a hint.
    if parse_result.elapsed_ms <= 0:
        parse_result.elapsed_ms = parse_seconds * 1000.0
    raw_bytes = transmit_bytes = 0
    if measure_transport:
        parse_result, transmit_bytes, raw_bytes = _apply_transport_filter(
            parse_result, transport_filter
        )
    elif transport_filter != "none":
        parse_result, transmit_bytes, raw_bytes = _apply_transport_filter(
            parse_result, transport_filter
        )
    result = TuWorkResult(
        parse=parse_result,
        analyses=analyses,
        error=_analysis_error_text(analysis_errors) or stage_error,
    )
    if cache_path is not None and parse_result.parse_ok and not result.error:
        _cache_store(cache_path, cache_key, result)
    return _UnitOutcome(
        result,
        definitions_total=definitions_total,
        definitions_analyzed=len(analyses),
        pickle_bytes=raw_bytes,
        transmit_bytes=transmit_bytes,
    )


def _ensure_worker_import_path() -> None:
    """Make ``import ca`` work in spawned workers (macOS default start method).

    pytest injects ``src`` through ``sys.path`` only; spawned processes rebuild
    their path from the environment, so export the package root as PYTHONPATH.
    """

    src_root = Path(__file__).resolve().parents[1]
    if not (src_root / "ca").is_dir():
        return
    entry = str(src_root)
    parts = [part for part in os.environ.get("PYTHONPATH", "").split(os.pathsep) if part]
    if entry not in parts:
        os.environ["PYTHONPATH"] = os.pathsep.join([entry, *parts])


def _pool_recycle_chunks() -> int:
    """Chunk tasks per worker before the pool generation is recreated.

    ``max_tasks_per_child`` in CPython 3.11 can hang when workers are recycled
    under load, so memory is bounded by rebuilding the whole pool per generation
    instead (clean process exit + fresh workers). Default: 2 chunks per worker
    (<= 32 TUs), configurable through ``CA_POOL_RECYCLE_CHUNKS`` (positive int);
    the older ``CA_MAX_TASKS_PER_CHILD`` is accepted as a fallback, capped at 4
    to keep generations short.
    """

    raw = os.environ.get("CA_POOL_RECYCLE_CHUNKS")
    if raw is None:
        legacy = os.environ.get("CA_MAX_TASKS_PER_CHILD")
        if legacy is not None:
            try:
                return max(1, min(4, int(str(legacy).strip())))
            except (TypeError, ValueError):
                pass
        return _DEFAULT_POOL_RECYCLE_CHUNKS
    try:
        return max(1, int(str(raw).strip()))
    except (TypeError, ValueError):
        return _DEFAULT_POOL_RECYCLE_CHUNKS


def _new_pool(workers: int) -> ProcessPoolExecutor:
    """Plain pool; memory is bounded by wave-based pool recreation."""

    return ProcessPoolExecutor(max_workers=max(1, int(workers)))


def _peak_rss_mb() -> float:
    """Peak RSS of the current process from the OS (MB)."""

    try:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        peak = float(getattr(usage, "ru_maxrss", 0.0))
        # Linux reports KiB, macOS reports bytes.
        if sys.platform == "darwin":
            return peak / (1024.0 * 1024.0)
        return peak / 1024.0
    except Exception:  # pragma: no cover - resource is available everywhere
        return 0.0


def _log_worker_rss(label: str, units: int) -> None:
    """Log worker peak RSS when it exceeds the warn threshold or on request."""

    peak = _peak_rss_mb()
    try:
        threshold = int(os.environ.get("CA_WORKER_RSS_WARN_MB", str(_DEFAULT_WORKER_RSS_WARN_MB)))
    except (TypeError, ValueError):
        threshold = _DEFAULT_WORKER_RSS_WARN_MB
    always = os.environ.get("CA_WORKER_RSS_LOG", "") not in ("", "0", "false")
    if always or (threshold > 0 and peak >= threshold):
        print(
            f"[pipeline-worker] {label}: peak_rss={peak:.0f}MB units={units}",
            file=sys.stderr,
            flush=True,
        )


def _analyze_chunk(
    units: Sequence[CompilationUnit],
    cache_dir: str | None,
    analyze_scope: str = "main",
    transport_filter: str = "none",
    measure_transport: bool = False,
    record_scope: str = "all",
) -> list[_UnitOutcome]:
    """Worker entry point for a deterministic chunk of translation units.

    The process-local dedupe set is cleared per chunk: who runs which chunk must
    not change the produced records, otherwise identical commands would return
    slightly different fact sets depending on process scheduling.
    """

    _WORKER_SEEN.clear()
    outcomes = [
        _analyze_unit(
            unit,
            cache_dir,
            analyze_scope,
            transport_filter,
            measure_transport,
            record_scope,
        )
        for unit in units
    ]
    _log_worker_rss("analyze", len(units))
    return outcomes


def _iter_results(
    tus: Sequence[CompilationUnit],
    jobs: int,
    cache_dir: str | None = None,
    analyze_scope: str = "main",
    transport_filter: str = "none",
    measure_transport: bool = False,
    metrics: dict[str, object] | None = None,
    record_scope: str = "all",
) -> Iterator[_UnitOutcome]:
    if analyze_scope == "all" and len(tus) > 1:
        yield from _iter_all_scope_results(
            tus, jobs, transport_filter, measure_transport, metrics, record_scope
        )
        return
    if jobs <= 1 or len(tus) <= 1:
        _WORKER_SEEN.clear()
        for tu in tus:
            yield _analyze_unit(
                tu,
                cache_dir,
                analyze_scope,
                transport_filter,
                measure_transport,
                record_scope,
            )
        return

    _ensure_worker_import_path()
    remaining: list[CompilationUnit] = list(tus)
    attempt = 0
    while remaining:
        current_jobs = max(1, int(jobs) >> attempt)
        workers, chunks = _chunk_units(remaining, current_jobs)
        recycling = _pool_recycle_chunks()
        wave_chunks = max(1, workers * recycling)
        leftovers: list[CompilationUnit] = []
        broken = False
        for wave_start in range(0, len(chunks), wave_chunks):
            wave = chunks[wave_start : wave_start + wave_chunks]
            with _new_pool(workers) as pool:
                futures = {
                    pool.submit(
                        _analyze_chunk,
                        chunk,
                        cache_dir,
                        analyze_scope,
                        transport_filter,
                        measure_transport,
                        record_scope,
                    ): chunk
                    for chunk in wave
                }
                for future in as_completed(futures):
                    chunk = futures[future]
                    try:
                        outcomes = future.result()
                    except BrokenProcessPool as exc:
                        # A worker was killed (e.g. OOM). Retry this chunk in a
                        # later generation instead of failing the entire run.
                        broken = True
                        leftovers.extend(chunk)
                        if metrics is not None:
                            metrics["worker_pool_restarts"] = int(
                                metrics.get("worker_pool_restarts", 0) or 0
                            ) + 1
                            metrics["worker_pool_last_error"] = (
                                f"{type(exc).__name__}: {exc}"
                            )
                        continue
                    except Exception as exc:
                        for unit in chunk:
                            yield _UnitOutcome(_failed_result(unit, exc))
                        continue
                    yield from outcomes
        if not broken:
            return
        attempt += 1
        if metrics is not None:
            metrics["worker_pool_retried_units"] = int(
                metrics.get("worker_pool_retried_units", 0) or 0
            ) + len(leftovers)
        if attempt > _POOL_RETRY_LIMIT:
            _warn(
                f"worker pool broke {attempt} times; serial fallback for"
                f" {len(leftovers)} remaining TUs"
            )
            for unit in leftovers:
                _WORKER_SEEN.clear()
                try:
                    yield _analyze_unit(
                        unit,
                        cache_dir,
                        analyze_scope,
                        transport_filter,
                        measure_transport,
                        record_scope,
                    )
                except Exception as exc:
                    yield _UnitOutcome(_failed_result(unit, exc))
            return
        _warn(
            f"worker pool broken (attempt {attempt}); retrying"
            f" {len(leftovers)} TUs with reduced jobs"
        )
        remaining = leftovers


# --------------------------------------------------------------------------- #
# ``analyze_scope="all"``: two-phase deterministic ownership
# --------------------------------------------------------------------------- #


def _parse_chunk_info(
    units: Sequence[CompilationUnit],
    record_scope: str = "all",
) -> tuple[list[tuple[str, tuple[str, ...]]], float]:
    """Phase A worker: parse a chunk and return (tu_id, definition files).

    Only the set of files that contribute function definitions crosses the
    process boundary, which keeps the phase-A payload tiny next to a full
    ``ParseResult`` (8-11 MB/TU on OpenHarmony).
    """

    from ca.parse import parse_compilation_unit

    _WORKER_SEEN.clear()
    infos: list[tuple[str, tuple[str, ...]]] = []
    cpu_seconds = 0.0
    for unit in units:
        started = time.perf_counter()
        try:
            parsed = parse_compilation_unit(unit, _parse_options(record_scope))
            cpu_seconds += time.perf_counter() - started
            files = {
                function.sloc.file
                for function in parsed.functions
                if getattr(function, "is_definition", False) and function.sloc.file
            }
            infos.append((unit.id, tuple(sorted(files))))
        except Exception:
            cpu_seconds += time.perf_counter() - started
            infos.append((unit.id, ()))
    _log_worker_rss("parse-info", len(units))
    return infos, cpu_seconds


def _analyze_chunk_owned(
    items: Sequence[tuple[CompilationUnit, tuple[str, ...]]],
    transport_filter: str = "none",
    measure_transport: bool = False,
    record_scope: str = "all",
) -> list[_UnitOutcome]:
    """Phase B worker: re-parse a TU and analyze only the owned definition files.

    Each definition file has exactly one owning TU (the first TU in repository
    order that defines anything in it), so every definition is analyzed exactly
    once globally, even when the same header is parsed by many TUs.
    """

    from ca.analyze import analyze_function
    from ca.parse import parse_compilation_unit

    outcomes: list[_UnitOutcome] = []
    for unit, owned_files in items:
        _WORKER_SEEN.clear()
        owned = set(owned_files)
        try:
            parsed = parse_compilation_unit(unit, _parse_options(record_scope))
        except Exception as exc:
            outcomes.append(_UnitOutcome(_failed_result(unit, exc)))
            continue
        analyses = []
        analysis_errors: list[str] = []
        definitions_total = 0
        for function in parsed.functions:
            if not getattr(function, "is_definition", False):
                continue
            definitions_total += 1
            if function.sloc.file not in owned:
                continue
            function_id = getattr(function, "id", "")
            if not function_id or function_id in _WORKER_SEEN:
                continue
            try:
                cursor = parsed.definition_cursor(function_id)
            except Exception:
                cursor = None
            if cursor is None:
                continue
            _WORKER_SEEN.add(function_id)
            try:
                analyses.append(analyze_function(parsed, function))
            except Exception as exc:
                analysis_errors.append(f"{function.name}: {_describe_exception(exc)}")
        try:
            parse_result = parsed.parse_result()
        except Exception as exc:
            outcomes.append(_UnitOutcome(_failed_result(unit, exc)))
            continue
        raw_bytes = transmit_bytes = 0
        if measure_transport or transport_filter != "none":
            parse_result, transmit_bytes, raw_bytes = _apply_transport_filter(
                parse_result, transport_filter
            )
        outcomes.append(
            _UnitOutcome(
                TuWorkResult(
                    parse=parse_result,
                    analyses=analyses,
                    error=_analysis_error_text(analysis_errors),
                ),
                definitions_total=definitions_total,
                definitions_analyzed=len(analyses),
                pickle_bytes=raw_bytes,
                transmit_bytes=transmit_bytes,
            )
        )
    return outcomes


def _iter_all_scope_results(
    tus: Sequence[CompilationUnit],
    jobs: int,
    transport_filter: str = "none",
    measure_transport: bool = False,
    metrics: dict[str, object] | None = None,
    record_scope: str = "all",
) -> Iterator[_UnitOutcome]:
    """Two-phase analysis for ``analyze_scope="all"``.

    Phase A parses every TU and returns per-TU definition files; the parent then
    assigns each definition file to exactly one owning TU (first in repository
    order). Phase B re-parses those TUs and deep-analyzes only their owned
    files. Trade-off: one extra parse pass, but header definitions are analyzed
    once instead of once per including TU (the OpenHarmony bottleneck).
    """

    _ensure_worker_import_path()
    workers, chunks = _chunk_units(tus, jobs)
    phase_started = time.perf_counter()

    # Phase A: run chunks in parallel but consume them in order so ownership is
    # deterministic (first TU in repository order wins).
    owner: dict[str, str] = {}
    owned_files: dict[str, list[str]] = {}
    phase_a_cpu = 0.0
    phase_a_remaining: list[list[CompilationUnit]] = list(chunks)
    phase_a_attempt = 0
    while phase_a_remaining:
        phase_a_broken = False
        phase_a_leftover: list[list[CompilationUnit]] = []
        phase_a_workers = max(1, workers >> phase_a_attempt)
        wave_chunks = max(1, phase_a_workers * _pool_recycle_chunks())
        for wave_start in range(0, len(phase_a_remaining), wave_chunks):
          wave = phase_a_remaining[wave_start : wave_start + wave_chunks]
          with _new_pool(phase_a_workers) as pool:
            futures = [
                pool.submit(_parse_chunk_info, chunk, record_scope)
                for chunk in wave
            ]
            for future, chunk in zip(futures, wave):
                try:
                    infos, cpu_seconds = future.result()
                    phase_a_cpu += cpu_seconds
                except BrokenProcessPool as exc:
                    phase_a_broken = True
                    phase_a_leftover.append(chunk)
                    if metrics is not None:
                        metrics["worker_pool_restarts"] = int(
                            metrics.get("worker_pool_restarts", 0) or 0
                        ) + 1
                        metrics["worker_pool_last_error"] = (
                            f"{type(exc).__name__}: {exc}"
                        )
                    continue
                except Exception:
                    continue
                for tu_id, files in infos:
                    claim: list[str] = []
                    for file in files:
                        if file not in owner:
                            owner[file] = tu_id
                            claim.append(file)
                    if claim:
                        owned_files[tu_id] = claim
        if not phase_a_broken:
            break
        phase_a_attempt += 1
        _warn(
            f"phase A worker pool broken (attempt {phase_a_attempt}); retrying"
            f" {len(phase_a_leftover)} chunks with reduced jobs"
        )
        if phase_a_attempt > _POOL_RETRY_LIMIT:
            _warn("phase A serial fallback for remaining chunks")
            for chunk in phase_a_leftover:
                try:
                    infos, cpu_seconds = _parse_chunk_info(chunk, record_scope)
                    phase_a_cpu += cpu_seconds
                except Exception:
                    continue
                for tu_id, files in infos:
                    claim = []
                    for file in files:
                        if file not in owner:
                            owner[file] = tu_id
                            claim.append(file)
                    if claim:
                        owned_files[tu_id] = claim
            break
        phase_a_remaining = phase_a_leftover
    phase_a_seconds = time.perf_counter() - phase_started
    if metrics is not None:
        metrics["phase_a_seconds"] = round(phase_a_seconds, 3)
        metrics["phase_a_cpu_seconds"] = round(phase_a_cpu, 3)

    # Phase B: chunks of (unit, owned files); outcomes stream to the writer.
    items: list[tuple[CompilationUnit, tuple[str, ...]]] = [
        (unit, tuple(owned_files.get(unit.id, ()))) for unit in tus
    ]
    chunk_items: list[list[tuple[CompilationUnit, tuple[str, ...]]]] = [
        [] for _ in chunks
    ]
    for index, item in enumerate(items):
        chunk_items[index % len(chunk_items)].append(item)
    phase_b_remaining = [chunk for chunk in chunk_items if chunk]
    phase_b_attempt = 0
    while phase_b_remaining:
        phase_b_broken = False
        phase_b_leftover: list[list[tuple[CompilationUnit, tuple[str, ...]]]] = []
        phase_b_workers = max(1, workers >> phase_b_attempt)
        wave_chunks = max(1, phase_b_workers * _pool_recycle_chunks())
        for wave_start in range(0, len(phase_b_remaining), wave_chunks):
          wave = phase_b_remaining[wave_start : wave_start + wave_chunks]
          with _new_pool(phase_b_workers) as pool:
            futures = {
                pool.submit(
                    _analyze_chunk_owned,
                    chunk,
                    transport_filter,
                    measure_transport,
                    record_scope,
                ): chunk
                for chunk in wave
            }
            for future in as_completed(futures):
                chunk = futures[future]
                try:
                    yield from future.result()
                except BrokenProcessPool as exc:
                    phase_b_broken = True
                    phase_b_leftover.append(chunk)
                    if metrics is not None:
                        metrics["worker_pool_restarts"] = int(
                            metrics.get("worker_pool_restarts", 0) or 0
                        ) + 1
                        metrics["worker_pool_last_error"] = (
                            f"{type(exc).__name__}: {exc}"
                        )
                    continue
                except Exception as exc:  # pragma: no cover - defensive
                    for unit, _owned in chunk:
                        yield _UnitOutcome(_failed_result(unit, exc))
        if not phase_b_broken:
            break
        phase_b_attempt += 1
        if phase_b_attempt > _POOL_RETRY_LIMIT:
            for chunk in phase_b_leftover:
                for unit, _owned in chunk:
                    yield _UnitOutcome(
                        _failed_result(
                            unit,
                            RuntimeError(
                                "worker pool broke repeatedly; giving up on remaining TUs"
                            ),
                        )
                    )
            break
        phase_b_remaining = phase_b_leftover


# --------------------------------------------------------------------------- #
# Parent side
# --------------------------------------------------------------------------- #


def _record_file(rel_file: str, bucket: set[str]) -> None:
    if rel_file:
        bucket.add(rel_file)


def _collect_files(parse: ParseResult) -> set[str]:
    files: set[str] = set()
    _record_file(parse.tu.file, files)
    for record in parse.functions:
        _record_file(record.sloc.file, files)
    for record in parse.types:
        _record_file(record.sloc.file, files)
    for record in parse.fields:
        _record_file(record.sloc.file, files)
    for record in parse.calls:
        _record_file(record.sloc.file, files)
    return files


def _count_lines_of_code(repo_root: Path, rel_files: Iterable[str]) -> int:
    total = 0
    seen: set[str] = set()
    for rel_file in rel_files:
        if rel_file in seen:
            continue
        seen.add(rel_file)
        path = Path(rel_file)
        if not path.is_absolute():
            path = repo_root / rel_file
        try:
            with path.open("rb") as handle:
                total += sum(1 for _ in handle)
        except OSError:
            continue
    return total


def _unique_units(units: Iterable[CompilationUnit]) -> list[CompilationUnit]:
    unique: list[CompilationUnit] = []
    seen: set[str] = set()
    for unit in units:
        key = unit.id or f"{unit.file}|{unit.directory}|{unit.arguments!r}"
        if key in seen:
            continue
        seen.add(key)
        unique.append(unit)
    return unique


def run_index(
    repo_root: str,
    compilation_units: Sequence[CompilationUnit],
    db_path: str,
    *,
    jobs: int = 8,
    max_tus: int | None = None,
    batch_size: int = 128,
    progress: ProgressCallback | None = None,
    produced: dict[str, int] | None = None,
    cache_dir: str | None = None,
    cache_stats: dict[str, object] | None = None,
    analyze_scope: str = "main",
    record_scope: str = "auto",
    transport_filter: str = "none",
    measure_transport: bool = False,
    metrics: dict[str, object] | None = None,
    dedupe_files: bool = False,
    header_declarations: bool | None = None,
) -> IndexSummary:
    """Index ``compilation_units`` (under ``repo_root``) into SQLite.

    ``max_tus`` truncates the unit list before parsing (benchmarks and smoke
    tests use it). ``progress`` is called after each finished TU with
    ``(done, total, result)``; the CLI uses it for stderr progress.

    ``IndexSummary`` counts unique persisted rows (cross-TU duplicates are
    collapsed at ingest). When ``produced`` is given, it is filled with the raw
    per-TU record counts before dedupe under the keys ``functions``, ``types``,
    ``fields`` and ``calls`` (diagnostic only, not part of the schema).

    Incremental mode is enabled with ``cache_dir``: the cache stores one
    ``ParseResult`` plus its ``AnalysisResult`` list per TU, keyed by a hash of
    the TU source content, language/driver and exact compile arguments. Cache
    hits skip parse+analyze and replay the cached records into the store.
    Header contents are not part of the key (see :func:`_unit_cache_key`), so a
    header change requires a full rebuild. ``cache_stats`` (diagnostic, like
    ``produced``) receives ``hits``/``misses``/``tus``/``cache_dir``.

    ``analyze_scope`` controls deep analysis only (symbol/call extraction is
    unaffected): ``main`` (default) analyzes definitions whose source file is
    the TU's own source file, ``all`` analyzes every definition with ``"all"``
    cross-worker deduplication via deterministic definition-file ownership (one
    extra parse pass), and ``none`` skips analysis entirely. ``transport_filter``
    may drop declaration records from the cross-process payload (lossy, opt-in);
    ``measure_transport`` records pickle sizes and ``metrics`` (diagnostic
    out-dict, like ``produced``/``cache_stats``) receives the scope counters,
    serialization bytes and phase timings.
    """

    if analyze_scope not in ANALYZE_SCOPES:
        raise ValueError(
            f"analyze_scope must be one of {', '.join(ANALYZE_SCOPES)},"
            f" got {analyze_scope!r}"
        )
    if transport_filter not in TRANSPORT_FILTERS:
        raise ValueError(
            f"transport_filter must be one of {', '.join(TRANSPORT_FILTERS)},"
            f" got {transport_filter!r}"
        )
    if record_scope not in RECORD_SCOPES:
        raise ValueError(
            f"record_scope must be one of {', '.join(RECORD_SCOPES)},"
            f" got {record_scope!r}"
        )
    wall_started = time.perf_counter()
    repo = Path(repo_root).resolve()
    db = Path(db_path).resolve()
    db.parent.mkdir(parents=True, exist_ok=True)

    previous_header_declarations = os.environ.get("CA_HEADER_DECLARATIONS")
    if header_declarations is not None:
        os.environ["CA_HEADER_DECLARATIONS"] = "1" if header_declarations else "0"
    units = _unique_units(compilation_units)
    units_before_dedupe = len(units)
    if dedupe_files:
        seen_files: set[str] = set()
        deduped: list[CompilationUnit] = []
        for unit in units:
            try:
                key = str(Path(unit.file).resolve())
            except OSError:
                key = unit.file
            if key in seen_files:
                continue
            seen_files.add(key)
            deduped.append(unit)
        units = deduped
    if max_tus is not None:
        if max_tus < 0:
            raise ValueError(f"max_tus must be >= 0, got {max_tus}")
        units = units[:max_tus]

    resolved_cache: Path | None = None
    if cache_dir:
        resolved_cache = Path(cache_dir).expanduser().resolve()
        try:
            resolved_cache.mkdir(parents=True, exist_ok=True)
            # Self-ignoring cache: keeps `git status` clean when the DB (and
            # therefore the default cache dir) lives inside the repository.
            ignore = resolved_cache / ".gitignore"
            if not ignore.exists():
                ignore.write_text("*\n", encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"cannot create cache directory {resolved_cache}: {exc}")
    cache_hits = 0
    cache_misses = 0
    resolved_record_scope = record_scope
    if record_scope == "auto":
        resolved_record_scope = (
            "main_file" if len(units) > AUTO_MAIN_FILE_TU_THRESHOLD else "all"
        )
    cache_disabled_reason = ""
    if analyze_scope == "all" and resolved_cache is not None:
        cache_disabled_reason = (
            "incremental cache is not used for analyze_scope=all"
            " (per-TU ownership is corpus-wide)"
        )
        resolved_cache = None

    scope_metrics: dict[str, object] = {}
    summary = IndexSummary(repo_root=str(repo), db_path=str(db), tus_total=len(units))
    produced_counts = {"functions": 0, "types": 0, "fields": 0, "calls": 0}
    errors: list[str] = []
    reported_worker_errors: set[str] = set()
    loc_files: set[str] = set()
    write_seconds = 0.0
    pending = 0

    from ca.store import SqliteStore  # lazy: keep import-time cost out of CLI startup

    store = SqliteStore(str(db))
    store.open()
    try:
        store.begin()
        outcomes = _iter_results(
            units,
            int(jobs),
            str(resolved_cache) if resolved_cache else None,
            analyze_scope,
            transport_filter,
            measure_transport,
            scope_metrics,
            resolved_record_scope,
        )
        definitions_total = 0
        definitions_analyzed = 0
        pickle_bytes_total = 0
        transmit_bytes_total = 0
        for done, outcome in enumerate(outcomes, start=1):
            definitions_total += outcome.definitions_total
            definitions_analyzed += outcome.definitions_analyzed
            pickle_bytes_total += outcome.pickle_bytes
            transmit_bytes_total += outcome.transmit_bytes
            result = outcome.result
            if outcome.cached:
                cache_hits += 1
            else:
                cache_misses += 1
            tu = result.parse.tu
            write_started = time.perf_counter()
            produced_counts["functions"] += len(result.parse.functions)
            produced_counts["types"] += len(result.parse.types)
            produced_counts["fields"] += len(result.parse.fields)
            produced_counts["calls"] += len(result.parse.calls)
            try:
                store.add_parse_result(result.parse)
                for analysis in result.analyses:
                    store.add_analysis_result(analysis)
                wrote = True
            except Exception as exc:
                errors.append(f"{tu.file}: write failed: {_describe_exception(exc)}")
                wrote = False
            write_seconds += time.perf_counter() - write_started

            parse = result.parse
            # A TU with error diagnostics can still yield usable records (e.g.
            # cross-target flags libclang dislikes but C source it understands).
            # Count it as parsed so callers can use the data, and report the
            # degradation through the error list below.
            usable = bool(parse.functions or parse.calls or parse.types)
            parsed_ok = parse.parse_ok or usable
            if not wrote:
                summary.tus_failed += 1
            elif parsed_ok:
                summary.tus_parsed += 1
                parse = result.parse
                summary.functions += len(parse.functions)
                summary.types += len(parse.types)
                summary.calls += len(parse.calls)
                summary.analyzed_functions += len(result.analyses)
                for analysis in result.analyses:
                    summary.branches += len(analysis.branches)
                    summary.assignments += len(analysis.assignments)
                    summary.returns += len(analysis.returns)
                    summary.resource_ops += len(analysis.resource_ops)
                    summary.findings += len(analysis.findings)
                    summary.analyze_seconds += analysis.elapsed_ms / 1000.0
                summary.parse_seconds += parse.elapsed_ms / 1000.0
                loc_files |= _collect_files(parse)
            else:
                summary.tus_failed += 1
                detail = result.parse.error or result.error or "parse failed"
                errors.append(f"{tu.file}: {detail}")

            if not parse.parse_ok and parsed_ok:
                reported = f"parse degraded: {parse.error or result.error or 'diagnostics'}"
                if reported not in reported_worker_errors:
                    reported_worker_errors.add(reported)
                    errors.append(f"{tu.file}: {reported}")
            if result.error and parse.parse_ok:
                # Analysis degraded for this TU; parsing/writing still succeeded.
                reported = f"analysis: {result.error}"
                if reported not in reported_worker_errors:
                    reported_worker_errors.add(reported)
                    errors.append(f"{tu.file}: {reported}")

            pending += 1
            if pending >= batch_size and done < len(units):
                commit_started = time.perf_counter()
                store.commit()
                write_seconds += time.perf_counter() - commit_started
                store.begin()
                pending = 0
            if progress is not None:
                try:
                    progress(done, len(units), result)
                except Exception:  # progress is best effort, never fatal
                    pass

        if pending:
            commit_started = time.perf_counter()
            store.commit()
            write_seconds += time.perf_counter() - commit_started
        finalize_started = time.perf_counter()
        store.finalize()
        write_seconds += time.perf_counter() - finalize_started
        # Report unique persisted rows: headers are parsed (and counted) once per
        # TU but the store collapses duplicate stable ids at ingest.
        try:
            tables = dict(store.stats().get("tables") or {})
        except Exception:
            tables = {}
        if tables:
            summary.functions = int(tables.get("functions", summary.functions))
            summary.types = int(tables.get("types", summary.types))
            summary.calls = int(tables.get("calls", summary.calls))
            summary.branches = int(tables.get("branches", summary.branches))
            summary.assignments = int(tables.get("assignments", summary.assignments))
            summary.returns = int(tables.get("returns", summary.returns))
            summary.resource_ops = int(tables.get("resource_ops", summary.resource_ops))
            summary.findings = int(tables.get("findings", summary.findings))
    except Exception as exc:
        errors.append(f"pipeline: {_describe_exception(exc)}")
        raise
    finally:
        try:
            store.close()
        except Exception:
            pass

    summary.files = len(loc_files)
    summary.lines_of_code = _count_lines_of_code(repo, loc_files)
    summary.write_seconds = write_seconds
    summary.total_seconds = time.perf_counter() - wall_started
    summary.parse_seconds = round(summary.parse_seconds, 6)
    summary.analyze_seconds = round(summary.analyze_seconds, 6)
    if metrics is not None:
        metrics.clear()
        metrics.update(
            {
                "analyze_scope": analyze_scope,
                "record_scope": resolved_record_scope,
                "transport_filter": transport_filter,
                "definitions_total": definitions_total,
                "definitions_analyzed": definitions_analyzed,
                "definitions_suppressed": max(
                    0, definitions_total - definitions_analyzed
                ),
                "pickle_bytes_total": pickle_bytes_total,
                "transmit_bytes_total": transmit_bytes_total,
                "pickle_bytes_avg": (
                    round(pickle_bytes_total / len(units), 1) if units else 0.0
                ),
                "transmit_bytes_avg": (
                    round(transmit_bytes_total / len(units), 1) if units else 0.0
                ),
                "phase_a_seconds": scope_metrics.get("phase_a_seconds", 0.0),
                "phase_a_cpu_seconds": scope_metrics.get("phase_a_cpu_seconds", 0.0),
                "cache_disabled_reason": cache_disabled_reason,
                "worker_pool_restarts": scope_metrics.get("worker_pool_restarts", 0),
                "worker_pool_retried_units": scope_metrics.get(
                    "worker_pool_retried_units", 0
                ),
                "worker_pool_last_error": scope_metrics.get("worker_pool_last_error", ""),
                "dedupe_files": dedupe_files,
                "units_before_dedupe": units_before_dedupe,
                "units_after_dedupe": len(units),
            }
        )
    if produced is not None:
        produced.clear()
        produced.update(produced_counts)
    if cache_stats is not None:
        cache_stats.clear()
        cache_stats.update(
            {
                "enabled": resolved_cache is not None,
                "cache_dir": str(resolved_cache) if resolved_cache else "",
                "hits": cache_hits,
                "misses": cache_misses,
                "tus": len(units),
                "hit_rate": round(cache_hits / len(units), 4) if units else 0.0,
            }
        )
    if len(errors) > _MAX_REPORTED_ERRORS:
        hidden = len(errors) - _MAX_REPORTED_ERRORS
        errors = errors[:_MAX_REPORTED_ERRORS] + [f"... {hidden} more error(s)"]
    summary.errors = tuple(errors)
    if header_declarations is not None:
        if previous_header_declarations is None:
            os.environ.pop("CA_HEADER_DECLARATIONS", None)
        else:
            os.environ["CA_HEADER_DECLARATIONS"] = previous_header_declarations
    return summary


def index_repository(
    repo_root: str,
    db_path: str,
    *,
    compdb: str | None = None,
    source: str = "auto",
    jobs: int = 8,
    max_tus: int | None = None,
    build_command: Sequence[str] | None = None,
    progress: ProgressCallback | None = None,
    produced: dict[str, int] | None = None,
    cache_dir: str | None = None,
    cache_stats: dict[str, object] | None = None,
    analyze_scope: str = "main",
    record_scope: str = "auto",
    transport_filter: str = "none",
    measure_transport: bool = False,
    metrics: dict[str, object] | None = None,
    dedupe_files: bool = False,
    header_declarations: bool | None = None,
) -> IndexSummary:
    """Resolve compile arguments and index a repository in one call.

    This convenience wrapper is used by the CLI and benchmarks; it keeps
    compile-argument discovery in ``ca.build`` (the owner of that policy).
    """

    from ca.build import (
        capture_build,
        load_compilation_database,
        synthesize_for_files,
    )

    repo = Path(repo_root).resolve()
    mode = source
    units: list[CompilationUnit] = []
    if mode == "auto":
        found = find_compilation_database(repo, compdb)
        mode = "compdb" if found is not None else "synthesized"
        if found is not None:
            units = list(load_compilation_database(str(found), str(repo)))
    elif mode == "compdb":
        found = find_compilation_database(repo, compdb)
        if found is None:
            raise FileNotFoundError(
                "compile_commands.json not found; pass --compdb <file|dir>"
            )
        units = list(load_compilation_database(str(found), str(repo)))
    elif mode == "synthesized":
        units = list(synthesize_for_files(repository_source_files(repo), str(repo)))
    elif mode == "capture":
        output = Path(compdb) if compdb else repo / ".tmp" / "ca-capture"
        units = list(
            capture_build(build_command or ["make"], str(output), str(repo), jobs=jobs)
        )
    else:
        raise ValueError(f"unknown compile-argument source: {mode!r}")
    del mode
    return run_index(
        str(repo),
        units,
        db_path,
        jobs=jobs,
        max_tus=max_tus,
        progress=progress,
        produced=produced,
        cache_dir=cache_dir,
        cache_stats=cache_stats,
        analyze_scope=analyze_scope,
        record_scope=record_scope,
        transport_filter=transport_filter,
        measure_transport=measure_transport,
        metrics=metrics,
    )


def find_compilation_database(repo: Path, hint: str | None) -> Path | None:
    """Locate compile_commands.json from an explicit hint or common build dirs."""

    if hint:
        candidate = Path(hint)
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        if candidate.is_dir():
            candidate = candidate / "compile_commands.json"
        return candidate if candidate.is_file() else None
    for relative in (
        "compile_commands.json",
        "build/compile_commands.json",
        "cmake-build-debug/compile_commands.json",
        "out/compile_commands.json",
    ):
        candidate = repo / relative
        if candidate.is_file():
            return candidate
    try:
        matches = sorted(repo.rglob("compile_commands.json"))
    except OSError:
        matches = []
    return matches[0] if matches else None


def repository_source_files(repo: Path, limit: int = 20000) -> list[str]:
    """Repository-relative C sources used for synthesized fallback arguments."""

    files: list[str] = []
    skip_dirs = {".git", ".tmp", "build", "out", "node_modules", ".venv"}
    for path in sorted(repo.rglob("*.c")):
        relative = path.relative_to(repo)
        if any(part in skip_dirs for part in relative.parts[:-1]):
            continue
        files.append(relative.as_posix())
        if len(files) >= limit:
            break
    return files
