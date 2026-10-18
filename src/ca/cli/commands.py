"""Implementations of the CA command line: index, query, stats, export, version.

Behaviour contract: successful data goes to stdout (tables by default, JSON with
``--json``), progress and diagnostics go to stderr. Exit codes are 0 success,
1 argument/runtime error, 2 no results.
"""

from __future__ import annotations

import json
import platform
import shlex
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Sequence

from ca.cli import format as fmt
from ca.core.schema import SCHEMA_VERSION, TOOL_VERSION

_QUERY_KINDS = (
    "functions",
    "callers",
    "callees",
    "branches",
    "assignments",
    "returns",
    "resources",
    "leaks",
    "detail",
)

_SYMBOL_REQUIRED = {
    "functions",
    "callers",
    "callees",
    "branches",
    "assignments",
    "returns",
    "resources",
    "detail",
}

_DEFAULT_LIMITS = {
    "functions": 50,
    "callers": 200,
    "callees": 200,
    "branches": 200,
    "assignments": 200,
    "returns": 200,
    "resources": 200,
    "leaks": 200,
}

_QUERY_COLUMNS: dict[str, tuple[str, ...]] = {
    "functions": (
        "name",
        "kind",
        "file",
        "line",
        "is_definition",
        "is_static",
        "linkage",
    ),
    "callers": (
        "caller_name",
        "caller_file",
        "caller_line",
        "callee_name",
        "call_file",
        "call_line",
        "is_resolved",
    ),
    "callees": (
        "callee_name",
        "callee_file",
        "callee_line",
        "call_file",
        "call_line",
        "is_resolved",
    ),
    "branches": (
        "file",
        "line",
        "kind",
        "condition",
        "var_names",
        "true_block",
        "false_block",
        "is_negated",
    ),
    "assignments": (
        "file",
        "line",
        "lhs",
        "rhs",
        "operator",
        "var_names",
        "is_decl_init",
    ),
    "returns": ("file", "line", "expr", "var_names", "is_implicit"),
    "resources": (
        "file",
        "line",
        "op",
        "resource_kind",
        "api_name",
        "target",
        "target_vars",
        "matcher",
    ),
    "leaks": (
        "function_name",
        "kind",
        "resource_kind",
        "severity",
        "message",
        "file",
        "line",
    ),
}


# --------------------------------------------------------------------------- #
# index
# --------------------------------------------------------------------------- #


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _remove_database_files(db_path: Path) -> None:
    """Replace a previous index instead of appending to it."""

    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(db_path) + suffix)
        try:
            if candidate.exists():
                candidate.unlink()
        except OSError:
            pass


class _ProgressReporter:
    """Throttled stderr progress; never emits more than ~2 lines per second."""

    def __init__(self) -> None:
        self.failed = 0
        self.last_emit = 0.0

    def __call__(self, done: int, total: int, result: Any) -> None:
        if not result.parse.parse_ok:
            self.failed += 1
        now = time.monotonic()
        if done != total and now - self.last_emit < 0.5:
            return
        self.last_emit = now
        print(
            f"[index] {done}/{total} TUs parsed ({self.failed} failed)",
            file=sys.stderr,
        )


