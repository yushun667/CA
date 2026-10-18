"""Cross-TU callback / function-pointer post-processing pass.

The index pipeline resolves callbacks only inside each translation unit. This
pass runs *after* indexing (or on an existing database) and resolves indirect
call sites against registration facts collected from every TU:

1. rebuild the ``CompilationUnit`` list from the database ``tus`` table
   (falling back to a compile database when a TU row is missing);
2. parse each TU and extract ``RegistrationFact`` / ``IndirectCallSite`` rows
   per TU in worker processes (only the small fact/site records cross the
   process boundary);
3. resolve sites against facts with qualifier/path-aware matching and persist
   ``callback_registrations``, candidate ``call_targets`` and
   ``callback_target_links``.

The pass is idempotent: registration facts use a natural-key unique index and
candidate edges/links use ``INSERT OR IGNORE`` with stable ids, so re-running on
the same database reports zero new rows. It never mutates existing records.
"""

from __future__ import annotations

import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Iterator, Sequence

_CALLBACK_PASS_VERSION = 1


@dataclass
class CallbackPassSummary:
    """Result metrics for one ``resolve_callbacks`` run."""

    units: int = 0
    units_parsed: int = 0
    units_failed: int = 0
    registrations: int = 0
    indirect_call_sites: int = 0
    candidate_edges: int = 0
    links: int = 0
    written_registrations: int = 0
    written_edges: int = 0
    written_links: int = 0
    existing_edges: int = 0
    registration_kinds: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    pass_version: int = _CALLBACK_PASS_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "pass_version": self.pass_version,
            "units": self.units,
            "units_parsed": self.units_parsed,
            "units_failed": self.units_failed,
            "registrations": self.registrations,
            "registration_kinds": self.registration_kinds,
            "indirect_call_sites": self.indirect_call_sites,
            "candidate_edges": self.candidate_edges,
            "links": self.links,
            "existing_edges": self.existing_edges,
            "written": {
                "registrations": self.written_registrations,
                "candidate_edges": self.written_edges,
                "links": self.written_links,
            },
            "errors": self.errors[:50],
            "elapsed_seconds": round(self.elapsed_seconds, 3),
        }


# --------------------------------------------------------------------------- #
# TU loading
# --------------------------------------------------------------------------- #


def load_units_from_store(store: Any, limit: int | None = None) -> list[Any]:
    """Rebuild CompilationUnits from persisted ``tus`` rows (stable order)."""

    from ca.core.schema import CompilationUnit

    units: list[Any] = []
    for row in store.iter_records(kinds=("compilation_unit",)):
        arguments = row.get("arguments") or ()
        units.append(
            CompilationUnit(
                id=str(row.get("id") or ""),
                file=str(row.get("file") or ""),
                directory=str(row.get("directory") or ""),
                repo_root=str(row.get("repo_root") or ""),
                arguments=tuple(str(argument) for argument in arguments),
                language=str(row.get("language") or "c"),
                source=str(row.get("source") or "compile_commands"),
                driver=str(row.get("driver") or ""),
            )
        )
        if limit is not None and len(units) >= limit:
            break
    units.sort(key=lambda unit: unit.id)
    return units


def load_units_from_compdb(repo: str, compdb: str, limit: int | None = None) -> list[Any]:
    from ca.build import load_compilation_database

    units = list(load_compilation_database(str(compdb), str(repo)))
    units.sort(key=lambda unit: unit.id)
    if limit is not None:
        units = units[:limit]
    return units


# --------------------------------------------------------------------------- #
# Per-TU extraction (worker)
# --------------------------------------------------------------------------- #


