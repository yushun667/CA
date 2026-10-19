# CA — operating-system source analyzer

CA reads a repository's **real compile arguments**, parses every translation
unit with Clang, extracts functions, types, calls and source locations, builds
per-function CFGs and data-flow facts (branch conditions, assignments, return
paths, resource operations and leak evidence), and stores everything in SQLite
for fast queries.

Status: **phase-2 engineering delivered (2026-10-17)**. The phase-1 pipeline is
verified end-to-end on xv6-riscv (real RISC-V cross compile database) and
FreeRTOS-Kernel (real CMake compile database); phase 2 adds incremental
re-indexing (`--incremental`), an installable `ca` console script, a CI gate
(pytest + accuracy F1 ≥ 0.9) and a kernel-scale corpus benchmark. Accuracy and
performance baselines are recorded under `benchmarks/results/`.

## Installation

CA offers native binary packages for Windows, Linux and macOS, a Python source
installation, and a DeepSeek Harness plugin. Native packages include Python,
libclang, DuckDB, the database schema and the web UI; users do not install Python
or LLVM separately. A source installation requires **Python >= 3.11**.

### Native application (CLI, MCP and web UI)

Get the installer or portable archive for your OS and architecture from a
successful [Binaries workflow](https://github.com/yushun667/CA/actions/workflows/binaries.yml)
run's **Artifacts** section. These are workflow artifacts; this workflow does
not publish a GitHub Release. Available targets, installation paths and
verification instructions are in [the binary package guide](docs/binaries.md).

After installing, double-click **CA** to open the local browser UI. The same
executable also provides every CLI command and MCP:

```sh
ca version
ca index --repo /path/to/source --compdb /path/to/compile_commands.json --db /path/to/index.db
ca desktop --db /path/to/index.db
ca mcp --db /path/to/index.db
```

First launch creates an empty index in the user's data directory; index your
sources before querying. Portable archives must keep the executable and its
supporting files together. See [the binary package guide](docs/binaries.md) for
Windows commands, MCP client configuration, user data and build instructions.

### As a DeepSeek Harness plugin

1. Install DeepSeek Harness (desktop app or `dsh web`) and open its plugin
   manager.
2. Install the bundle from this repository's `dsh/` directory
   (Plugins → install from local directory → `<repo>/dsh`), then enable
   `@local/dsh-ca`.
3. Open the CA panel from the sidebar and add a project:
   - **Local**: choose **Choose directory…**, pick a source tree. The plugin
     installs a private CA venv, creates the SQLite index under
     `<source tree>/.ca/index.db` (a fixed hidden directory inside the source tree), starts `ca serve` and
     activates the project.
   - **Remote Linux**: choose **Remote server**, enter username, host/IP, port
     (22), and a password or a key/agent (免密), press **Connect**, browse the
     server and choose the source directory. The plugin installs the backend in
     `~/dsh-ca/backend` on the server, creates
     `<server source tree>/.ca/index.db`, starts `ca serve` behind an SSH tunnel and
     activates the project.
4. Run an **index** task on the Tasks page (agent tool: `ca_task`). A freshly
   created project starts with an empty index; queries return nothing until
   indexing finishes.
5. Installations are online (PyPI) by default. For an air-gapped host, build a
   per-platform wheelhouse with
   `node dsh/scripts/build-wheelhouse.mjs --platform <linux-x64|linux-arm64|macos-arm64|macos-x64|windows-x64> [--tar]`
   and place it under `$DSH_HOME/plugin-data/dsh-ca/wheelhouse/<platform>/` (or
   pass the path to the installer); the plugin uses it automatically. No
   backend configuration page exists — backend lifecycle is automatic. Passwords
   are transient (askpass helper), never persisted, so they are re-entered after
   a restart; Windows hosts should use key/agent authentication.

See [docs/dsh-plugin.md](docs/dsh-plugin.md) for the full plugin guide.

### CLI / development install

```sh
scripts/bootstrap.sh        # .venv + pinned deps + editable CA + libclang check
.venv/bin/ca version
```

`scripts/bootstrap.sh` requires Python 3.11+ and network access (or a populated
pip cache). See [docs/usage.md](docs/usage.md) for indexing and query commands.

## Pipeline

```text
real build ──> ca.build ──> ca.parse ──> ca.analyze ──> ca.store ──> ca.cli
               compile      Clang AST    CFG + data      SQLite      index/query/
               args         facts        flow facts      + indexes   stats/export
```

| Stage | Responsibility |
|---|---|
| `ca.build` | Load `compile_commands.json`, capture a real build through `CC`/`CXX` wrappers, reconstruct Linux kbuild `.cmd`, or synthesize flags with explicit provenance |
| `ca.parse` | libclang AST extraction: functions, types, fields, calls, source ranges, stable IDs; RISC-V/ARM cross-target inference and GCC sysroot handling |
| `ca.analyze` | Per-function CFG, branch conditions, assignments, return paths, resource operations (memory/lock/file/refcount/...), intraprocedural leak and double-release findings |
| `ca.store` | 13-table SQLite schema, batched WAL ingest, indexes built at `finalize()`, query API and JSONL export |
| `ca.pipeline` | Process-parallel orchestration, per-TU failure isolation, deduplication, `IndexSummary` metrics |
| `ca.cli` | `index`, `query`, `stats`, `export`, `version`; data on stdout, progress/metrics on stderr, exit codes 0/1/2 |

Shared contracts live in `src/ca/core/` (records, stable IDs, inter-module
protocols); import direction is `core <- {build, parse, analyze, store} <-
pipeline <- cli`.

## Quick start

```sh
scripts/bootstrap.sh     # .venv + pinned deps + `pip install -e .` + libclang check
.venv/bin/ca version     # console script (module form: PYTHONPATH=src .venv/bin/python -m ca)

# index a repository with a real compile database (paths under .tmp are corpora, not committed)
.venv/bin/ca index \
    --repo .tmp/corpus/xv6-riscv \
    --compdb .tmp/corpus/xv6-riscv/compile_commands.json \
    --db ca.db --jobs 8

# query
.venv/bin/ca query functions kalloc --db ca.db
.venv/bin/ca query callers kfree --db ca.db
.venv/bin/ca query branches syscall --db ca.db
.venv/bin/ca query resources kalloc --db ca.db
.venv/bin/ca query leaks --db ca.db
.venv/bin/ca stats --db ca.db
.venv/bin/ca export --db ca.db --out ca.jsonl
```

`--repo` must contain every `file` in the compile database; otherwise `index`
exits 1 instead of silently producing an empty database. When no compile
database is available, `auto` falls back to synthesized flags and prints a loud
warning; treat such extraction as reduced accuracy. Full instructions and how to
capture a build are in [docs/usage.md](docs/usage.md).

## Incremental re-indexing

`ca index --incremental` caches one parse result plus its analysis results per
translation unit in `<db-dir>/.ca-cache` (or `--cache-dir DIR`), keyed by the TU
source content, language/driver, exact compile arguments and tool/schema
version. Unchanged TUs skip parse+analyze and replay cached records; the stderr
summary reports hit rate:

```sh
.venv/bin/ca index --repo .tmp/corpus/xv6-riscv \
    --compdb .tmp/corpus/xv6-riscv/compile_commands.json --db ca.db --incremental
# [index] cache: hits=46/46 (100.0%), misses=0, dir=.../.ca-cache
```

Header contents are not part of the key: after editing a header, rebuild without
`--incremental` (or delete the cache). Corrupt cache entries are discarded and
parsed normally; the cache directory is self-ignoring for Git and safe to
delete.

## Agent access (MCP) and call graphs

CA is designed to run on the source server while AI agents query it. `ca mcp`
serves a read-only MCP interface over JSON-RPC 2.0 (stdio by default, HTTP
`POST /mcp` + `GET /health` on request) with tools for symbols, callers,
callees, call graphs, C/C++ facts, findings, details and stats. Call graphs are
also available from the CLI:

```sh
.venv/bin/ca graph --from main --depth 2 --db ca.db
.venv/bin/ca graph --from kfree --depth 2 --reverse --db ca.db --format dot
.venv/bin/ca mcp --db ca.db          # stdio; agents connect locally or over SSH
```

Incremental refresh uses `ca update --changed-files ... --repo ... --db ...`
(re-parses only affected TUs through the per-TU cache; header changes fall back
to a full rebuild). See [docs/mcp.md](docs/mcp.md) for tool schemas, SSH/tunnel
recipes and server verification, and [docs/incremental.md](docs/incremental.md)
for the offline/online model and watcher design.

## Human web UI (read-only)

For interactive inspection, `ca ui` serves a local, read-only browser UI and a
stdlib JSON API over the same index (symbol search, function detail with
branches/returns/resources, merged callers/callees, an SVG call graph with
candidate callback edges, callback registrations and stats). It binds
`127.0.0.1:8766` by default, has no write endpoints, and its frontend ships as
plain HTML/CSS/JS without a CDN or build step:

```sh
.venv/bin/ca ui --db ca.db                 # open http://127.0.0.1:8766/
.venv/bin/ca ui --db-dir shards/ --port 9000
```

See [docs/ui.md](docs/ui.md) for pages, API endpoints, the read-only guarantee
and SSH-tunnel guidance.

## DSH plugin (DeepSeek Harness)

`dsh/` packages CA as a DeepSeek Harness bundle (`@local/dsh-ca`): the full CA
page inside the Harness Web UI, twelve native `ca_*` tools for the agent, a
local or remote Linux `ca serve` backend, long indexing tasks and AI
explanations powered by the session's model (CA itself stays LLM-free). Projects pair one source root with one database (or shard
directory) and a backend location, created by hand in the CA panel. The
plugin is additive: the CLI, `ca ui`, `ca mcp` and the MCP protocol are
unchanged, and plugin code does not change CA's test baseline. Install it from
the local `dsh/` directory through the Harness plugin manager; see
[docs/dsh-plugin.md](docs/dsh-plugin.md) for installation, configuration, the
remote backend, UI pages, tools, troubleshooting and security boundaries.