def cmd_index(args: Any) -> int:
    from ca.build import (
        capture_build,
        load_compilation_database,
        synthesize_for_files,
    )
    from ca.pipeline import (
        find_compilation_database,
        repository_source_files,
        run_index,
    )

    from ca.cli import registry as registry_module

    try:
        registry_module.validate_index_options(args)
    except registry_module.RegistryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    repo = Path(args.repo).expanduser().resolve()
    if not repo.is_dir():
        print(f"error: repository not found: {repo}", file=sys.stderr)
        return 1
    if args.jobs < 1:
        print(f"error: --jobs must be >= 1, got {args.jobs}", file=sys.stderr)
        return 1
    db_path = Path(args.db).expanduser()

    layer = getattr(args, "layer", "deep")
    if layer == "base":
        if args.analyze_scope != "none" or args.record_scope != "all":
            print(
                "[index] note: --layer base forces --analyze-scope none"
                " --record-scope all (symbols/types/calls only)",
                file=sys.stderr,
            )
        args.analyze_scope = "none"
        args.record_scope = "all"
    cache_dir: str | None = None
    if args.cache_dir:
        cache_dir = args.cache_dir
    elif args.incremental or args.clear_cache:
        cache_dir = str(db_path.expanduser().resolve().parent / ".ca-cache")
    if args.clear_cache and cache_dir:
        shutil.rmtree(cache_dir, ignore_errors=True)
        print(f"[index] incremental: cleared cache dir {cache_dir}", file=sys.stderr)
    if cache_dir:
        print(
            f"[index] incremental: cache dir {cache_dir}"
            " (header changes require a full rebuild; see docs/usage)",
            file=sys.stderr,
        )

    source = args.source
    compdb: Path | None = None
    units: Sequence[Any] = ()
    if source in ("auto", "compdb"):
        compdb = find_compilation_database(repo, args.compdb)
        if compdb is None:
            if source == "compdb":
                print(
                    "error: compile_commands.json not found; pass --compdb <file|dir>",
                    file=sys.stderr,
                )
                return 1
            source = "synthesized"
        else:
            try:
                units = load_compilation_database(str(compdb), str(repo))
            except Exception as exc:
                print(f"error: cannot load {compdb}: {exc}", file=sys.stderr)
                return 1
            source = "compdb"  # effective source after auto-resolution
            print(f"[index] compile database: {compdb}", file=sys.stderr)

    if source == "synthesized":
        print(
            "[index] WARNING: no compile_commands.json found in"
            f" {repo} (searched the repo root and common build dirs).",
            file=sys.stderr,
        )
        print(
            "[index] WARNING: falling back to SYNTHESIZED compile arguments;"
            " flags, include paths and macros are GUESSES, not real build data."
            " Pass --compdb <file|dir> to use the real compile database.",
            file=sys.stderr,
        )
        print("[index] WARNING: CompilationUnit.source='synthesized'.", file=sys.stderr)
        files = repository_source_files(repo)
        if not files:
            print(f"error: no C sources found under {repo}", file=sys.stderr)
            return 1
        try:
            units = synthesize_for_files(files, str(repo))
        except Exception as exc:
            print(f"error: cannot synthesize compile arguments: {exc}", file=sys.stderr)
            return 1
    elif source == "capture":
        output = Path(args.compdb) if args.compdb else repo / ".tmp" / "ca-capture"
        command = shlex.split(args.build_cmd) if args.build_cmd else ["make"]
        print(
            f"[index] capturing build: {' '.join(command)} (output {output})",
            file=sys.stderr,
        )
        try:
            units = capture_build(command, str(output), str(repo), jobs=args.jobs)
        except Exception as exc:
            print(f"error: build capture failed: {exc}", file=sys.stderr)
            return 1

    units = list(units)
    if not units:
        print("error: no translation units resolved", file=sys.stderr)
        return 1
    inside = sum(1 for unit in units if _is_within(Path(unit.file), repo))
    if inside == 0:
        print(
            f"error: none of the {len(units)} compile-database sources are under"
            f" --repo {repo}; this would index nothing. Pass the repository root"
            " that actually contains the sources (and its compile_commands.json).",
            file=sys.stderr,
        )
        return 1
    print(
        f"[index] source={source} tus={len(units)} jobs={args.jobs}"
        + (f" max_tus={args.max_tus}" if args.max_tus is not None else ""),
        file=sys.stderr,
    )

    _remove_database_files(db_path)
    produced_counts: dict[str, int] = {}
    cache_stats: dict[str, object] = {}
    scope_metrics: dict[str, object] = {}
    try:
        summary = run_index(
            str(repo),
            units,
            str(db_path),
            jobs=args.jobs,
            max_tus=args.max_tus,
            progress=_ProgressReporter(),
            produced=produced_counts,
            cache_dir=cache_dir,
            cache_stats=cache_stats,
            analyze_scope=args.analyze_scope,
            record_scope=args.record_scope,
            transport_filter=args.transport_filter,
            measure_transport=args.measure_transport,
            metrics=scope_metrics,
            dedupe_files=(layer == "base"),
            header_declarations=getattr(args, "header_declarations", None),
        )
    except Exception as exc:
        print(f"error: indexing failed: {exc}", file=sys.stderr)
        return 1

    db_size = db_path.stat().st_size if db_path.is_file() else None
    fmt.print_summary(summary, db_size=db_size, produced=produced_counts)
    if getattr(args, "resolve_callbacks", False):
        from ca.cli import callbacks as callbacks_module

        pass_summary = callbacks_module.resolve_callbacks(
            str(db_path),
            repo=str(repo),
            compdb=str(compdb) if compdb else "",
            jobs=args.jobs,
        )
        print(
            f"[index] cross-TU callbacks: registrations={pass_summary.registrations}"
            f" candidate_edges={pass_summary.candidate_edges}"
            f" links={pass_summary.links}"
            f" elapsed={pass_summary.elapsed_seconds}s",
            file=sys.stderr,
        )
    from ca.cli import deep as deep_module

    deep_mode = (
        "base"
        if layer == "base" or args.analyze_scope == "none"
        else ("full" if args.analyze_scope == "all" else "mixed")
    )
    deep_module.set_index_mode(str(db_path), None, deep_mode)
    print(
        f"[index] layer={layer} deep_mode={deep_mode}"
        + (
            f" (unique files: {scope_metrics.get('units_after_dedupe')}/"
            f"{scope_metrics.get('units_before_dedupe')} TUs after file dedupe)"
            if layer == "base"
            else ""
        ),
        file=sys.stderr,
    )
    if scope_metrics.get("cache_disabled_reason"):
        print(f"[index] note: {scope_metrics['cache_disabled_reason']}", file=sys.stderr)
    scope = scope_metrics.get("analyze_scope", args.analyze_scope)
    print(
        f"[index] scope={scope} record_scope={scope_metrics.get('record_scope', args.record_scope)}: analyzed"
        f" {scope_metrics.get('definitions_analyzed', 0)}/"
        f"{scope_metrics.get('definitions_total', 0)} definitions"
        f" ({scope_metrics.get('definitions_suppressed', 0)} skipped),"
        f" transport={scope_metrics.get('transport_filter', 'none')}"
        + (
            f", pickle {float(scope_metrics.get('pickle_bytes_avg', 0.0)) / 1e6:.2f}MB/TU"
            f" -> {float(scope_metrics.get('transmit_bytes_avg', 0.0)) / 1e6:.2f}MB/TU"
            if args.measure_transport or args.transport_filter != "none"
            else ""
        )
        + (
            f", phase_a={scope_metrics.get('phase_a_seconds')}s"
            if scope == "all" and scope_metrics.get("phase_a_seconds")
            else ""
        ),
        file=sys.stderr,
    )
    if cache_stats.get("enabled"):
        print(
            f"[index] cache: hits={cache_stats.get('hits')}/{cache_stats.get('tus')}"
            f" ({float(cache_stats.get('hit_rate', 0.0)) * 100.0:.1f}%),"
            f" misses={cache_stats.get('misses')},"
            f" dir={cache_stats.get('cache_dir')}",
            file=sys.stderr,
        )
    if summary.tus_parsed == 0:
        print("error: no translation unit parsed successfully", file=sys.stderr)
        return 1
    if summary.functions == 0 and summary.calls == 0:
        print(
            "error: indexing produced no functions or calls; check that --repo"
            " contains the compile-database sources and that the parser accepts"
            " their arguments",
            file=sys.stderr,
        )
        return 1
    try:
        registered = registry_module.register_index(db_path, str(repo), args)
    except registry_module.RegistryError as exc:
        print(f"error: registry registration failed: {exc}", file=sys.stderr)
        return 1
    if registered:
        print(
            "[index] registry: workspace={workspace_id} project={project_id}"
            " variant={variant_id}".format(**registered),
            file=sys.stderr,
        )
    return 0


# --------------------------------------------------------------------------- #
# query
# --------------------------------------------------------------------------- #