def extract_unit_callbacks(unit: Any) -> tuple[list[Any], list[Any], dict[str, Any], str]:
    """Worker: parse one TU and return (facts, call_sites, signatures, error).

    Signatures are built from the same parsed AST as the call sites, so the
    resolver's signature filter compares identical type spellings (DB rows may
    store typedef spellings such as ``uint64`` where the AST spells
    ``unsigned long``).
    """

    from ca.analyze.registrations import (
        extract_registrations,
        extract_target_signatures,
    )
    from ca.parse import ParseOptions, parse_compilation_unit

    try:
        parsed = parse_compilation_unit(unit, ParseOptions(record_scope="main_file"))
    except Exception as exc:  # pragma: no cover - environment dependent
        return [], [], {}, f"parse failed: {type(exc).__name__}: {exc}"
    try:
        facts, sites = extract_registrations(parsed)
        target_ids = {
            str(fact.target_function_id)
            for fact in facts
            if getattr(fact, "target_function_id", "")
        }
        signatures = extract_target_signatures(parsed, target_ids)
    except Exception as exc:  # pragma: no cover - defensive
        return [], [], {}, f"extract failed: {type(exc).__name__}: {exc}"
    return facts, sites, signatures, ""


def _iter_extracted(
    units: Sequence[Any],
    jobs: int,
    batch_size: int,
    progress: Any | None = None,
) -> Iterator[tuple[list[Any], list[Any], str, Any]]:
    """Yield (facts, sites, error, unit) in deterministic unit order."""

    workers = max(1, min(int(jobs), len(units))) if units else 1
    chunk_size = max(1, int(batch_size))
    chunks = [units[index : index + chunk_size] for index in range(0, len(units), chunk_size)]
    if workers <= 1 or len(chunks) <= 1:
        for chunk in chunks:
            for unit in chunk:
                facts, sites, signatures, error = extract_unit_callbacks(unit)
                yield facts, sites, signatures, error, unit
        return
    # Process in waves so workers do not accumulate AST memory across the whole
    # database: one fresh pool per wave (same strategy as the index pipeline).
    wave_chunks = max(1, workers * 2)
    for wave_start in range(0, len(chunks), wave_chunks):
        wave = chunks[wave_start : wave_start + wave_chunks]
        with ProcessPoolExecutor(max_workers=min(workers, len(wave))) as pool:
            futures = [pool.submit(extract_unit_callbacks, unit) for chunk in wave for unit in chunk]
            for future, unit in zip(futures, [unit for chunk in wave for unit in chunk]):
                try:
                    facts, sites, signatures, error = future.result()
                except Exception as exc:
                    facts, sites, signatures, error = (
                        [], [], {}, f"worker failed: {type(exc).__name__}: {exc}"
                    )
                yield facts, sites, signatures, error, unit
        if progress is not None:
            progress()


# --------------------------------------------------------------------------- #
# Resolution / persistence
# --------------------------------------------------------------------------- #


