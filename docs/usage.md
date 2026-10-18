# CA user guide

CA analyzes operating-system source code from its *real* build: it takes the
exact compile arguments from `compile_commands.json` (or a captured build),
parses each translation unit with Clang, extracts functions, types, calls and
source locations, runs CFG/data-flow analysis for branch conditions,
assignments, return paths and resource operations, and stores everything in
SQLite for fast queries.

Pipeline: **build → parse → analyze → store → CLI**.

## Installation

Requirements: Python 3.11, a C/C++ project with (ideally) a
`compile_commands.json`, and macOS or Linux. No sudo is needed; all Python
dependencies are pinned in `requirements.txt`.

```sh
# from the repository root
scripts/bootstrap.sh     # .venv + pinned deps + `pip install -e .` + libclang check
.venv/bin/ca version     # console script installed by the editable package
```

`bootstrap.sh` installs the package in editable mode, so the `ca` console script
lives at `.venv/bin/ca`. The module form works too (useful in scripts that must
not rely on the console script):

```sh
PYTHONPATH=src .venv/bin/python -m ca version
```

## Indexing a repository

```sh
# index xv6 with its captured compile database (explicit path is safest)
PYTHONPATH=src .venv/bin/python -m ca index \
    --repo .tmp/corpus/xv6-riscv \
    --db ca.db \
    --compdb .tmp/corpus/xv6-riscv/compile_commands.json

# explicit compile database, limited work (useful for a quick check)
PYTHONPATH=src .venv/bin/python -m ca index \
    --repo .tmp/corpus/xv6-riscv \
    --db ca.db \
    --compdb .tmp/corpus/xv6-riscv/compile_commands.json \
    --jobs 8 --max-tus 20
```

`index` options:

| Option | Meaning |
|---|---|
| `--repo DIR` | repository root; all extracted paths are relative to it |
| `--db PATH` | output SQLite database (created/overwritten) |
| `--compdb FILE\|DIR` | `compile_commands.json` or a directory containing one |
| `--jobs N` | worker processes (default 8); parsing/analysis run in parallel, the parent is the only writer |
| `--max-tus N` | index at most N translation units |
| `--source auto\|compdb\|capture\|synthesized` | where compile arguments come from |
| `--build-cmd CMD` | `capture` mode only: build command to run, e.g. `make -j8` |
| `--incremental` | reuse cached parse/analysis results for unchanged TUs (cache beside the DB: `<db-dir>/.ca-cache`) |
| `--cache-dir DIR` | explicit incremental cache directory (implies `--incremental`) |
| `--clear-cache` | delete the incremental cache before indexing (implies `--incremental`) |
| `--analyze-scope main\|all\|none` | deep-analysis scope (default `main`; see below) |
| `--record-scope auto\|all\|main_file` | AST extraction scope: `auto` picks `main_file` above 200 TUs (default), keeping header symbols only for smaller repos |
| `--transport-filter none\|declarations` | drop declaration records from the worker payload (lossy, opt-in) |
| `--measure-transport` | report ParseResult pickle bytes/TU on stderr |

Compile-argument sources:

- `auto` (default): use `compile_commands.json` when found in the repository
  root or a common build directory; otherwise fall back to synthesized
  arguments. The fallback prints a loud `WARNING: ... SYNTHESIZED ...` block on
  stderr. `auto` never looks outside `--repo`, so always pass `--compdb` when
  the database lives elsewhere.
- The compile database must describe sources under `--repo` (both `file` and
  `directory` must live there, symlinks are normalized). Otherwise `index`
  exits `1` with an explanation instead of silently writing an empty database.
- `compdb`: require a compile database; error if missing.
- `capture`: run `--build-cmd` under a compiler wrapper and record the real
  commands; `--compdb` then names the output directory.
- `synthesized`: never use real build data; arguments are guessed from the
  repository layout and the `CompilationUnit.source` field is `synthesized`.
  Treat extraction quality as reduced.