## Continuous integration

`.github/workflows/ci.yml` runs on Python 3.11: installs the pinned requirements
and the editable package, runs the full test suite, then gates on extraction
accuracy (`benchmarks/run_accuracy.py --fail-under 0.9`). Corpora live under the
ignored `.tmp/` directory, so real-corpus e2e tests skip in CI while synthetic
and golden-fixture tests run everywhere.

## Verified results

Host: macOS arm64, 18 cores, 64 GB RAM, Python 3.11, libclang 18.1.1.

| Corpus | TUs | Unique functions | Calls | Branches | Assignments | Returns | Resource ops | Findings | DB | End-to-end |
|---|---|---|---|---|---|---|---|---|---|---|
| xv6-riscv (real RISC-V compdb) | 46 | 394 | 2,829 | 1,423 | 1,679 | 339 | 575 | 2 (known cross-function FPs) | 4.2 MiB | 0.79 s wall |
| FreeRTOS-Kernel (real CMake compdb) | 10 | 201 | 368 | 554 | — | — | 25 | 0 | 2.0 MiB | 2.1 s wall |
| Linux 6.12.113 kernel, arm64 defconfig subset (real kernel compdb) | 100 of 1,682 | 26,570 | 49,321 | 19,060 | 28,037 | 14,286 | 1,457 | 18 | 107 MiB | 183 s wall |