def _query_rows(
    store: Any,
    kind: str,
    symbol: str | None,
    limit: int,
    offset: int = 0,
    federated: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    def paginated(method: Any, *args: Any) -> list[dict[str, Any]]:
        if federated:
            return list(method(*args, limit=limit, offset=offset))
        if offset:
            rows = list(method(*args, limit=limit + offset))
            return rows[offset:]
        return list(method(*args, limit=limit))

    if kind == "functions":
        return paginated(store.query_functions, symbol or ""), None
    if kind == "callers":
        return paginated(store.query_callers, symbol or ""), None
    if kind == "callees":
        return paginated(store.query_callees, symbol or ""), None
    if kind == "branches":
        return list(store.query_branches(symbol or "")), None
    if kind == "assignments":
        return list(store.query_assignments(symbol or "")), None
    if kind == "returns":
        return list(store.query_returns(symbol or "")), None
    if kind == "resources":
        return list(store.query_resource_ops(symbol or "")), None
    if kind == "leaks":
        rows = list(store.query_findings(kind="leak", limit=limit))
        if symbol:
            needle = symbol.lower()
            rows = [
                row
                for row in rows
                if needle in str(row.get("resource_kind", "")).lower()
            ]
        return rows, None
    if kind == "detail":
        return [], dict(store.query_function_detail(symbol or ""))
    raise ValueError(f"unknown query kind: {kind}")


def _print_detail(detail: dict[str, Any]) -> None:
    function = detail.get("function")
    if isinstance(function, dict) and function:
        print("function:")
        fmt.write_key_values(function)
    for section in ("branches", "assignments", "returns", "resource_ops", "findings"):
        rows = detail.get(section) or []
        print(f"\n{section} ({len(rows)}):")
        if rows:
            fmt.write_table(rows, columns=_QUERY_COLUMNS.get(section, None))


def _db_selector(args: Any) -> tuple[Any, str | None]:
    db = getattr(args, "db", None)
    return db, getattr(args, "db_dir", None)


def _variant_selector(args: Any) -> tuple[Any, str | None, bool]:
    """Return (db_values, db_dir, read_only) honoring ``--variant``."""

    from ca.cli import registry as registry_module

    target = registry_module.query_target(args)
    if target:
        return [target], None, True
    db_values, db_dir = _db_selector(args)
    return db_values, db_dir, False


def cmd_query(args: Any) -> int:
    from ca.cli import query_ux
    from ca.cli.dbsource import DatabaseSourceError, add_shard_columns, open_query_store

    if args.kind in _SYMBOL_REQUIRED and not args.symbol and not getattr(args, "symbols", None):
        print(f"error: query {args.kind} requires <symbol>", file=sys.stderr)
        return 1
    limit = args.limit if args.limit is not None else _DEFAULT_LIMITS.get(args.kind, 200)
    offset = int(getattr(args, "offset", 0) or 0)
    fields = getattr(args, "fields", None)
    compact = bool(getattr(args, "compact", False))
    symbols_arg = getattr(args, "symbols", None)
    if compact and args.kind != "detail":
        print("error: --compact only applies to query detail", file=sys.stderr)
        return 1
    if fields and args.kind != "detail":
        print("error: --fields only applies to query detail", file=sys.stderr)
        return 1
    try:
        db_values, db_dir, read_only = _variant_selector(args)
    except Exception as exc:
        from ca.cli.registry import RegistryError

        if isinstance(exc, RegistryError):
            print(f"error: {exc}", file=sys.stderr)
            return 1
        raise
    try:
        if symbols_arg:
            if symbols_arg == "-":
                text = sys.stdin.read()
            else:
                text = Path(symbols_arg).expanduser().read_text(encoding="utf-8")
            symbols = query_ux.parse_symbols(text)
            if not symbols:
                print("error: no symbols in input", file=sys.stderr)
                return 2
            with open_query_store(db_values, db_dir, read_only=read_only) as (store, source):
                batch = query_ux.resolve_batch(
                    store,
                    args.kind,
                    symbols,
                    limit=limit,
                    offset=offset,
                    fields=fields,
                    compact=compact,
                    federated=source.federated,
                )
        else:
            with open_query_store(db_values, db_dir, read_only=read_only) as (store, source):
                rows, detail = _query_rows(
                    store, args.kind, args.symbol, limit, offset, source.federated
                )
    except query_ux.QueryUxError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except DatabaseSourceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"error: query failed: {exc}", file=sys.stderr)
        return 1

    if symbols_arg:
        if args.json_output:
            fmt.write_json(batch)
        else:
            rows = [
                {
                    "symbol": item["symbol"],
                    "ok": item["ok"],
                    "summary": (
                        f"{len(item['data'])} rows"
                        if isinstance(item.get("data"), list)
                        else "detail"
                    )
                    if item["ok"]
                    else item.get("error", ""),
                }
                for item in batch["items"]
            ]
            fmt.write_table(rows)
        return 0 if any(item["ok"] for item in batch["items"]) else 2

    if args.kind == "detail":
        found = isinstance(detail, dict) and bool(detail.get("function"))
        if not found:
            if args.json_output:
                fmt.write_json({})
            print(f"no function matching {args.symbol!r}", file=sys.stderr)
            return 2
        try:
            if compact:
                detail = query_ux.compact_detail(detail)
            elif fields:
                detail = query_ux.project_detail(detail, query_ux.parse_fields(fields))
        except query_ux.QueryUxError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if args.json_output or compact or fields:
            fmt.write_json(detail)
        else:
            _print_detail(detail)
        return 0

    if not rows:
        if args.json_output:
            fmt.write_json([])
        print(f"no results for {args.kind} {args.symbol or ''}".rstrip(), file=sys.stderr)
        return 2
    if args.json_output:
        fmt.write_json(rows)
    else:
        columns, rows = add_shard_columns(rows, _QUERY_COLUMNS.get(args.kind))
        fmt.write_table(rows, columns=columns)
    return 0


def cmd_brief(args: Any) -> int:
    """Summary-first overview for one symbol (JSON or a small key/value view)."""

    from ca.cli import query_ux
    from ca.cli.dbsource import DatabaseSourceError, open_query_store

    db_values, db_dir = _db_selector(args)
    try:
        with open_query_store(db_values, db_dir) as (store, _source):
            brief = query_ux.resolve_brief(store, args.symbol)
    except DatabaseSourceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except query_ux.QueryUxError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"error: brief failed: {exc}", file=sys.stderr)
        return 1
    if not brief.get("found"):
        if args.json_output:
            fmt.write_json(brief)
        print(f"no function matching {args.symbol!r}", file=sys.stderr)
        return 2
    if args.json_output:
        fmt.write_json(brief)
        return 0
    function = brief["function"]
    fmt.write_key_values(
        {
            "symbol": function.get("name", args.symbol),
            "signature": function.get("signature", ""),
            "location": f"{function.get('file', '')}:{function.get('line', 0)}",
            "deep_ready": brief.get("deep_ready", False),
            "definition_missing": brief.get("definition_missing", False),
            "counts": brief.get("counts", {}),
            "key_conditions": [item.get("condition", "") for item in brief.get("key_conditions", [])],
            "callers": ", ".join(brief.get("callers", [])),
            "callees": ", ".join(brief.get("callees", [])),
        }
    )
    print(f"[brief] size={len(json.dumps(brief, ensure_ascii=False).encode())} bytes", file=sys.stderr)
    return 0


# --------------------------------------------------------------------------- #
# stats / export / version
# --------------------------------------------------------------------------- #


def cmd_resolve_callbacks(args: Any) -> int:
    """Post-process an existing database: resolve cross-TU callbacks."""

    from ca.cli import callbacks as callbacks_module

    db_path = Path(args.db).expanduser()
    if not db_path.is_file():
        print(f"error: database not found: {db_path}", file=sys.stderr)
        return 1
    repo = str(Path(args.repo).expanduser().resolve()) if args.repo else ""
    compdb = str(Path(args.compdb).expanduser().resolve()) if args.compdb else ""
    if args.compdb and not Path(compdb).is_file():
        print(f"error: compile database not found: {compdb}", file=sys.stderr)
        return 1
    summary = callbacks_module.resolve_callbacks(
        str(db_path),
        repo=repo,
        compdb=compdb,
        jobs=args.jobs,
        max_tus=args.max_tus,
        batch_size=getattr(args, "batch_size", 4),
        finalize=not args.no_finalize,
    )
    data = summary.to_dict()
    if args.json_output:
        fmt.write_json(data)
    else:
        fmt.write_key_values(
            {
                "units": f"{summary.units} ({summary.units_parsed} parsed,"
                f" {summary.units_failed} failed)",
                "registrations": summary.registrations,
                "registration_kinds": summary.registration_kinds,
                "indirect_call_sites": summary.indirect_call_sites,
                "candidate_edges": summary.candidate_edges,
                "links": summary.links,
                "written": data["written"],
                "elapsed_seconds": data["elapsed_seconds"],
            }
        )
        if summary.errors:
            print("errors:", file=sys.stderr)
            for error in summary.errors[:10]:
                print(f"  {error}", file=sys.stderr)
    print(
        f"[resolve-callbacks] units={summary.units_parsed}/{summary.units}"
        f" registrations={summary.registrations}"
        f" candidate_edges={summary.candidate_edges}"
        f" written={data['written']}"
        f" elapsed={data['elapsed_seconds']}s",
        file=sys.stderr,
    )
    if summary.errors and summary.units_parsed == 0:
        return 1
    return 0