### Analyze scope (large corpora)

On a corpus like OpenHarmony, every TU expands thousands of header-defined
inline functions while only tens of definitions live in the TU's own source
file. Deep-analyzing all header definitions again for every including TU
dominates indexing time, so `ca index` defaults to:

| Scope | Deep analysis | Use case |
|---|---|---|
| `main` (default) | only definitions whose `sloc.file` is the TU's main source file | large corpora; scales with `--jobs` |
| `all` | every definition; phase A determines one owning TU per definition file, phase B re-parses and analyzes each definition exactly once (extra parse pass) | complete per-function facts across the corpus |
| `none` | no CFG/data-flow analysis | symbols/calls/graph only, maximum speed |

Symbols, types, fields and calls are always extracted regardless of
`--analyze-scope`, so `query functions`, `callers`/`callees`, `graph` and MCP
queries keep working on those records; only branches/assignments/returns/
resource operations/findings are affected by the analysis scope (header
functions then have no deep facts).

`--record-scope` controls how much of the Clang AST is turned into records
*at all*. `all` keeps every header and main-file definition/call.
`main_file` records only definitions declared in the TU's own source file plus
the calls whose caller is a recorded function (types and fields stay complete
for struct layout/offset analysis); header-defined entities then have no
symbol rows in the database. `auto` (default) uses `main_file` when indexing
more than 200 TUs and `all` otherwise, so small/toy repositories keep the full
symbol graph while large ones avoid re-recording the same header definitions
for every TU. On OpenHarmony this cuts the cross-process payload by ~85%
(≈9.2 MB → ≈1.4 MB pickle per TU) and parse CPU by ~39%. If you need one copy
of the header symbols in a large corpus, index the headers' representative TU
with `--record-scope all` (or a separate symbol pass) — the two scopes can be
mixed across runs.

The stderr summary reports the split, e.g. `[index] scope=main
record_scope=main_file: analyzed 347/354 definitions (7 skipped)`; with
`--measure-transport` it also prints pickle MB/TU before/after the optional
declaration filter. `ca update` uses the same defaults; the incremental cache
key includes both scopes, and `analyze-scope all` disables the cache because
its ownership is corpus-wide.

### Incremental re-indexing

`--incremental` caches one `ParseResult` plus its analysis results per
translation unit under `<db-dir>/.ca-cache` (or `--cache-dir DIR`). A cached TU
is keyed by a hash of its source-file content, language/driver and exact
compile arguments plus the tool/schema versions. On the next run, unchanged TUs
skip parse+analyze, replay their cached records into the database, and the
stderr summary reports the hit rate:

```sh
# first run: cold cache (hits=0/N)
PYTHONPATH=src .venv/bin/python -m ca index \
    --repo .tmp/corpus/xv6-riscv \
    --compdb .tmp/corpus/xv6-riscv/compile_commands.json \
    --db ca.db --incremental

# second run: unchanged TUs are cache hits
PYTHONPATH=src .venv/bin/python -m ca index \
    --repo .tmp/corpus/xv6-riscv \
    --compdb .tmp/corpus/xv6-riscv/compile_commands.json \
    --db ca.db --incremental
```

Invalidation rules:

- a cached entry is reused only when the **main TU source content**, language,
  driver, compile arguments, tool version and schema version are unchanged;
- **header contents are not part of the key**. After changing a header, run a
  full index without `--incremental` (or delete the cache directory); otherwise
  stale results are replayed;
- the cache is best effort: corrupt/truncated entries are discarded and the TU
  is parsed normally; `--clear-cache` (or deleting `.ca-cache`) is always safe.

Progress and metrics are printed to **stderr**; stdout stays empty so `index`
can be used in scripts. A non-zero number of failed translation units does not
abort the run: failures are listed in the summary and the process exits `0` as
long as at least one TU parsed. Exit code `1` means an argument/runtime error
or that no TU could be parsed.

