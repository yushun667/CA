# Layered indexing: base symbols now, deep facts on demand

CA separates the cheap "where is what" layer from the expensive CFG/data-flow
layer. Agents get a usable database quickly after a fresh checkout and pay for
deep analysis only for the functions they actually inspect.

```text
L0 base index        symbols / types / fields / direct calls / locations
   (ca index --layer base)                       deep_ready = 0
        │
        ▼
L1 lazy deep         CFG / branches / assignments / returns / resources /
   (ca ensure-deep)  call_targets for requested functions only
        │                                        deep_ready = 1
        ▼
L2 prewarm queue     hottest queried symbols analyzed in the background
   (ca prewarm / MCP prewarm)
```

## L0 — base index

```sh
ca index --repo /home/yu/CA_OH/openharmony \
    --compdb /home/yu/CA_OH/corpus/openharmony/compile_commands.unique.json \
    --db /home/yu/CA_OH/openharmony.db --jobs 20 --layer base
```

`--layer base` parses every **unique source file once** (duplicate TUs for the
same file are dropped), forces `record_scope=all` + `analyze_scope=none`, and
writes symbols, types, fields, direct calls and locations. Analysis tables are
empty, so `deep_ready` is `false` for every function. Queries that only need the
base layer stay instant:

- `find_symbol` / `functions`, `callers` / `callees`, `graph`, `detail`
  summaries, `stats`, exports.

Optionally the index layer can be set on rebuilds via `ca update --layer base`
(keeps the database in lazy mode and resets deep status for rebuilt files).

## L1 — lazy deep analysis

```sh
ca ensure-deep --db /home/yu/CA_OH/openharmony.db \
    --repo /home/yu/CA_OH/openharmony \
    --compdb /home/yu/CA_OH/corpus/openharmony/compile_commands.unique.json \
    --symbols OHOS_MyFunc,MyClass::Method --jobs 2
```

For each requested symbol `ensure_deep`:

1. resolves the function row and its translation unit (`functions.tu_id`);
2. skips it immediately if `deep_status` already says `ready` (cache hit);
3. otherwise parses the owning TU with `record_scope=main_file` (header-defined
   targets fall back to a full-record parse of the same TU), analyzes only the
   requested functions, writes the facts through `store.add_analysis_result`
   and marks them deep-ready.

Header-defined inline functions are valid targets: they are re-parsed with full
records on demand and analyzed once. Unrelated TUs are never parsed. Failures
are isolated per function (`status="error"`, database untouched) and reported
through the CLI/MCP result.

Result keys (stable):

```json
{"requested": 20, "results": [{"symbol": "kfree", "function_id": "fn:..",
  "status": "computed|ready|not_found|error", "deep_ready": true,
  "latency_ms": 9.9, "tu": "tu:..", "facts": {"branches": 3, "returns": 0},
  "error": ""}], "metrics": {"hits": 0, "computed": 20, "errors": 0,
  "hit_rate": 0.0, "parse_ms": 208.1, "analyze_ms": 0.1, "write_ms": 3.2,
  "first_latency_ms": 10.4, "hit_latency_ms": 5.3}}
```

On xv6 (46 TUs): L0 base index 0.31 s wall / 1.6 MB DB / deep_ready=0;
`ensure_deep` on 20 functions: first call ≈10 ms per symbol (208 ms parse for
their TUs), second call ≈5 ms per symbol (status hits), 0 errors.

On the OpenHarmony **unique-100** sample (matched libclang15 backend, 20 jobs):

| layer | result |
|---|---|
| L0 base | 100/100 TUs (deduped; 21 TUs degraded on missing generated headers), 206,017 functions / 37,260 types / 192,340 calls / 5,446 files / 1.43M LOC; parse CPU 711 s (full-record extraction ≈7.1 s/TU), write 69 s, wall 87.5 s, DB 359 MiB; deep coverage 0 |
| L1 ensure_deep (20 symbols) | first call 41.5 s total (on-demand TU parses, ≈2.06 s/symbol), 20/20 computed, 0 errors; second call 20.4 ms total (≈1 ms/symbol status hits); deep coverage 20/206,017 |
| MCP | `deep_status`/`detail` return deep flags; `ensure_deep` hit returns in ~1.6 ms |