def cmd_stats(args: Any) -> int:
    from ca.cli.dbsource import DatabaseSourceError, open_query_store

    db_values, db_dir = _db_selector(args)
    try:
        with open_query_store(db_values, db_dir) as (store, _source):
            stats = dict(store.stats())
            if hasattr(store, "callback_stats"):
                try:
                    stats["callbacks"] = dict(store.callback_stats())
                except Exception:
                    pass
    except DatabaseSourceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"error: stats failed: {exc}", file=sys.stderr)
        return 1

    if args.json_output:
        fmt.write_json(stats)
        return 0
    callbacks_stats = stats.pop("callbacks", None)
    tables = stats.pop("tables", None)
    shards = stats.pop("shards", None)
    skipped = stats.pop("skipped", None)
    fmt.write_key_values(stats)
    if isinstance(tables, dict) and tables:
        print("\ntables:")
        fmt.write_table({"table": name, "rows": count} for name, count in tables.items())
    if isinstance(shards, list) and shards:
        print("\nshards:")
        fmt.write_table(
            {
                "shard": item.get("shard", ""),
                "path": item.get("path", ""),
                "db_bytes": item.get("db_bytes", 0),
                "schema_version": item.get("schema_version", ""),
                "ok": item.get("ok", True),
            }
            for item in shards
        )
    if isinstance(skipped, list):
        for item in skipped:
            print(
                f"[stats] warning: skipped {item.get('path')}: {item.get('error')}",
                file=sys.stderr,
            )
    if isinstance(callbacks_stats, dict) and callbacks_stats:
        print("\ncallbacks:")
        fmt.write_key_values(callbacks_stats)
    return 0


def _record_iterator(store: Any, limit: int | None) -> Iterator[dict[str, Any]]:
    for name in ("iter_records", "dump_records", "export_records"):
        method = getattr(store, name, None)
        if callable(method):
            try:
                return iter(method(limit=limit))
            except TypeError:
                return iter(method())
    raise RuntimeError(
        "store does not expose iter_records(); export requires the read-only"
        " record iterator from ca.store.SqliteStore"
    )


def cmd_export(args: Any) -> int:
    db_path = Path(args.db).expanduser()
    if not db_path.is_file():
        print(f"error: database not found: {db_path}", file=sys.stderr)
        return 1
    out_path = Path(args.out).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    from ca.store import SqliteStore

    store = SqliteStore(str(db_path))
    count = 0
    kinds: dict[str, int] = {}
    try:
        store.open()
        iterator = _record_iterator(store, args.limit)
        with out_path.open("w", encoding="utf-8") as handle:
            for record in iterator:
                record = dict(record)
                record.setdefault("schema", SCHEMA_VERSION)
                kind = str(record.get("kind", ""))
                kinds[kind] = kinds.get(kind, 0) + 1
                handle.write(
                    json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
                    + "\n"
                )
                count += 1
    except Exception as exc:
        print(f"error: export failed: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            store.close()
        except Exception:
            pass
    summary = ", ".join(f"{name}={value}" for name, value in sorted(kinds.items()))
    print(
        f"[export] wrote {count} records to {out_path}"
        + (f" ({summary})" if summary else ""),
        file=sys.stderr,
    )
    return 0


def cmd_graph(args: Any) -> int:
    """Traverse the call graph rooted at a symbol (direct + candidate edges)."""

    from ca.cli import graph as graph_module
    from ca.cli.dbsource import DatabaseSourceError, open_query_store

    direction = "both" if args.both else ("reverse" if args.reverse else "forward")
    from ca.cli.registry import RegistryError

    try:
        db_values, db_dir, read_only = _variant_selector(args)
    except RegistryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        with open_query_store(db_values, db_dir, read_only=read_only) as (store, source):
            data = graph_module.build_call_graph(
                store,
                args.from_symbol,
                direction=direction,
                depth=args.depth,
                limit=args.limit,
            )
    except DatabaseSourceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except graph_module.GraphError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"error: graph query failed: {exc}", file=sys.stderr)
        return 1

    if args.format == "json":
        fmt.write_json(data)
    elif args.format == "dot":
        sys.stdout.write(graph_module.to_dot(data))
        sys.stdout.flush()
    else:
        nodes = graph_module.nodes_table_rows(data)
        edges = graph_module.edges_table_rows(data)
        if nodes:
            fmt.write_table(nodes)
        if edges:
            print()
            fmt.write_table(edges)
        print(
            f"[graph] root={data['root']} direction={data['direction']}"
            f" depth={data['depth']} nodes={data['stats']['nodes']}"
            f" edges={data['stats']['edges']} cycles={data['stats']['cycles']}"
            f" truncated={data['truncated']}"
            + (
                f" shards={len(source.paths)}"
                if getattr(source, "federated", False)
                else ""
            ),
            file=sys.stderr,
        )
    if not data["nodes"]:
        print(f"no graph nodes for {args.from_symbol!r}", file=sys.stderr)
        return 2
    return 0