## Querying

All queries take `--db`, optional `--limit N` and `--json`:

```sh
Q="PYTHONPATH=src .venv/bin/python -m ca query"
$Q functions kalloc --db ca.db
$Q callers kfree --db ca.db
$Q callees syscall --db ca.db
$Q branches syscall --db ca.db
$Q assignments fork --db ca.db
$Q returns wait --db ca.db
$Q resources kalloc --db ca.db
$Q leaks memory --db ca.db
$Q detail kalloc --db ca.db
```

- The symbol argument is a function name or unique substring (exact matches
  first, then substring); `leaks` instead treats it as a resource-kind filter.
  `resources` therefore lists acquire/release operations *inside* a function.
- Human-readable table on stdout (columns chosen per fact kind).
- `--json` emits the raw query rows (stable keys are the record field names
  from `ca.core.schema`, with source locations flattened into
  `file, line, col, end_line, end_col`) or one JSON object for `detail`.
- Exit code `2` means the query succeeded but returned no rows; `1` means a
  usage/runtime error.

`leaks` may omit the symbol and then lists all leak findings; its optional
symbol filters by resource kind (`memory`, `file`, `lock`, ...). Every other
query kind requires a symbol.

## Stats and export

```sh
PYTHONPATH=src .venv/bin/python -m ca stats --db ca.db          # row counts + DB size
PYTHONPATH=src .venv/bin/python -m ca stats --db ca.db --json
PYTHONPATH=src .venv/bin/python -m ca export --db ca.db --out ca.jsonl
```

`export` writes one JSON object per line, each with `kind` and `schema`. Kinds:
`function, type, field, call, cfg_block, cfg_edge, branch, assignment, return,
resource_op, finding` (plus `compilation_unit` when persisted). This is the
format used by the accuracy benchmark.

## Output reference

`index` writes a summary to stderr with:

- translation units parsed/failed and the database path;
- record counts (functions, types, calls, files, LOC); these are unique
  persisted rows, so header definitions parsed from many TUs are counted once
  (use `stats` for the same table counts);
- `records produced before dedupe` (raw per-TU totals for functions, types,
  fields and calls). Produced counts include repeated header declarations from
  every TU; they are diagnostics, while summary/DB counts stay unique;
- analysis counts (analyzed functions, branches, assignments, returns,
  resource operations, findings);
- timing breakdown: parse CPU seconds (summed over workers), analysis CPU
  seconds, database write seconds, wall time;
- database size and up to ten error lines;
- in incremental mode, `[index] cache: hits=.. / misses=..` with the cache
  directory.

SQLite tables: `meta`, `tus`, `functions`, `params`, `types`, `fields`, `calls`,
`cfg_blocks`, `cfg_edges`, `branches`, `assignments`, `returns`, `resource_ops`,
`findings`. Secondary indexes and `ANALYZE` run in `finalize()`; a database
that was interrupted before finalize is not query-optimized.

## Layered indexing (base now, deep on demand)

For very large trees, index the cheap layer first and analyze functions lazily:

```sh
.venv/bin/ca index --repo <repo> --compdb <compdb> --db ca.db --jobs 20 --layer base
.venv/bin/ca ensure-deep --db ca.db --repo <repo> --compdb <compdb> \
    --symbols func_a,func_b --jobs 2
.venv/bin/ca deep-status --db ca.db
.venv/bin/ca prewarm --db ca.db --repo <repo> --compdb <compdb> --limit 20
```

While `ca index`/`ca update` are explicit batch commands, `ca watch` keeps a
database fresh automatically: it polls source/header mtimes, debounces bursts
and runs the same incremental update path, reporting per-cycle metrics
(`--once` for one poll). See [incremental.md](incremental.md).