def _count_by(items: Sequence[Any], attribute: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        key = str(getattr(item, attribute, "") or "")
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _load_function_rows(store: Any, target_ids: set[str]) -> dict[str, dict[str, Any]]:
    """Load persisted function rows for the registration target ids."""

    rows: dict[str, dict[str, Any]] = {}
    if not target_ids:
        return rows
    for row in store.iter_records(kinds=("function",)):
        function_id = str(row.get("id") or "")
        if function_id not in target_ids or function_id in rows:
            continue
        rows[function_id] = row
        if len(rows) == len(target_ids):
            break
    return rows


def _functions_from_rows(rows: dict[str, dict[str, Any]]) -> dict[str, Any]:
    from types import SimpleNamespace

    return {
        function_id: SimpleNamespace(
            id=function_id,
            name=str(row.get("name") or ""),
            qualified_name=str(row.get("qualified_name") or ""),
            usr=str(row.get("usr") or ""),
            kind=str(row.get("kind") or ""),
            return_type=str(row.get("return_type") or ""),
            signature=str(row.get("signature") or ""),
            tu_id=str(row.get("tu_id") or ""),
        )
        for function_id, row in rows.items()
    }


def _signatures_from_rows(rows: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Best-effort ``_Signature`` map from persisted function rows.

    The persisted rows carry the return type, parameter type spellings and the
    variadic flag, which is exactly what the resolver's signature filter needs.
    If the analyze internals change, the pass degrades to no signature filter
    instead of failing.
    """

    try:
        from ca.analyze.callbacks import _Signature  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover - defensive
        return {}
    signatures: dict[str, Any] = {}
    for function_id, row in rows.items():
        params = row.get("params") or []
        spellings = tuple(
            str(param.get("type_spelling") or "")
            if isinstance(param, dict)
            else str(getattr(param, "type_spelling", "") or "")
            for param in params
        )
        signatures[function_id] = _Signature(
            return_type=str(row.get("return_type") or ""),
            params=spellings,
            variadic=bool(row.get("is_variadic")),
        )
    return signatures


def _existing_edges(store: Any) -> set[tuple[str, str]]:
    edges: set[tuple[str, str]] = set()
    for row in store.iter_records(kinds=("call_target",)):
        call_id = str(row.get("call_id") or "")
        callee_id = str(row.get("callee_id") or "")
        if call_id and callee_id:
            edges.add((call_id, callee_id))
    return edges


def resolve_callbacks(
    db_path: str,
    *,
    repo: str = "",
    compdb: str = "",
    jobs: int = 1,
    max_tus: int | None = None,
    batch_size: int = 4,
    finalize: bool = True,
    progress: Any | None = None,
) -> CallbackPassSummary:
    """Run the cross-TU callback pass against an existing database."""

    from ca.analyze.registrations import resolve_cross_tu_targets
    from ca.store import SqliteStore

    started = time.perf_counter()
    summary = CallbackPassSummary()
    store = SqliteStore(str(db_path))
    store.open()
    try:
        units = load_units_from_store(store, limit=max_tus)
        if not units and compdb:
            units = load_units_from_compdb(repo, compdb, limit=max_tus)
        summary.units = len(units)
        if not units:
            summary.errors.append(
                "no translation units in database and no usable compile database"
            )
            return summary

        registrations: list[Any] = []
        call_sites: list[Any] = []
        signatures: dict[str, Any] = {}
        for facts, sites, unit_signatures, error, unit in _iter_extracted(
            units, jobs, batch_size, progress=progress
        ):
            if error:
                summary.units_failed += 1
                if len(summary.errors) < 50:
                    summary.errors.append(f"{unit.file}: {error}")
                continue
            summary.units_parsed += 1
            registrations.extend(facts)
            call_sites.extend(sites)
            for function_id, signature in unit_signatures.items():
                signatures.setdefault(function_id, signature)

        summary.registrations = len(registrations)
        summary.indirect_call_sites = len(call_sites)
        summary.registration_kinds = _count_by(registrations, "registration_kind")

        if not registrations or not call_sites:
            return summary

        target_ids = {
            str(fact.target_function_id)
            for fact in registrations
            if getattr(fact, "target_function_id", "")
        }
        function_rows = _load_function_rows(store, target_ids)
        functions = _functions_from_rows(function_rows)
        for function_id, signature in _signatures_from_rows(function_rows).items():
            signatures.setdefault(function_id, signature)
        existing = _existing_edges(store)
        summary.existing_edges = len(existing)
        call_targets, links = resolve_cross_tu_targets(
            call_sites,
            registrations,
            functions,
            signatures=signatures,
            existing_edges=existing,
        )
        summary.candidate_edges = len(call_targets)
        summary.links = len(links)

        if call_targets or registrations or links:
            store.begin()
            try:
                summary.written_registrations = store.add_callback_registrations(registrations)
                summary.written_edges = int(store.add_call_targets(call_targets))
                summary.written_links = store.link_callback_targets(links)
                store.commit()
            except Exception:
                # Closing the store rolls the open transaction back.
                raise
            if finalize:
                store.finalize()
    finally:
        store.close()
        summary.elapsed_seconds = time.perf_counter() - started
    return summary
