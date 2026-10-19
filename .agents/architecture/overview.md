# System architecture

Status: Phase 2 delivered on 2026-10-17; modules are verified on xv6, FreeRTOS and a Linux 6.12 kernel subset (see ../../planning/progress.md).

Rationale: `../decisions/active/`; execution status: `../planning/`.

## System boundary

- Goal: see the "project original goal" in the root `AGENTS.md`.
- Users: engineers and AI agents analyzing operating-system repositories (C/C++ first).
- Inputs: a source repository plus a real compilation database, or a captured build.
- Outputs: one SQLite database plus CLI queries over the extracted facts.
- Out of scope in this phase: runtime tracing, binary analysis, behavioral
  simulation, and whole-program pointer/alias analysis.

## Components and responsibilities

| Component | Responsibility | Contract |
|---|---|---|
| `ca.build` | Acquire real compile arguments: compile_commands, captured build, Linux kernel `.cmd` reconstruction, explicit synthesized fallback | `load_compilation_database`, `capture_build`, `reconstruct_kernel_commands`, `synthesize_for_files` |
| `ca.parse` | Parse each TU with libclang; extract functions, types, fields, calls, source locations | `parse_compilation_unit -> ParsedUnit` |
| `ca.analyze` | Per-function CFG, branch conditions, assignments, return paths, resource operations, intraprocedural data-flow findings | `analyze_function -> AnalysisResult` |
| `ca.store` | Bulk ingest with dedupe, indexes, and the query API | `SqliteStore` implementing `RecordStore` |
| `ca.pipeline` | Process-parallel orchestration, metrics, per-TU error isolation | `run_index -> IndexSummary` |
| `ca.cli` | `index`, `query`, `stats`, `export` commands | `main(argv) -> int` |
| `ca.desktop` | Launch a loopback unified backend and open the browser; initialize default user data | explicit `ca desktop`, or frozen executable without arguments |

`ca.core` is Lead-owned and holds the record contract, stable IDs, and the
inter-module protocols. No other module imports `clang` types into `core`.

## Key data flow

```text
repository + real build
  -> CompilationUnit (absolute file/directory, verbatim argv, source provenance)
  -> worker process: libclang AST -> ParseResult + per-function AnalysisResult
  -> main process: batched SQLite transactions -> indexed database
  -> CLI queries / JSONL export
```

Only plain dataclasses cross process and database boundaries; live libclang
objects remain inside the worker. A call's `callee_id` may reference a
declaration outside the analyzed files; the store materialises a placeholder
row so caller/callee joins stay complete.

## Interface and dependency boundaries

- Import direction: `core <- {build, parse, analyze, store} <- pipeline <- cli`.
- IDs come from `ca.core.ids`: external linkage uses the Clang USR, internal
  linkage adds the file, USR-less cursors fall back to name plus location.
- `SCHEMA_VERSION` versions the record/file format. A field or ID-rule change is
  a contract change and needs a decision record.
- A TU may be parsed from several translation units through headers; the store
  deduplicates by stable ID. Analysis of header-defined functions is deduped per
  worker and by database primary key.

## Phase-2 additions

- `ca.build.kernel_corpus`: reproducible Linux kernel corpus through an Apple
  container Linux environment (pinned kernel and image, LLVM arm64 build,
  kernel `gen_compile_commands.py`, container-to-host path rewrite). It fails
  explicitly when the container runtime is unavailable.
- `ca.store`: `functions_fts` is an FTS5 trigram external-content index built in
  `finalize()`; `meta.fts_ready` records availability. Symbol substring search
  uses FTS with a `LIKE` fallback; exact matches keep their original indexes.
- `ca.pipeline` / CLI: an incremental TU cache under `.ca-cache/` keyed by
  source hash + compile arguments + schema/tool version skips parse/analyze for
  unchanged translation units; `ca` is available as a console script.
- `.github/workflows/ci.yml`: CI runs the full test suite and an accuracy gate
  (macro F1 >= 0.9) on Python 3.11.

## Runtime and deployment

- Python 3.11 CLI with optional MCP/UI/unified backend; native
  Windows/Linux/macOS packages expose the same commands.
- Dependencies: `requirements.txt`; bundles include Python, matched libclang,
  DuckDB, schema and UI. Frozen worker dispatch precedes CLI; only frozen
  no-argument startup defaults to desktop.
- Desktop binds loopback; user data contains backend task state and an empty
  default index. Installers do not own user indexes or source trees. Installers
  and portable archives share `dist/ca`; native CI verifies builds. Contracts:
  [binary guide](../../docs/binaries.md).
- Parallelism uses one process per worker (`ProcessPoolExecutor`); SQLite WAL
  allows one writer (the main process) with concurrent readers.
- Corpora, captured builds, and benchmark artifacts live under `.tmp/` (ignored).