`--layer base` parses every unique source file once and skips deep analysis;
symbols/types/calls/graph work immediately while `deep_ready=false`.
`ensure-deep` parses only the owning TUs of the requested functions, writes
CFG/branch/assignment/return/resource/candidate facts and marks them
`deep_ready=true`; repeat calls hit the deep-status store (~ms). MCP exposes the
same flow through `ensure_deep`, `deep_status` and `prewarm` tools and adds
`deep_ready`/`pending` fields to detail/graph/fact queries. See
[layered-indexing.md](layered-indexing.md) for latency budgets, invalidation and
fallbacks.

## Federated queries across shard databases

Large corpora can be indexed as several shard databases (the OpenHarmony
acceptance run uses `oh_S1.db` … `oh_S8.db`). `query`, `graph` and `stats`
accept a directory (or repeated `--db`) and merge the shards read-only:

```sh
# every *.db in the directory, sorted by path; unreadable files are skipped
.venv/bin/ca query functions MySymbol --db-dir /home/yu/CA_OH/index --json
.venv/bin/ca query detail MySymbol --db-dir /home/yu/CA_OH/index
.venv/bin/ca query callers MySymbol --db-dir /home/yu/CA_OH/index --limit 20
.venv/bin/ca graph --from MySymbol --depth 2 --db-dir /home/yu/CA_OH/index
.venv/bin/ca stats --db-dir /home/yu/CA_OH/index

# explicit shard list instead of a directory
.venv/bin/ca query branches MySymbol --db oh_S3.db --db oh_S5.db
```

- Every merged row gets an extra `shard` field (database filename stem) in JSON
  and table output; rows are deduplicated by stable ids (first shard in path
  order wins), sorted deterministically and `--offset`/`--limit` are applied
  after merge, so pagination is exact. Per-shard queries push `offset+limit`
  down to bound scans.
- `stats --db-dir` sums table counts/bytes and adds `shards` (per-shard
  `db_bytes`/`schema_version`/tables) and `skipped` entries.
- Unreadable/missing shard files are skipped with a stderr warning; if no shard
  is readable the command exits 1.
- Federated mode is read-only: no write commands (deep analysis, indexing,
  callbacks post-processing) run against a shard set; `ensure_deep`/`prewarm`
  need a single `--db`. Single-`--db` behaviour is unchanged (no `shard` key).
- Shards can be queried in place (with their `-wal`/`-shm` sidecars) or as
  single-file `.db` copies; copies fall back to read-only `immutable=1` (works,
  slightly slower) and must be static while queried (a shard being actively
  written/checkpointed is not a valid federated source).
- Local benchmark (macOS arm64, 5 shards = real OH S7 copy + 4 synthetic,
  read-only; single-file copies): `functions` p50 7.6 ms / p95 9.1 ms,
  `detail` 7.0/7.3 ms, `callers` 11.3/16.1 ms, `graph depth=1` 6.9/7.4 ms,
  `stats` 12.0/12.3 ms; single S7 baseline p50 3.7–10.0 ms (see
  `benchmarks/results/federated_bench.json`). Querying shards in place with
  sidecars is faster than single-file immutable copies.

## Agent access: MCP, call graph, incremental updates

CA is designed to run on the machine that owns the sources; AI agents query it
instead of copying repositories around. Three interfaces serve that workflow:

```sh
# read-only MCP server (stdio by default; HTTP POST /mcp + GET /health)
.venv/bin/ca mcp --db /home/yu/CA_OH/openharmony.db
.venv/bin/ca mcp --db /home/yu/CA_OH/openharmony.db --transport http --port 8765

# call graph: forward/reverse/both, depth-limited, table/JSON/DOT
.venv/bin/ca graph --from syscall --depth 2 --db ca.db
.venv/bin/ca graph --from kfree --depth 1 --reverse --db ca.db --format dot

# incremental refresh after editing a few files (offline batch stays `index`)
.venv/bin/ca update --repo .tmp/corpus/xv6-riscv --db ca.db \
    --changed-files kernel/syscall.c
```