- **Extraction accuracy**: 7/7 fact categories (functions, types, calls,
  branches, assignments, returns, resources) have precision = recall = F1 =
  1.0000 on the labeled `golden_os_mini` fixture (target 0.9, PASS).
- **Parse/index throughput**: xv6 50–66 TU/s and 25–32k LOC/s with 8 workers
  on a loaded 18-core host (parse CPU 0.7–0.9 s, analyze CPU 1.4–2.2 s, write
  0.1 s); FreeRTOS 16–23 TU/s and 85–120k LOC/s.
- **Incremental re-index**: xv6 46 TUs cold 1.0–1.25 s → warm 0.29–0.30 s
  (3.4–4.2×, 100 % cache hit rate, identical counts); the cache only skips
  parse+analyze, the database is still rewritten.
- **Kernel scale**: the real Linux 6.12.113 subset (kernel/ mm/ fs/
  drivers/net/, arm64 defconfig, 1,682 real TUs) indexes the first 100 TUs in
  183 s wall (447 s parse CPU, 432 s analyze CPU, 124 s write, 107 MiB DB) on a
  loaded 18-core host. Each kernel TU expands large headers: 140k function
  analyses for 100 TUs (1.4k/TU) and 674k LOC of header-inclusive sources;
  analysis of header-defined inline functions dominates, as do write and memory
  costs (peak parent RSS ~6 GB). Full 1,682-TU runs are a multi-tens-of-minutes
  batch job for CI/nightly runs rather than interactive use.
- **Database**: synthetic 10k functions / 157k rows ingest at 410–465k rows/s
  (40.7 MiB DB, including the FTS5 trigram index at finalize); point queries
  p95 ≤ 3.6 ms, callers p95 2.9 ms; the kernel 100-TU DB (107 MiB, 26.6k
  functions, 49.3k calls) answers symbol/caller/branch/resource queries in
  single-digit milliseconds.
