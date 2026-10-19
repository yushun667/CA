"""``ca`` command line entry point: ``main(argv=None) -> int``.

Subcommands: ``index``, ``update``, ``query``, ``graph``, ``mcp``, ``ui``, ``desktop``,
``stats``, ``export``, ``version``.
Exit codes: 0 success, 1 argument/runtime error, 2 no results.
"""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from ca.cli import commands

_DESCRIPTION = """\
CA indexes a real build (compile_commands.json -> Clang AST -> CFG/data-flow)
into a SQLite database and answers structural questions about the code.

Data is written to stdout, progress and metrics to stderr.
Exit codes: 0 success, 1 error, 2 no results.
"""

_EPILOG = """\
examples:
  .venv/bin/ca index --repo .tmp/corpus/xv6-riscv --db ca.db
  .venv/bin/ca index --repo .tmp/corpus/xv6-riscv --db ca.db --incremental
  .venv/bin/ca query callers kfree --db ca.db
  .venv/bin/ca query branches syscall --db ca.db --json
  .venv/bin/ca stats --db ca.db
  .venv/bin/ca export --db ca.db --out ca.jsonl

  .venv/bin/ca graph --from syscall --depth 2 --db ca.db --format dot
  .venv/bin/ca update --repo .tmp/corpus/xv6-riscv --db ca.db \
      --changed-files kernel/syscall.c
  .venv/bin/ca mcp --db ca.db                      # stdio JSON-RPC
  .venv/bin/ca mcp --db ca.db --transport http --port 8765
  .venv/bin/ca ui --db ca.db                       # read-only web UI
  .venv/bin/ca ui --db-dir shards/ --port 8766
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ca",
        description=_DESCRIPTION,
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    index = sub.add_parser(
        "index",
        help="index a repository into a SQLite database",
        description=(
            "Resolve real compile arguments, parse every TU, run CFG/data-flow"
            " analysis and write all records to SQLite."
        ),
    )
    index.add_argument("--repo", required=True, help="repository root to index")
    index.add_argument("--db", required=True, help="output SQLite database path")
    index.add_argument(
        "--compdb",
        default=None,
        help="compile_commands.json file or directory containing one",
    )
    index.add_argument("--jobs", type=int, default=8, help="worker processes (default: 8)")
    index.add_argument(
        "--max-tus", type=int, default=None, help="index at most N translation units"
    )
    index.add_argument(
        "--source",
        choices=("auto", "compdb", "capture", "synthesized"),
        default="auto",
        help=(
            "compile-argument source; auto uses compile_commands.json when"
            " present and falls back to synthesized arguments"
        ),
    )
    index.add_argument(
        "--build-cmd",
        default=None,
        help="capture mode only: build command to run, e.g. 'make -j8'",
    )
    index.add_argument(
        "--incremental",
        action="store_true",
        help=(
            "reuse cached parse/analysis results for unchanged TUs"
            " (cache in <db-dir>/.ca-cache; header changes need a full rebuild)"
        ),
    )
    index.add_argument(
        "--cache-dir",
        default=None,
        help="incremental cache directory (implies --incremental)",
    )
    index.add_argument(
        "--clear-cache",
        action="store_true",
        help="delete the incremental cache before the run (implies --incremental)",
    )
    index.add_argument(
        "--analyze-scope",
        choices=("main", "all", "none"),
        default="main",
        help=(
            "deep-analysis scope: main=only definitions in the TU's own source"
            " (default, scales to large corpora), all=every definition with"
            " cross-worker dedupe (extra parse pass), none=symbols/calls only"
        ),
    )
    index.add_argument(
        "--header-declarations",
        action="store_true",
        dest="header_declarations",
        default=None,
        help="emit header function declarations too (default: on for main-file scope)",
    )
    index.add_argument(
        "--no-header-declarations",
        action="store_false",
        dest="header_declarations",
        help="disable header function declarations for main-file scope",
    )
    index.add_argument(
        "--resolve-callbacks",
        action="store_true",
        help="after indexing, run the cross-TU callback post-processing pass",
    )
    index.add_argument(
        "--layer",
        choices=("deep", "base"),
        default="deep",
        help=(
            "index layer: deep=parse+analyze (default); base=L0 fast pass with"
            " symbols/types/calls only and lazy deep analysis"
        ),
    )
    index.add_argument(
        "--record-scope",
        choices=("auto", "all", "main_file"),
        default="auto",
        help=(
            "extraction scope: auto=main_file above 200 TUs else all (default),"
            " all=every header/main record, main_file=only the TU's own source"
            " definitions/calls (large-corpus default, much smaller payload)"
        ),
    )
    index.add_argument(
        "--transport-filter",
        choices=("none", "declarations"),
        default="none",
        help=(
            "drop declaration records from the cross-parse payload (lossy,"
            " saves transfer/serialization; default: keep everything)"
        ),
    )
    index.add_argument(
        "--measure-transport",
        action="store_true",
        help="measure ParseResult pickle bytes per TU and report them on stderr",
    )
    index.add_argument(
        "--workspace-path", default=None,
        help="registry: workspace root to register after a successful index",
    )
    index.add_argument("--workspace-name", default=None, help="registry: workspace display name")
    index.add_argument("--project-path", default=None, help="registry: project path to register")
    index.add_argument("--project-name", default=None, help="registry: project display name")
    index.add_argument("--project-repo", default=None, help="registry: project repo name")
    index.add_argument("--project-product", default=None, help="registry: project product")
    index.add_argument("--project-board", default=None, help="registry: project board")
    index.add_argument("--project-version", default=None, help="registry: project version")
    index.add_argument("--variant-name", default=None, help="registry: variant name to register")
    index.add_argument("--variant-product", default=None, help="registry: variant product")
    index.add_argument("--variant-board", default=None, help="registry: variant board")
    index.add_argument("--variant-version", default=None, help="registry: variant version")
    index.add_argument(
        "--variant-db", default=None,
        help="registry: variant index DB (default: this --db path)",
    )
    index.set_defaults(func=commands.cmd_index)

    query = sub.add_parser(
        "query",
        help="query indexed facts (data on stdout, --json for scripting)",
        description="Query functions, calls, branches, assignments, returns and resources.",
    )
    query.add_argument(
        "kind",
        choices=commands._QUERY_KINDS,
        help="fact kind to query",
    )
    query.add_argument(
        "symbol",
        nargs="?",
        default=None,
        help="function/symbol/pattern; optional for functions and leaks",
    )
    query.add_argument(
        "--db", action="append", default=None,
        help="SQLite database path (repeatable for federated query)",
    )
    query.add_argument(
        "--db-dir", default=None,
        help="directory of shard *.db files (federated query)",
    )
    query.add_argument(
        "--limit", type=int, default=None, help="maximum number of rows (default: 50-200)"
    )
    query.add_argument(
        "--offset", type=int, default=0, help="rows to skip after merge (default: 0)"
    )
    query.add_argument(
        "--fields",
        default=None,
        help="detail projection: comma-separated dot paths (e.g. function.name,deep_ready)",
    )
    query.add_argument(
        "--compact",
        action="store_true",
        help="detail compact preset (identity/counts/findings, <=3-5 KB)",
    )
    query.add_argument(
        "--symbols",
        default=None,
        metavar="FILE|-",
        help="batch mode: query many symbols from a file or stdin ('-'), one per line",
    )
    query.add_argument(
        "--variant", default=None,
        help="query a registered variant's db_path (requires one --db registry database)",
    )
    query.add_argument("--json", action="store_true", dest="json_output", help="emit JSON")
    query.set_defaults(func=commands.cmd_query)

    stats = sub.add_parser(
        "stats", help="show table row counts and database size"
    )
    stats.add_argument(
        "--db", action="append", default=None,
        help="SQLite database path (repeatable for federated stats)",
    )
    stats.add_argument("--db-dir", default=None,
                       help="directory of shard *.db files (federated stats)")
    stats.add_argument("--json", action="store_true", dest="json_output", help="emit JSON")
    stats.set_defaults(func=commands.cmd_stats)

    export = sub.add_parser(
        "export", help="export every record as JSON Lines"
    )
    export.add_argument("--db", required=True, help="SQLite database path")
    export.add_argument("--out", required=True, help="output .jsonl file")
    export.add_argument(
        "--limit", type=int, default=None, help="export at most N records"
    )
    export.set_defaults(func=commands.cmd_export)

    graph = sub.add_parser(
        "graph",
        help="traverse the call graph from a symbol",
        description=(
            "Recursive call-graph traversal over indexed calls (direct) and"
            " call_targets candidates, with depth/cycle/hard limits."
        ),
    )
    graph.add_argument("--from", dest="from_symbol", required=True,
                       help="root function symbol (name, id or unique substring)")
    graph.add_argument("--depth", type=int, default=1, help="traversal depth (default: 1, max 12)")
    direction = graph.add_mutually_exclusive_group()
    direction.add_argument("--reverse", action="store_true",
                           help="follow callers instead of callees")
    direction.add_argument("--both", action="store_true",
                           help="follow both callees and callers")
    graph.add_argument("--format", choices=("table", "json", "dot"), default="table",
                       help="output format (default: table)")
    graph.add_argument("--limit", type=int, default=500,
                       help="maximum number of graph nodes (default: 500)")
    graph.add_argument(
        "--db", action="append", default=None,
        help="SQLite database path (repeatable for federated graph)",
    )
    graph.add_argument("--db-dir", default=None,
                       help="directory of shard *.db files (federated graph)")
    graph.add_argument(
        "--variant", default=None,
        help="graph a registered variant's db_path (requires one --db registry database)",
    )
    graph.set_defaults(func=commands.cmd_graph)

    update = sub.add_parser(
        "update",
        help="refresh an existing index after source files changed",
        description=(
            "Re-parse/analyze only the translation units affected by changed"
            " files (incremental cache) and rebuild the database from it."
        ),
    )
    update.add_argument("--repo", required=True, help="repository root")
    update.add_argument("--db", required=True, help="SQLite database to refresh")
    update.add_argument("--compdb", default=None,
                        help="compile_commands.json file or directory containing one")
    update.add_argument("--changed-files", nargs="+", required=True,
                        help="changed source/header/build files (repo-relative or absolute)")
    update.add_argument("--jobs", type=int, default=8, help="worker processes (default: 8)")
    update.add_argument("--cache-dir", default=None,
                        help="incremental cache directory (default: <db-dir>/.ca-cache)")
    update.add_argument("--dry-run", action="store_true",
                        help="print the affected-unit plan as JSON and exit")
    update.add_argument("--analyze-scope", choices=("main", "all", "none"),
                        default="main", help="deep-analysis scope (default: main)")
    update.add_argument("--record-scope", choices=("auto", "all", "main_file"),
                        default="auto", help="extraction scope (default: auto)")
    update.add_argument("--layer", choices=("deep", "base"), default="deep",
                        help="layer to rebuild: base keeps lazy deep analysis")
    update.add_argument(
        "--header-declarations", action="store_true", dest="header_declarations",
        default=None, help="emit header function declarations (default: auto)",
    )
    update.add_argument(
        "--no-header-declarations", action="store_false", dest="header_declarations",
        help="disable header function declarations for main-file scope",
    )
    update.set_defaults(func=commands.cmd_update)

    mcp = sub.add_parser(
        "mcp",
        help="serve the MCP query interface (JSON-RPC)",
        description=(
            "Model Context Protocol server exposing CA queries to AI agents."
            " stdio is the default transport (works over SSH pipes); HTTP serves"
            " POST /mcp plus GET /health."
        ),
    )
    mcp.add_argument(
        "--db", action="append", default=None,
        help="SQLite database path (repeatable for a federated server)",
    )
    mcp.add_argument("--db-dir", default=None,
                     help="directory of shard *.db files (federated MCP server)")
    mcp.add_argument("--transport", choices=("stdio", "http"), default="stdio",
                     help="transport (default: stdio)")
    mcp.add_argument(
        "--serve-url", default=None,
        help="proxy stdio MCP to a running `ca serve` backend (URL or host:port)",
    )
    mcp.add_argument("--repo", default=None,
                     help="repository root (required for ensure_deep/prewarm tools)")
    mcp.add_argument("--compdb", default=None,
                     help="compile_commands.json path (required for deep tools)")
    mcp.add_argument("--host", default="127.0.0.1", help="HTTP bind host (default: 127.0.0.1)")
    mcp.add_argument("--port", type=int, default=8765, help="HTTP port (default: 8765)")
    mcp.set_defaults(func=commands.cmd_mcp)

    brief = sub.add_parser(
        "brief",
        help="summary-first overview for one symbol (small, agent-friendly)",
        description=(
            "Return a compact summary for a symbol: identity, deep_ready, direct"
            " caller/callee counts and samples, fact counts, key conditions and"
            " findings. Intended as the first call before query detail."
        ),
    )
    brief.add_argument("symbol", help="function name, id or unique substring")
    brief.add_argument(
        "--db", action="append", default=None,
        help="SQLite database path (repeatable for federated brief)",
    )
    brief.add_argument("--db-dir", default=None,
                       help="directory of shard *.db files (federated brief)")
    brief.add_argument("--json", action="store_true", dest="json_output")
    brief.set_defaults(func=commands.cmd_brief)

    ensure_deep = sub.add_parser(
        "ensure-deep",
        help="lazily compute deep facts (CFG/data-flow) for function symbols",
        description=(
            "L1 lazy deep analysis: parse the owning TUs, analyze only the"
            " requested functions, write facts and mark them deep-ready."
        ),
    )
    ensure_deep.add_argument("--db", required=True)
    ensure_deep.add_argument("--repo", required=True)
    ensure_deep.add_argument("--compdb", required=True)
    ensure_deep.add_argument("--symbols", nargs="+", required=True,
                             help="function names/ids (comma or space separated)")
    ensure_deep.add_argument("--jobs", type=int, default=1)
    ensure_deep.add_argument("--json", action="store_true", dest="json_output")
    ensure_deep.set_defaults(func=commands.cmd_ensure_deep)

    prewarm = sub.add_parser(
        "prewarm",
        help="pre-analyze the hottest pending queried symbols (L2 queue)",
    )
    prewarm.add_argument("--db", required=True)
    prewarm.add_argument("--repo", required=True)
    prewarm.add_argument("--compdb", required=True)
    prewarm.add_argument("--limit", type=int, default=20)
    prewarm.add_argument("--jobs", type=int, default=1)
    prewarm.add_argument("--symbols", nargs="*", default=None)
    prewarm.set_defaults(func=commands.cmd_prewarm)

    resolve_callbacks = sub.add_parser(
        "resolve-callbacks",
        help="post-process an existing database: resolve cross-TU callbacks",
        description=(
            "Parse the database's translation units, extract cross-TU callback"
            " registration facts and indirect call sites, then write candidate"
            " call_targets. Idempotent; safe to re-run on the same database."
        ),
    )
    resolve_callbacks.add_argument("--db", required=True)
    resolve_callbacks.add_argument("--repo", default=None,
                                   help="repository root (optional; DB tus rows carry it)")
    resolve_callbacks.add_argument("--compdb", default=None,
                                   help="compile_commands.json fallback when tus rows are missing")
    resolve_callbacks.add_argument("--jobs", type=int, default=1)
    resolve_callbacks.add_argument("--max-tus", type=int, default=None)
    resolve_callbacks.add_argument("--batch-size", type=int, default=4)
    resolve_callbacks.add_argument("--no-finalize", action="store_true")
    resolve_callbacks.add_argument("--json", action="store_true", dest="json_output")
    resolve_callbacks.set_defaults(func=commands.cmd_resolve_callbacks)

    deep_status = sub.add_parser(
        "deep-status", help="report deep_ready coverage and per-symbol status"
    )
    deep_status.add_argument("--db", required=True)
    deep_status.add_argument("--symbols", nargs="*", default=None)
    deep_status.add_argument("--json", action="store_true", dest="json_output")
    deep_status.set_defaults(func=commands.cmd_deep_status)

    watch = sub.add_parser(
        "watch",
        help="watch the repository and keep the database incrementally fresh",
        description=(
            "Watch source/header files with a native event backend (Linux"
            " inotify / macOS FSEvents) or a portable polling fallback;"
            " debounce bursts and run the incremental update path when files"
            " change. Single instance per database; failures are retried;"
            " new/deleted files missing from the compile database are reported"
            " as compdb_stale and skipped."
        ),
    )
    watch.add_argument("--repo", required=True)
    watch.add_argument("--db", required=True)
    watch.add_argument("--compdb", default=None)
    watch.add_argument("--interval", type=float, default=5.0,
                       help="seconds between polls (default: 5)")
    watch.add_argument("--debounce", type=float, default=2.0,
                       help="seconds of quiet before a change set is applied (default: 2)")
    watch.add_argument("--jobs", type=int, default=8)
    watch.add_argument("--layer", choices=("deep", "base"), default="deep")
    watch.add_argument("--analyze-scope", choices=("main", "all", "none"), default="main")
    watch.add_argument("--record-scope", choices=("auto", "all", "main_file"), default="auto")
    watch.add_argument("--cache-dir", default=None)
    watch.add_argument("--state-file", default=None,
                       help="snapshot state path (default: <db>.watch.json)")
    watch.add_argument("--changed-files", nargs="*", default=None,
                       help="apply one manual batch instead of snapshotting")
    watch.add_argument("--once", action="store_true",
                       help="run one poll cycle and exit (tests/scripts)")
    watch.add_argument(
        "--backend", choices=("auto", "native", "poll"), default="auto",
        help=(
            "event backend: auto=native when available with poll fallback,"
            " native=require inotify/FSEvents, poll=portable snapshot poller"
        ),
    )
    watch.add_argument("--json", action="store_true", dest="json_output")
    watch.set_defaults(func=commands.cmd_watch)

    ui = sub.add_parser(
        "ui",
        help="serve the local read-only web UI",
        description=(
            "Serve a browser UI and JSON API for the indexed database on"
            " loopback. The server is read-only and reuses the store/federated"
            " query API; see docs/ui.md."
        ),
    )
    ui.add_argument(
        "--db", action="append", default=None,
        help="SQLite database path (repeatable for a federated UI)",
    )
    ui.add_argument("--db-dir", default=None,
                    help="directory of shard *.db files (federated UI)")
    ui.add_argument(
        "--serve-url", default=None,
        help=(
            "proxy the UI to a running `ca serve` backend (thin client);"
            " cannot be combined with --db/--db-dir"
        ),
    )
    ui.add_argument(
        "--host", default="127.0.0.1",
        help="bind host (default: 127.0.0.1, loopback only)",
    )
    ui.add_argument(
        "--port", type=int, default=8766,
        help="bind port (default: 8766; 0 picks a free port)",
    )
    ui.set_defaults(func=_cmd_ui)

    serve_parser = sub.add_parser(
        "serve",
        help="run the unified backend: query API + jobs/SSE + MCP endpoint",
        description=(
            "One long-lived process owns the read-only query service (single DB"
            " or shards) and a writable job state database; browsers use"
            " /api/* + /events and agents use /mcp (or `ca mcp --serve-url`)."
        ),
    )
    serve_parser.add_argument(
        "--db", action="append", default=None,
        help="SQLite database path (repeatable for a federated backend)",
    )
    serve_parser.add_argument("--db-dir", default=None,
                              help="directory of shard *.db files")
    serve_parser.add_argument("--host", default="127.0.0.1",
                              help="bind host (default: 127.0.0.1)")
    serve_parser.add_argument("--port", type=int, default=8765,
                              help="bind port (default: 8765)")
    serve_parser.add_argument(
        "--state-db", default=None,
        help="writable job state DB (default: <db-dir>/.ca/jobs.db)",
    )
    serve_parser.add_argument("--repo", default=None,
                              help="repository root for deep tools (optional)")
    serve_parser.add_argument("--compdb", default=None,
                              help="compile_commands.json for deep tools (optional)")
    serve_parser.set_defaults(func=commands.cmd_serve)

    desktop = sub.add_parser(
        "desktop", help="open the local browser UI and HTTP MCP backend",
    )
    desktop.add_argument("--db", default=None, help="existing index (default: per-user empty index)")
    desktop.add_argument("--port", type=int, default=None,
                         help="local port (default: 8766, or a free port if busy; 0 picks a free port)")
    desktop.add_argument("--no-browser", action="store_true", help="start without opening a browser")
    desktop.add_argument("--repo", default=None, help="repository root for deep tools")
    desktop.add_argument("--compdb", default=None, help="compile_commands.json for deep tools")
    desktop.set_defaults(func=_cmd_desktop)

    # -- workspace / project / variant registry --------------------------- #
    for group_name, help_text in (
        ("workspace", "register and inspect workspace metadata"),
        ("project", "register and inspect projects inside a workspace"),
        ("variant", "register, inspect and switch build variants"),
    ):
        group = sub.add_parser(group_name, help=help_text)
        actions = group.add_subparsers(dest="registry_action", metavar="<action>")
        actions.required = True
        add = actions.add_parser("add")
        add.add_argument("--db", required=True, help="registry database")
        add.add_argument("--json", action="store_true", dest="json_output")
        listing = actions.add_parser("list")
        listing.add_argument("--db", required=True, help="registry database")
        listing.add_argument("--json", action="store_true", dest="json_output")
        remove = actions.add_parser("remove")
        remove.add_argument("--db", required=True, help="registry database")
        remove.add_argument("--json", action="store_true", dest="json_output")
        target = remove.add_argument("target", help="registry id or path/name")
        target.help = "registry id or path/name"
        if group_name == "workspace":
            add.add_argument("--path", required=True, help="workspace root path")
            add.add_argument("--name", default=None, help="display name")
            add.add_argument("--kind", default="local", help="workspace kind (default: local)")
            listing.add_argument("--limit", type=int, default=0, help="maximum rows")
            show = actions.add_parser("show")
            show.add_argument("--db", required=True, help="registry database")
            show.add_argument("--json", action="store_true", dest="json_output")
            show.add_argument("target", help="workspace id or path")
        elif group_name == "project":
            add.add_argument("--workspace", required=True, help="workspace id or path")
            add.add_argument("--path", required=True, help="project path")
            add.add_argument("--name", default=None, help="project name")
            add.add_argument("--repo", default=None, help="repo name")
            add.add_argument("--product", default=None, help="product")
            add.add_argument("--board", default=None, help="board")
            add.add_argument("--version", default=None, help="version")
            listing.add_argument("--workspace", default=None, help="filter by workspace")
        else:
            add.add_argument("--workspace", required=True, help="workspace id or path")
            add.add_argument("--name", required=True, help="variant name")
            add.add_argument("--product", default=None, help="product")
            add.add_argument("--board", default=None, help="board")
            add.add_argument("--version", default=None, help="version")
            add.add_argument("--db-path", default=None, help="variant index database")
            listing.add_argument("--workspace", default=None, help="filter by workspace")
            show = actions.add_parser("show")
            show.add_argument("--db", required=True, help="registry database")
            show.add_argument("--json", action="store_true", dest="json_output")
            show.add_argument("target", help="variant id or name")
            use = actions.add_parser("use")
            use.add_argument("--db", required=True, help="registry database")
            use.add_argument("--json", action="store_true", dest="json_output")
            use.add_argument("target", help="variant id or name")
        group.set_defaults(func=commands.cmd_registry)

    version = sub.add_parser("version", help="print tool and schema versions")
    version.add_argument(
        "--json", action="store_true", dest="json_output", help="emit JSON"
    )
    version.set_defaults(func=commands.cmd_version)
    return parser


def _cmd_ui(args: argparse.Namespace) -> int:
    """Dispatch ``ca ui`` lazily so other commands never import http.server."""

    from ca.ui.server import cmd_ui

    return cmd_ui(args)


def _cmd_desktop(args: argparse.Namespace) -> int:
    from ca.desktop import launch

    return launch(args)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:  # argparse uses SystemExit for --help and errors
        code = exc.code if isinstance(exc.code, int) else 1
        return 0 if code == 0 else 1
    if not getattr(args, "command", None):
        parser.print_help(sys.stderr)
        return 1
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("error: interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # never leak a traceback as the CLI contract
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