MCP exposes `find_symbol`/`functions`, `callers`, `callees`, `graph`, `branches`,
`assignments`, `returns`, `resources`, `leaks`, `detail` and `stats` with a
stable JSON envelope, limits/pagination and stable error codes. See
[mcp.md](mcp.md) for the tool schemas, SSH/tunnel setup and server verification,
and [incremental.md](incremental.md) for the offline/online model, cache
invalidation (headers require a full rebuild) and the watcher/daemon design.

### Cross-TU callbacks (post-processing)

The index resolves callbacks inside each translation unit. Global registration
tables that are written in one TU and invoked from another (ops structs, array
tables, designated initializers, registration calls such as
`AddCallback(&g_ops, fn)`, callbacks passed as arguments or returned by getters)
are resolved by a separate pass that can run on an existing database:

```sh
# post-process an existing database (idempotent; safe to re-run)
.venv/bin/ca resolve-callbacks --db ca.db --repo .tmp/corpus/xv6-riscv --jobs 2

# or run it automatically after indexing
.venv/bin/ca index --repo .tmp/corpus/xv6-riscv \
    --compdb .tmp/corpus/xv6-riscv/compile_commands.json --db ca.db \
    --resolve-callbacks

# inspect registration facts and candidate edges
.venv/bin/ca stats --db ca.db --json          # .callbacks: registrations/object paths/fields/modules/linked edges
.venv/bin/ca graph --from filewrite --depth 1 --db ca.db --format json
```

The pass reparses the database's TUs (it needs the AST) and reports units
parsed/failed, registration facts (by kind), indirect call sites, candidate
edges (with confidence buckets), links and elapsed time; `--max-tus` limits a
trial run and `--min-confidence` filters weak matches. It only records
registrations into *global* storage, which keeps same-name local test structs
out of the candidate set. Candidate edges are ordinary `call_targets` rows
(`kind="indirect_candidate"`, `source="call_target"`, `candidate=true`,
confidence/reason), so `ca graph`, `ca query callees` and the MCP `graph` tool
show them without any other change; MCP also gains a `callbacks` tool and a
`stats.callbacks` object. Re-running the pass on an unchanged database writes
nothing (natural-key upserts and stable ids). Header-inline duplicates of the
same logical edge (same caller/callee/line across TUs) are collapsed by the
resolver's dedupe rule; see [cross-tu-callbacks.md](cross-tu-callbacks.md) for
the full registration/qualifier/dedupe contract. On the OpenHarmony acceptance
shards (libclang15, parse-only) the pass added **264 logical candidate edges**
(S5 256, S7 8 after header-inline dedupe), raising candidate callers from
2.18% to 2.32% of indirect-call callers with a 10/10 production-path precision
sample. Incremental re-index/`ca update` and a watcher are documented in
[incremental.md](incremental.md).

## Web UI (read-only browser)

`ca ui` serves a local read-only web UI over the same index: symbol search,
function detail (location/signature/branches/returns/resources/calls), merged
callers/callees, an SVG call graph (candidate callback edges dashed with
confidence), callback registrations and database statistics. Federated shard
queries show a `shard` badge per row. The server binds `127.0.0.1` by default,
serves a stdlib JSON API (`/api/health`, `/api/search`, `/api/function/...`,
`/api/callers`, `/api/callees`, `/api/graph`, `/api/stats`, `/api/callbacks`)
and has no write endpoints:

```sh
.venv/bin/ca ui --db ca.db                 # http://127.0.0.1:8766/
.venv/bin/ca ui --db-dir shards/ --port 9000
```

The frontend ships in `src/ca/ui/static` with no CDN or build step. See
[ui.md](ui.md) for pages, the JSON envelope/error codes, the read-only
`PRAGMA query_only` guarantee and SSH-tunnel guidance.

## Accuracy and performance