- **Tests**: `497 passed / 4 skipped` (contract, parse, analyze, store, FTS, e2e,
  incremental); xv6 e2e asserts non-vacuous counts and real compile-argument
  provenance.

## Reproduce

```sh
.venv/bin/python -m pytest -q
.venv/bin/python benchmarks/run_accuracy.py
.venv/bin/python benchmarks/run_parse_bench.py \
    --repo .tmp/corpus/xv6-riscv \
    --compdb .tmp/corpus/xv6-riscv/compile_commands.json --jobs 8
.venv/bin/python benchmarks/run_parse_bench.py \
    --repo .tmp/corpus/xv6-riscv \
    --compdb .tmp/corpus/xv6-riscv/compile_commands.json --jobs 8 --incremental
.venv/bin/python benchmarks/run_db_bench.py --functions 10000 --queries 300

# kernel scale (first 100 of 1,682 real TUs; full run is a nightly job)
.venv/bin/python benchmarks/run_parse_bench.py \
    --repo .tmp/linux/linux-6.12.113 \
    --compdb .tmp/corpus/linux/compile_commands.json --jobs 8 --max-tus 100
```

Each benchmark result JSON records the machine, corpus revision, compile-database
hash and command line.

## Repository layout

```text
src/ca/core/       Lead-owned records, stable IDs, module protocols
src/ca/build/      compile-argument acquisition and corpora helpers
src/ca/parse/      libclang AST extraction
src/ca/analyze/    CFG, data flow, resource analysis
src/ca/store/      SQLite schema, writer, queries
src/ca/pipeline.py multi-process indexing orchestration
src/ca/cli/        command-line interface
tests/             contract, unit, e2e, fixtures (labeled golden data)
benchmarks/        accuracy, parse/index (incremental), database benchmarks
docs/              bilingual user guide
scripts/           environment bootstrap (venv + editable install)
dsh/               DeepSeek Harness plugin bundle (CA page + native tools)
.github/           CI workflow and accuracy gate
.agents/           governance: architecture, decisions, planning, standards
```

## Accuracy and known limitations

- **Build data beats synthesis.** Without a real compile database, flags and
  include paths are guesses; use the capture path for serious analysis.
- **One preprocessor configuration** is analyzed (the captured commands); other
  `#ifdef` configurations are not explored.
- **Function pointers / indirect calls** are recorded as unresolved callees;
  caller/callee graphs are complete for direct calls.
- **Macros** are analyzed after expansion; macro-generated control flow is
  treated as opaque and text fields are bounded.
- **Cross-function data flow is not modelled**: leak findings describe resource
  paths inside one function; ownership transfer is heuristic. On xv6 two
  findings remain, both known false positives (kernel-resident allocation and
  loop-carried aliasing).
- **Incremental caching only keys on the main TU source and compile arguments**;
  header changes are not detected, so rebuild without `--incremental` after
  editing a header. The database is still rebuilt on every run; only the
  expensive parse/analyze work is skipped.
- **Kernel-scale analysis is expensive**: every TU expands thousands of
  header-defined inline functions, so analysis (not parsing) dominates and a
  full 1,682-TU kernel run is a multi-tens-of-minutes batch job. Header-derived
  analysis records may currently expose absolute paths for files outside the
  immediate TU source (open issue).
- **Linux kernel corpora require a Linux build environment**: the real kernel
  `compile_commands.json` is produced by a containerized Ubuntu build
  (`ca.build.kernel_corpus`, Apple `container`/Docker); the compile database
  and source tree stay under the ignored `.tmp/` directory.
- **One writer**: workers parse/analyze; the parent process performs all writes.

## Built by an agent team

This project was delivered by a Lead plus five parallel agents with disjoint
write scopes and a shared task board: `parser-engine` (AST extraction),
`build-capture` (real compile arguments and corpora), `ir-analyst` (CFG/data
flow/resources), `storage-engine` (SQLite ingest/queries) and `integration-qa`
(pipeline, CLI, e2e, accuracy/performance). Nontrivial choices are recorded in
[.agents/decisions/active/](.agents/decisions/active/AGENTS.md); architecture and
progress are in [.agents/architecture/overview.md](.agents/architecture/overview.md)
and [.agents/planning/progress.md](.agents/planning/progress.md).

## Next

Phase 2 delivered: incremental re-indexing, packaging/CI gates and
kernel-scale corpora. Next: FTS5-backed symbol search, C++ coverage, and
cross-function/aliasing-aware data flow.