@dataclass
class UpdateResult:
    """Result of one incremental update pass (shared by ``update``/``watch``)."""

    exit_code: int = 1
    ok: bool = False
    plan: Any | None = None
    summary: Any | None = None
    produced: dict[str, int] = field(default_factory=dict)
    cache_stats: dict[str, object] = field(default_factory=dict)
    db_write: dict[str, Any] = field(default_factory=dict)
    incremental_rows: dict[str, Any] = field(default_factory=dict)
    callback_refresh: dict[str, Any] = field(default_factory=dict)
    db_size: int | None = None
    elapsed_seconds: float = 0.0
    messages: list[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        summary = self.summary
        return {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "plan": self.plan.to_dict() if self.plan is not None else None,
            "summary": {
                "tus_total": getattr(summary, "tus_total", 0),
                "tus_parsed": getattr(summary, "tus_parsed", 0),
                "tus_failed": getattr(summary, "tus_failed", 0),
                "functions": getattr(summary, "functions", 0),
                "types": getattr(summary, "types", 0),
                "calls": getattr(summary, "calls", 0),
            }
            if summary is not None
            else None,
            "produced": dict(self.produced),
            "cache_stats": dict(self.cache_stats),
            "db_write": dict(self.db_write),
            "incremental_rows": dict(self.incremental_rows),
            "callback_refresh": dict(self.callback_refresh),
            "db_size": self.db_size,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "messages": list(self.messages),
            "error": self.error,
        }


def _resolve_header_dependencies(
    plan: Any, units: list[Any], db_path: Path, note: Any
) -> Any:
    """Turn a header-change plan into a dependency-aware plan when possible.

    Reads ``tu_includes`` from the existing index: a changed header invalidates
    only TUs whose transitive include set contains it.  Databases without include
    facts (old index / parser without include collection) keep the old full
    rebuild with an explicit warning.
    """

    from ca.cli.watcher import resolve_dependent_units
    from ca.store import SqliteStore

    if not getattr(plan, "header_change", False):
        return plan
    dependent_ids: list[str] = []
    has_data = False
    try:
        store = SqliteStore(str(db_path))
        store.open()
        try:
            dependent_ids = store.dependent_tu_ids(plan.changed_files)
            has_data = store.has_tu_includes()
        finally:
            store.close()
    except Exception as exc:  # missing DB or unreadable index: fall back
        note(f"[update] warning: cannot read include dependencies: {exc}")
    resolved = resolve_dependent_units(
        plan, units, dependent_ids, has_dependency_data=has_data
    )
    if resolved.dependency_aware:
        if resolved.affected_count:
            note(
                f"[update] include dependencies: {resolved.affected_count}/"
                f"{resolved.total_units} TUs include the changed header(s)"
            )
        else:
            note("[update] changed header(s) are not included by any indexed TU")
    else:
        note(
            "[update] WARNING: no include dependency information in the index"
            " (old database or parser without include collection); clearing the"
            " whole incremental cache and re-parsing all TUs"
        )
    return resolved


def _affected_units_from_plan(plan: Any, units: Sequence[Any]) -> list[Any]:
    """Map changed/dependent file paths from the watch plan to compilation units."""

    affected_paths = {
        str(Path(path).expanduser().resolve())
        for path in getattr(plan, "affected", ()) or ()
    }
    return [
        unit
        for unit in units
        if str(Path(unit.file).expanduser().resolve()) in affected_paths
    ]


def _contributing_units(
    db_path: Path, repo: Path, changed_files: Sequence[str], units: Sequence[Any]
) -> list[Any]:
    """Extra TUs owning rows for changed files (header-inline first-writer rule).

    A function definition in a header is stored once, attributed to the first TU
    that inserted it. When that header changes we must re-index the owning TU in
    addition to the TUs in the dependency plan, otherwise a stale header-inline
    row could survive an in-place update.
    """

    if not changed_files or not db_path.is_file():
        return []
    import sqlite3

    by_id = {str(unit.id): unit for unit in units}
    found: list[Any] = []
    seen: set[str] = set()
    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
        connection.row_factory = sqlite3.Row
    except sqlite3.Error:
        return []
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "functions" not in tables:
            return []
        changed: list[str] = []
        for item in changed_files:
            candidate = Path(item)
            if candidate.is_absolute():
                try:
                    candidate = candidate.resolve().relative_to(repo)
                except (OSError, ValueError):
                    continue
            changed.append(candidate.as_posix())
        if not changed:
            return []
        marks = ", ".join("?" for _ in changed)
        rows = connection.execute(
            "SELECT DISTINCT tu_id FROM functions WHERE file IN (" + marks + ")",
            changed,
        ).fetchall()
        for row in rows:
            tu_id = str(row[0] or "")
            if tu_id and tu_id not in seen and tu_id in by_id:
                found.append(by_id[tu_id])
                seen.add(tu_id)
    except sqlite3.Error:
        return found
    finally:
        connection.close()
    return found


def _callback_cleanup_refs(db_path: Path, tu_ids: Sequence[str]) -> tuple[list[str], list[str]]:
    """Registration/candidate ids that become stale when ``tu_ids`` are replaced."""

    if not tu_ids or not db_path.is_file():
        return [], []
    import sqlite3

    if not db_path.is_file():
        return [], []
    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
        connection.row_factory = sqlite3.Row
    except sqlite3.Error:
        return [], []
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "callback_registrations" not in tables:
            return [], []
        marks = ", ".join("?" for _ in tu_ids)
        registration_ids = [
            str(row[0])
            for row in connection.execute(
                "SELECT id FROM callback_registrations WHERE tu_id IN (" + marks + ")",
                list(tu_ids),
            )
        ]
        candidate_ids: list[str] = []
        if registration_ids and "callback_target_links" in tables:
            reg_marks = ", ".join("?" for _ in registration_ids)
            candidate_ids = [
                str(row[0])
                for row in connection.execute(
                    "SELECT DISTINCT call_target_id FROM callback_target_links"
                    " WHERE registration_id IN (" + reg_marks + ")",
                    registration_ids,
                )
            ]
        return list(dict.fromkeys(registration_ids)), list(dict.fromkeys(candidate_ids))
    except sqlite3.Error:
        return [], []
    finally:
        connection.close()


def _delete_ids(connection: Any, table: str, column: str, values: Sequence[str]) -> int:
    deleted = 0
    for start in range(0, len(values), 500):
        chunk = values[start : start + 500]
        marks = ", ".join("?" for _ in chunk)
        cursor = connection.execute(
            f"DELETE FROM {table} WHERE {column} IN ({marks})", list(chunk)
        )
        deleted += max(0, int(cursor.rowcount or 0))
    return deleted


def _callbacks_present(db_path: Path) -> tuple[bool, bool]:
    """Return ``(tables_present, any_registration_or_indirect_call)``."""

    if not db_path.is_file():
        return False, False
    import sqlite3

    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error:
        return False, False
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if not {"callback_registrations", "calls"} <= tables:
            return False, False
        registrations = int(
            connection.execute("SELECT COUNT(*) FROM callback_registrations").fetchone()[0]
        )
        indirect = int(
            connection.execute(
                "SELECT COUNT(*) FROM calls WHERE kind = 'indirect'"
            ).fetchone()[0]
        )
        return True, bool(registrations or indirect)
    except sqlite3.Error:
        return False, False
    finally:
        connection.close()


def _refresh_callbacks(
    db_path: Path, repo: Path, compdb: Path | None, jobs: int
) -> dict[str, Any]:
    """Recompute cross-TU callback candidates after an in-place update.

    The DB retains registrations for unaffected TUs but not the AST-level
    indirect-call-site records, so the deterministic way to keep candidate edges
    identical to a full index is to rerun the existing callback pass (with
    ``finalize=False``). The pass reparses TUs; it is only invoked when callbacks
    or indirect call sites exist. Callers may skip it when no callback data is
    relevant.
    """

    tables, relevant = _callbacks_present(db_path)
    if not tables:
        return {"mode": "skipped", "reason": "no callback tables"}
    if not relevant:
        return {"mode": "skipped", "reason": "no callback registrations or indirect calls"}
    from ca.cli.callbacks import resolve_callbacks

    started = time.perf_counter()
    try:
        summary = resolve_callbacks(
            str(db_path),
            repo=str(repo),
            compdb=str(compdb) if compdb else "",
            jobs=max(1, int(jobs)),
            finalize=False,
        )
    except Exception as exc:
        return {
            "mode": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        }
    data = {
        "mode": "rerun",
        "units": summary.units,
        "units_parsed": summary.units_parsed,
        "registrations": summary.registrations,
        "candidate_edges": summary.candidate_edges,
        "links": summary.links,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }
    if summary.errors:
        data["errors"] = list(summary.errors[:5])
    return data


def _summary_from_outcomes(
    outcomes: Sequence[Any], repo: Path, db_path: Path
) -> Any:
    """Build an ``IndexSummary`` for the affected-TU pass (not whole-DB totals)."""

    from ca.core.schema import IndexSummary

    summary = IndexSummary(
        repo_root=str(repo), db_path=str(db_path), tus_total=len(outcomes)
    )
    for outcome in outcomes:
        result = outcome.result
        parse = result.parse
        usable = bool(parse.functions or parse.calls or parse.types)
        parsed_ok = parse.parse_ok or usable
        if parsed_ok:
            summary.tus_parsed += 1
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
            summary.parse_seconds += parse.elapsed_ms / 1000.0
        else:
            summary.tus_failed += 1
    summary.total_seconds = round(summary.parse_seconds + summary.analyze_seconds, 3)
    summary.parse_seconds = round(summary.parse_seconds, 6)
    summary.analyze_seconds = round(summary.analyze_seconds, 6)
    return summary


def run_update(
    *,
    repo: str,
    db: str,
    compdb: str | None = None,
    changed_files: Sequence[str] = (),
    jobs: int = 8,
    dry_run: bool = False,
    layer: str = "deep",
    analyze_scope: str = "main",
    record_scope: str = "auto",
    cache_dir: str | None = None,
    header_declarations: bool | None = None,
    print_summary: bool = True,
    emit: Any | None = None,
) -> UpdateResult:
    """Refresh an existing index after a small set of files changed.

    Only affected TUs are parsed/analyzed; their rows are replaced in place with
    :meth:`SqliteStore.replace_units` (no whole-database rebuild). Header changes
    use the ``tu_includes`` dependency map and fall back to a full rebuild only
    for build files / old indexes without include facts. Shared by ``ca update``
    and ``ca watch``.
    """

    from ca.build import load_compilation_database
    from ca.cli.watcher import plan_changes
    from ca.pipeline import (
        collect_unit_outcomes,
        find_compilation_database,
        resolve_record_scope,
        run_index,
    )
    from ca.store import SqliteStore

    started = time.perf_counter()
    result = UpdateResult()

    def note(message: str) -> None:
        result.messages.append(message)
        if emit is not None:
            emit(message)
        else:
            print(message, file=sys.stderr)

    repo_path = Path(repo).expanduser().resolve()
    if not repo_path.is_dir():
        result.error = f"repository not found: {repo_path}"
        note(f"error: {result.error}")
        return result
    if jobs < 1:
        result.error = f"--jobs must be >= 1, got {jobs}"
        note(f"error: {result.error}")
        return result
    db_path = Path(db).expanduser()
    compdb_path = find_compilation_database(repo_path, compdb)
    if compdb_path is None:
        result.error = (
            "update requires a real compile_commands.json; pass --compdb"
            " <file|dir> (synthesized arguments are not supported here)"
        )
        note(f"error: {result.error}")
        return result
    try:
        units = list(load_compilation_database(str(compdb_path), str(repo_path)))
    except Exception as exc:
        result.error = f"cannot load {compdb_path}: {exc}"
        note(f"error: {result.error}")
        return result
    plan = plan_changes(units, changed_files, repo_path)
    result.plan = plan
    for warning in plan.errors:
        note(f"[update] warning: {warning}")
    plan = _resolve_header_dependencies(plan, units, db_path, note)
    result.plan = plan

    if dry_run:
        fmt.write_json(plan.to_dict())
        result.ok = True
        result.exit_code = 0
        result.elapsed_seconds = time.perf_counter() - started
        return result
    if plan.affected_count == 0:
        note("no translation units affected by the changed files")
        result.db_write = {"mode": "noop"}
        result.callback_refresh = {"mode": "skipped", "reason": "noop"}
        result.exit_code = 2
        result.elapsed_seconds = time.perf_counter() - started
        return result

    if layer == "base":
        analyze_scope = "none"
        record_scope = "all"
    effective_cache_dir = cache_dir or str(
        db_path.expanduser().resolve().parent / ".ca-cache"
    )
    resolved_record_scope = resolve_record_scope(record_scope, len(units))
    dependency_aware = bool(getattr(plan, "dependency_aware", False))
    if plan.header_change:
        if dependency_aware:
            affected = {
                str(Path(path).resolve()) for path in getattr(plan, "affected", ())
            }
            affected_units = [
                unit for unit in units if str(Path(unit.file).resolve()) in affected
            ]
            from ca.pipeline import invalidate_unit_cache

            removed = invalidate_unit_cache(
                effective_cache_dir,
                affected_units,
                analyze_scope,
                resolved_record_scope,
            )
            note(
                f"[update] dependency-aware: invalidated {removed} cache entries"
                f" for {len(affected_units)} dependent TUs"
            )
        else:
            shutil.rmtree(effective_cache_dir, ignore_errors=True)

    note(
        f"[update] affected {plan.affected_count}/{plan.total_units} TUs"
        f" (header_change={plan.header_change}), cache={effective_cache_dir}"
    )

    full_rebuild = (
        bool(getattr(plan, "fallback_full", False))
        or analyze_scope == "all"
        or not db_path.is_file()
    )
    if full_rebuild:
        _remove_database_files(db_path)
        produced: dict[str, int] = {}
        cache_stats: dict[str, object] = {}
        try:
            summary = run_index(
                str(repo_path),
                units,
                str(db_path),
                jobs=jobs,
                produced=produced,
                cache_dir=effective_cache_dir,
                cache_stats=cache_stats,
                analyze_scope=analyze_scope,
                record_scope=record_scope,
                header_declarations=header_declarations,
            )
        except Exception as exc:
            result.error = f"update failed: {exc}"
            note(f"error: {result.error}")
            result.elapsed_seconds = time.perf_counter() - started
            return result
        result.summary = summary
        result.produced = produced
        result.cache_stats = cache_stats
        result.db_write = {"mode": "full_rebuild"}
        result.incremental_rows = {}
        result.callback_refresh = {"mode": "skipped", "reason": "full rebuild"}
        result.db_size = db_path.stat().st_size if db_path.is_file() else None
        if print_summary:
            fmt.print_summary(summary, db_size=result.db_size, produced=produced)
        from ca.cli import deep as deep_module

        if layer == "base":
            deep_module.invalidate_deep(str(db_path), None, None)
            deep_module.set_index_mode(str(db_path), None, "base")
            note("[update] layer=base: deep status reset (lazy deep will recompute on demand)")
        note(
            f"[update] cache hits={cache_stats.get('hits')}/{cache_stats.get('tus')}"
            f" misses={cache_stats.get('misses')}; database rebuilt (full fallback)"
        )
        result.ok = summary.tus_parsed > 0
        result.exit_code = 0 if result.ok else 1
        result.elapsed_seconds = time.perf_counter() - started
        return result

    if not db_path.is_file():
        result.error = f"database not found: {db_path}"
        note(f"error: {result.error}")
        result.exit_code = 1
        result.elapsed_seconds = time.perf_counter() - started
        return result

    affected_units = _affected_units_from_plan(plan, units)
    seen_ids = {str(unit.id) for unit in affected_units}
    extra_units = [
        unit
        for unit in _contributing_units(db_path, repo_path, changed_files, units)
        if str(unit.id) not in seen_ids
    ]
    if extra_units:
        affected_units.extend(extra_units)
        if plan.header_change and dependency_aware:
            from ca.pipeline import invalidate_unit_cache

            invalidate_unit_cache(
                effective_cache_dir,
                extra_units,
                analyze_scope,
                resolved_record_scope,
            )
        note(
            f"[update] including {len(extra_units)} contributing TU(s) for"
            " header-inline ownership"
        )
    if not affected_units:
        result.error = "affected TU list is empty"
        note(f"error: {result.error}")
        result.exit_code = 1
        result.elapsed_seconds = time.perf_counter() - started
        return result

    affected_ids = [str(unit.id) for unit in affected_units]
    stale_registrations, stale_candidates = _callback_cleanup_refs(db_path, affected_ids)
    try:
        outcomes = collect_unit_outcomes(
            affected_units,
            jobs=jobs,
            cache_dir=effective_cache_dir,
            analyze_scope=analyze_scope,
            record_scope=resolved_record_scope,
            header_declarations=header_declarations,
        )
    except Exception as exc:
        result.error = f"incremental parse/analyze failed: {exc}"
        note(f"error: {result.error}")
        result.elapsed_seconds = time.perf_counter() - started
        return result
    if not outcomes:
        result.error = "incremental pass produced no outcomes"
        note(f"error: {result.error}")
        result.exit_code = 1
        result.elapsed_seconds = time.perf_counter() - started
        return result

    replace_result: dict[str, Any] = {}
    store = SqliteStore(str(db_path))
    store.open()
    try:
        store.begin()
        replace_result = store.replace_units(
            [(outcome.result.parse, outcome.result.analyses) for outcome in outcomes]
        )
        if stale_candidates:
            _delete_ids(store.connection, "call_targets", "id", stale_candidates)
        if stale_registrations or stale_candidates:
            _delete_ids(
                store.connection, "callback_target_links", "registration_id", stale_registrations
            )
            _delete_ids(
                store.connection, "callback_target_links", "call_target_id", stale_candidates
            )
        store.commit()
    except Exception as exc:
        store.rollback()
        result.error = f"incremental write failed: {exc}"
        note(f"error: {result.error}")
        result.elapsed_seconds = time.perf_counter() - started
        store.close()
        return result
    finally:
        store.close()

    removed_rows = sum(int(value or 0) for value in replace_result.get("removed", {}).values())
    added_rows = sum(int(value or 0) for value in replace_result.get("added", {}).values())
    result.incremental_rows = {
        "tu_ids": list(replace_result.get("tu_ids") or []),
        "removed": removed_rows,
        "added": added_rows,
        "fts_deleted": int(replace_result.get("fts_deleted", 0) or 0),
        "fts_inserted": int(replace_result.get("fts_inserted", 0) or 0),
        "tables": {
            "removed": dict(replace_result.get("removed") or {}),
            "added": dict(replace_result.get("added") or {}),
        },
    }
    result.db_write = {
        "mode": "incremental",
        "tu_ids": list(affected_ids),
        "tu_count": len(affected_ids),
        "removed_rows": removed_rows,
        "added_rows": added_rows,
        "fts_deleted": result.incremental_rows["fts_deleted"],
        "fts_inserted": result.incremental_rows["fts_inserted"],
    }
    result.summary = _summary_from_outcomes(outcomes, repo_path, db_path)
    result.produced = {
        "functions": sum(len(outcome.result.parse.functions) for outcome in outcomes),
        "types": sum(len(outcome.result.parse.types) for outcome in outcomes),
        "fields": sum(len(outcome.result.parse.fields) for outcome in outcomes),
        "calls": sum(len(outcome.result.parse.calls) for outcome in outcomes),
    }
    unaffected = max(0, len(units) - len(affected_units))
    result.cache_stats = {
        "enabled": True,
        "cache_dir": str(Path(effective_cache_dir).expanduser().resolve()),
        "hits": unaffected,
        "misses": len(affected_units),
        "tus": len(units),
        "hit_rate": round(unaffected / len(units), 4) if units else 0.0,
    }
    result.db_size = db_path.stat().st_size if db_path.is_file() else None
    if print_summary:
        fmt.print_summary(result.summary, db_size=result.db_size, produced=result.produced)
    from ca.cli import deep as deep_module

    if layer == "base":
        deep_module.invalidate_deep(str(db_path), None, affected_ids)
        deep_module.set_index_mode(str(db_path), None, "base")
        note("[update] layer=base: deep status reset for affected TUs")
    note(
        "[update] db_write: incremental"
        f" tus={len(affected_ids)} rows -{removed_rows}/+{added_rows}"
        f" fts -{result.incremental_rows['fts_deleted']}/+{result.incremental_rows['fts_inserted']}"
    )
    result.callback_refresh = _refresh_callbacks(
        db_path, repo_path, compdb_path, jobs
    )
    if result.callback_refresh.get("mode") == "rerun":
        note(
            "[update] callback_refresh: rerun"
            f" units={result.callback_refresh.get('units')}"
            f" registrations={result.callback_refresh.get('registrations')}"
            f" candidate_edges={result.callback_refresh.get('candidate_edges')}"
            f" links={result.callback_refresh.get('links')}"
        )
    elif result.callback_refresh.get("mode") == "error":
        note(
            "[update] warning: callback refresh failed:"
            f" {result.callback_refresh.get('error')}"
        )
    note(
        f"[update] cache hits={unaffected}/{len(units)} misses={len(affected_units)}"
        " (unaffected TUs left untouched)"
    )
    result.ok = result.summary.tus_parsed > 0
    result.exit_code = 0 if result.ok else 1
    result.elapsed_seconds = time.perf_counter() - started
    return result


def cmd_update(args: Any) -> int:
    """Refresh an existing index after a small set of files changed."""

    result = run_update(
        repo=args.repo,
        db=args.db,
        compdb=args.compdb,
        changed_files=args.changed_files,
        jobs=args.jobs,
        dry_run=args.dry_run,
        layer=getattr(args, "layer", "deep"),
        analyze_scope=getattr(args, "analyze_scope", "main"),
        record_scope=getattr(args, "record_scope", "auto"),
        cache_dir=args.cache_dir,
        header_declarations=getattr(args, "header_declarations", None),
    )
    return result.exit_code


def cmd_watch(args: Any) -> int:
    """Watch the repository and keep the database incrementally fresh."""

    from ca.cli import watch as watch_module

    config = watch_module.WatchConfig(
        repo=args.repo,
        db=args.db,
        compdb=args.compdb,
        interval=args.interval,
        debounce=args.debounce,
        jobs=args.jobs,
        layer=getattr(args, "layer", "deep"),
        analyze_scope=getattr(args, "analyze_scope", "main"),
        record_scope=getattr(args, "record_scope", "auto"),
        cache_dir=args.cache_dir,
        once=args.once,
        json_output=args.json_output,
        changed_files=list(args.changed_files or []),
        state_file=args.state_file,
        backend=getattr(args, "backend", "auto"),
    )
    return watch_module.run_watch(config)


def _flatten_symbols(values: Any) -> list[str]:
    flat: list[str] = []
    for value in values or []:
        flat.extend(part.strip() for part in str(value).split(",") if part.strip())
    return flat


def cmd_ensure_deep(args: Any) -> int:
    """Compute CFG/data-flow facts lazily for the requested function symbols."""

    from ca.cli import deep as deep_module

    db_path = Path(args.db).expanduser()
    repo = Path(args.repo).expanduser().resolve()
    compdb = Path(args.compdb).expanduser().resolve()
    if not db_path.is_file():
        print(f"error: database not found: {db_path}", file=sys.stderr)
        return 1
    if not repo.is_dir() or not compdb.is_file():
        print("error: --repo and --compdb are required for lazy deep analysis", file=sys.stderr)
        return 1
    symbols = _flatten_symbols(args.symbols)
    if not symbols:
        print("error: no symbols requested", file=sys.stderr)
        return 1
    result = deep_module.ensure_deep(
        str(db_path), str(repo), str(compdb), symbols, jobs=args.jobs
    )
    if args.json_output:
        fmt.write_json(result)
    else:
        rows = [
            {
                "symbol": item["symbol"],
                "status": item["status"],
                "deep_ready": item["deep_ready"],
                "facts": ", ".join(f"{k}={v}" for k, v in (item.get("facts") or {}).items()),
                "latency_ms": item["latency_ms"],
                "tu": item["tu"],
                "error": item["error"],
            }
            for item in result["results"]
        ]
        if rows:
            fmt.write_table(rows)
        metrics = result["metrics"]
        print(
            f"[ensure-deep] requested={result['requested']} hits={metrics['hits']}"
            f" computed={metrics['computed']} errors={metrics['errors']}"
            f" first={metrics['first_latency_ms']}ms hit={metrics['hit_latency_ms']}ms"
            f" parse={metrics['parse_ms']}ms analyze={metrics['analyze_ms']}ms",
            file=sys.stderr,
        )
    return 0 if any(item["deep_ready"] for item in result["results"]) else 2


def cmd_prewarm(args: Any) -> int:
    """Pre-analyze the hottest pending queried symbols (L2 queue)."""

    from ca.cli import deep as deep_module

    db_path = Path(args.db).expanduser()
    repo = Path(args.repo).expanduser().resolve()
    compdb = Path(args.compdb).expanduser().resolve()
    if not db_path.is_file():
        print(f"error: database not found: {db_path}", file=sys.stderr)
        return 1
    result = deep_module.prewarm(
        str(db_path),
        str(repo),
        str(compdb),
        limit=args.limit,
        jobs=args.jobs,
        symbols=_flatten_symbols(args.symbols) or None,
    )
    fmt.write_json(result)
    metrics = result["metrics"]
    print(
        f"[prewarm] requested={result['requested']} computed={metrics.get('computed')}"
        f" hits={metrics.get('hits')} errors={metrics.get('errors')}",
        file=sys.stderr,
    )
    return 0


def cmd_deep_status(args: Any) -> int:
    from ca.cli import deep as deep_module
    from ca.store import SqliteStore

    db_path = Path(args.db).expanduser()
    if not db_path.is_file():
        print(f"error: database not found: {db_path}", file=sys.stderr)
        return 1
    store = SqliteStore(str(db_path))
    store.open()
    try:
        symbols = _flatten_symbols(args.symbols)
        data: dict[str, Any] = deep_module.deep_stats(str(db_path), store)
        if symbols:
            data["symbols"] = deep_module.deep_status_for(
                str(db_path), store, symbols
            )
    finally:
        store.close()
    if args.json_output:
        fmt.write_json(data)
    else:
        fmt.write_key_values({k: v for k, v in data.items() if k != "symbols"})
        rows = data.get("symbols") or {}
        if rows:
            fmt.write_table(
                {"symbol": symbol, **entry} for symbol, entry in rows.items()
            )
    return 0


def cmd_mcp(args: Any) -> int:
    """Serve the MCP JSON-RPC interface over stdio or HTTP."""

    from ca.cli import mcp as mcp_module
    from ca.cli.dbsource import DatabaseSourceError, resolve_paths

    serve_url = getattr(args, "serve_url", None)
    if serve_url:
        return mcp_module.run_stdio_proxy(serve_url)
    db_values, db_dir = _db_selector(args)
    try:
        paths, errors = resolve_paths(db_values, db_dir)
    except DatabaseSourceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for item in errors:
        print(
            f"[mcp] warning: skipping {item.get('path')}: {item.get('error')}",
            file=sys.stderr,
        )
    if not paths:
        print("error: no readable databases selected (--db/--db-dir)", file=sys.stderr)
        return 1
    federated = bool(db_dir) or len(paths) > 1
    if args.transport == "http":
        return mcp_module.run_http(
            paths,
            host=args.host,
            port=args.port,
            repo=args.repo,
            compdb=args.compdb,
            federated=federated,
        )
    return mcp_module.run_stdio(
        paths, repo=args.repo, compdb=args.compdb, federated=federated
    )


def cmd_serve(args: Any) -> int:
    """Run the unified backend (query API + jobs/SSE + MCP endpoint)."""

    from ca.server import ServeConfig, serve

    config = ServeConfig(
        db_values=getattr(args, "db", None),
        db_dir=getattr(args, "db_dir", None),
        host=getattr(args, "host", "127.0.0.1"),
        port=int(getattr(args, "port", 8765)),
        state_db=getattr(args, "state_db", None),
        repo=str(getattr(args, "repo", "") or ""),
        compdb=str(getattr(args, "compdb", "") or ""),
    )
    return serve(config)


def cmd_registry(args: Any) -> int:
    """Dispatch workspace/project/variant registry commands."""

    from ca.cli import registry as registry_module

    return registry_module.cmd_registry(args)


def cmd_version(args: Any) -> int:
    data = {
        "tool": "ca-analyzer",
        "version": TOOL_VERSION,
        "schema_version": SCHEMA_VERSION,
        "python": platform.python_version(),
        "platform": platform.platform(),
    }
    if args.json_output:
        fmt.write_json(data)
    else:
        print(f"ca-analyzer {TOOL_VERSION} (schema {SCHEMA_VERSION})")
        print(f"python {data['python']} on {data['platform']}")
    return 0