Reproducible scripts live in `benchmarks/` (results in `benchmarks/results/`,
ignored by git):

```sh
.venv/bin/python benchmarks/run_accuracy.py     # golden fixture precision/recall/F1
.venv/bin/python benchmarks/run_parse_bench.py  # TU/s, LOC/s, peak memory on xv6
.venv/bin/python benchmarks/run_db_bench.py     # write rows/s, query p50/p95

# real corpora: always pass the compile database explicitly
.venv/bin/python benchmarks/run_parse_bench.py \
    --repo .tmp/corpus/xv6-riscv \
    --compdb .tmp/corpus/xv6-riscv/compile_commands.json --jobs 8
# cold/warm incremental re-index: hit rate and speedup
.venv/bin/python benchmarks/run_parse_bench.py \
    --repo .tmp/corpus/xv6-riscv \
    --compdb .tmp/corpus/xv6-riscv/compile_commands.json --jobs 8 \
    --incremental --out benchmarks/results/parse_bench_incremental.json
.venv/bin/python benchmarks/run_parse_bench.py \
    --repo .tmp/corpus/FreeRTOS-Kernel \
    --compdb .tmp/corpus/freertos/compile_commands.json --jobs 8 \
    --out benchmarks/results/parse_bench_freertos.json
```

### Kernel scale

The Linux kernel corpus is far larger than the toy corpora: 1,682 real C
translation units (kernel/ mm/ fs/ drivers/net/, arm64 defconfig) from a
containerized Linux build. Start with a tier and scale up — each kernel TU
expands large headers, so parse is ~2–5 s/TU CPU before analysis:

```sh
# first 100 TUs (measured: 100/100 parsed, 0 failed, 107 MiB DB)
.venv/bin/python benchmarks/run_parse_bench.py \
    --repo .tmp/linux/linux-6.12.113 \
    --compdb .tmp/corpus/linux/compile_commands.json \
    --jobs 8 --max-tus 100 --out benchmarks/results/parse_bench_linux_100.json

# end-to-end index + queries on the same tier
.venv/bin/ca index --repo .tmp/linux/linux-6.12.113 \
    --compdb .tmp/corpus/linux/compile_commands.json \
    --db .tmp/linux-kernel.db --jobs 8 --max-tus 100
.venv/bin/ca query callers kfree --db .tmp/linux-kernel.db
.venv/bin/ca query resources skb_share_check --db .tmp/linux-kernel.db
```

Measured on an 18-core host under load: 183 s wall (447 s parse CPU, 432 s
analyze CPU, 124 s SQLite write), 107 MiB DB, 674k header-inclusive LOC, and
26,570 functions / 49,321 calls / 19,060 branches / 28,037 assignments / 14,286
returns / 1,457 resource operations / 18 findings. Each kernel TU expands large
headers, so 100 TUs already trigger ~140k function analyses; analysis, writing
and memory (peak parent RSS ~6 GB) dominate at this scale. A full 1,682-TU pass
is a multi-tens-of-minutes batch job suited to a nightly CI run rather than
interactive use. Incremental runs help for repeat passes over an unchanged
tree, but header edits still require a full rebuild.

### OpenHarmony-scale validation

The 300-TU OpenHarmony subset (rk3588 product, 270 unique source files) was
measured on a 24-core/62 GB Linux server with the same pipeline. With
`--record-scope main_file --analyze-scope main` (the large-corpus defaults) and
environment-generated headers still partially missing (280/300 TUs parsed):

| config | wall | CPU/TU | parse CPU | analyze CPU | write | pickle/TU | DB | projected 70k ETA |
|---|---|---|---|---|---|---|---|---|
| jobs=8, scope=main | 75.2 s | 1.81 s | 484.9 s | 15.4 s | 6.5 s | 1.13 MB | 89.2 MB | 5.2 h |
| jobs=20, scope=main | 54.2 s | 2.80 s | 745.9 s | 25.1 s | 12.7 s | 1.15 MB | 89.7 MB | **3.75 h** |
| jobs=8, scope=all | 139.7 s | 3.43 s | 939.7 s | 14.7 s | 6.9 s | 1.17 MB | 89.6 MB | 9.7 h |
| jobs=20, scope=all | 101.8 s | 5.45 s | 1487.0 s | 23.4 s | 15.4 s | 1.17 MB | 89.6 MB | 7.1 h |