Extrapolating L0 to 65k unique files at the same 20 jobs gives ≈15.8 h of wall
time (dominated by full-record extraction, 7.1 s CPU/TU); the "few hours" L0
target therefore needs either more workers (24 cores → ~13 h) or the ongoing
parser-side header-record pruning. Note the sample had missing generated
headers, so its `parse_ok`/coverage numbers will improve once build-capture's
header round lands. `ensure_deep` latency is dominated by the first parse of a
TU; batching symbols that share a TU amortizes it.

## L2 — prewarm queue

`ca prewarm --db ... --limit 20 --jobs 2` analyzes the symbols agents asked
about most (plus an explicit `--symbols` list). The MCP server records query
frequency automatically; `prewarm` can run synchronously or with
`background=true` on a daemon thread. The queue is persisted next to the
database and is fully decoupled from the offline index — a slow prewarm never
blocks interactive queries.

## deep_ready visibility (CLI/MCP)

- `ca deep-status --db ... [--symbols ...] [--json]` reports
  `{mode, total_functions, deep_ready, pending, failed, coverage}`.
- `ca query detail <sym>` and MCP `detail` include `deep_ready`.
- MCP `graph` includes `deep_ready` (root node; per-node flags are added by the
  store when available).
- MCP `branches/assignments/returns/resources` include `deep_ready` and
  `pending`; when a symbol is not deep-ready the list is empty and `pending` is
  true, so callers can decide to call `ensure_deep` (fast, returns immediately
  with base facts) or wait.
- `leaks` has `deep_ready: null` because findings span functions.
- New MCP tools: `ensure_deep` (`symbols`, `jobs`, `wait`), `deep_status`,
  `prewarm` (`limit`, `jobs`, `background`). All other tool schemas are
  unchanged; only fields are added.

The MCP server needs `--repo` and `--compdb` to run lazy deep analysis or
prewarm; without them these tools return `DEEP_UNAVAILABLE` and queries still
serve base facts.

## Status persistence and invalidation

Deep status is stored in the SQLite `deep_status` table by the store
(storage-engine task-19): `mark_deep_ready` / `mark_deep_failed` /
`mark_deep_pending`, `get_deep_status`, `list_deep_pending` / `list_deep_ready`,
`count_deep_status`, `deep_stats`, `invalidate_deep`. `query_function_detail` and
`query_call_graph` merge `deep_ready` into their results (additive keys only).
When an older store has no `deep_status` API, the CLI/MCP falls back to a small
JSON sidecar next to the database (`<db>.deep.json`) so the contract is stable.
Indexing with `--layer deep` marks analyzed functions deep-ready (`full` after
`--analyze-scope all`, inferred from existing facts for mixed runs).

A file change invalidates the affected TU's base analysis by rebuilding the
database (`ca update`) and resets deep status for the rebuilt database
(`ca update --layer base` keeps it lazy). Per-TU deep invalidation via
`invalidate_deep(tu_ids=...)` lands with the store's per-TU replace support.

## Latency budget and fallbacks

- Base queries (symbol/caller/callee/graph/detail summary): milliseconds — the
  L0 database is just SQLite indexes.
- `ensure_deep` first call: one TU parse (~0.2 s for xv6; ~1–4 s for a large
  OpenHarmony TU) divided across symbols in that TU, plus ~ms per function
  analysis; cache hit: single-digit milliseconds.
- If parse/analyze is unavailable (missing environment headers, unsupported
  compiler flags), `ensure_deep` returns `status="error"` per symbol and leaves
  the base facts intact; queries continue to work with `deep_ready=false`.

## Reproduce

```sh
.venv/bin/python benchmarks/run_deep_bench.py \
    --repo .tmp/corpus/xv6-riscv \
    --compdb .tmp/corpus/xv6-riscv/compile_commands.json --symbols 20 --jobs 4
```

The JSON result records the base-index cost, first/hit latency, fact yield and
coverage (`benchmarks/results/deep_bench*.json`).