The same subset with the pre-optimization extraction scope (`--record-scope all`,
no header-dedup change: `record_scope` only affects extraction, this is the
"before" baseline) measured 191.8 s wall / 3.39 CPU/TU / 7.53 MB pickle/TU /
358.9 MB DB at jobs=8 and 173.8 s / 4.52 CPU/TU / 12.07 h projected at jobs=20.
So `record_scope=main_file` cuts the database by ~75%, the cross-process pickle
payload by ~85%, and wall time by 2.6–3.2×, moving the projected full-corpus ETA
from 13.3 h/12.1 h (jobs=8/20, just above the target) to 5.2 h/**3.75 h**.

The target (`scope=main`, `jobs=20`: ≤5 s CPU/TU, full-corpus ETA ≤12 h) is met
at 2.80 s CPU/TU and 3.75 h projected. `analyze_scope=all` with main-file
extraction still lands at 7.1 h projected. These are ETA extrapolations from 280
parsed TUs at the same job count; environment headers were still incomplete, so
`parse_ok` and wall time are expected to improve further.

Each result JSON records machine, corpus revision, compile-database hash,
corpus metadata (kernel version/arch/config) and the command line, so numbers
can be reproduced and compared over time.

## Tests

```sh
.venv/bin/python -m pytest -q                  # all tests
.venv/bin/python -m pytest tests/test_e2e_*.py -q
```

The end-to-end tests build a self-contained repository in a temporary
directory; the xv6 test is skipped unless `.tmp/corpus/xv6-riscv/
compile_commands.json` exists.

## Agent-oriented query helpers

- `ca brief <symbol> --db ... [--json]` returns a compact summary: signature,
  location, caller/callee counts, branch/return/resource counts and the first
  key condition, so an agent can decide whether to fetch details.
- `ca query detail <symbol> --fields function,deep_ready --json` projects the
  detail payload to selected top-level fields; `--compact` returns the built-in
  compact form. This avoids the 13-23 KB detail payloads.
- `ca query <kind> --symbols FILE|- --json` runs one batched query for several
  symbols (one entry per symbol, stable order); it also works with `--db-dir`
  federated indexes.

## Known limitations

- **Build data beats synthesis.** Without a real `compile_commands.json`, flags,
  include paths and macros are guesses; header parsing and type queries lose
  accuracy. Capture a real build for serious analysis.
- **Preprocessor configuration is one configuration.** Only the macros/includes
  of the captured commands are analyzed; `#ifdef` branches for other configs
  are not explored.
- **Function pointers and indirect calls** are recorded as unresolved callees;
  caller/callee graphs are complete for direct calls only.
- **Macros** are analyzed after expansion; branch/assignment text comes from
  the expanded source and can look unlike the original macros.
- **Cross-function data flow** is not modeled: leak findings describe resource
  paths inside one function, not ownership transferred between functions.
- **Analysis failures are isolated per function; parse failures per TU.** Check
  the summary's error lines for partial results.
- **Incremental caching only tracks the main TU source and compile arguments.**
  It reuses parse/analysis results for unchanged translation units, but header
  changes are not detected: rebuild without `--incremental` after editing a
  header. The database itself is still rebuilt on every `index` run; only the
  expensive parse/analyze work is skipped.
- **One writer.** Parallel workers only parse/analyze; SQLite ingestion happens
  in the parent process.
- The index covers only files under `--repo` and only translation units listed
  by the compile database (or discovered as `*.c` for synthesis).
