# Offline indexing and incremental updates

CA separates batch work from interactive querying:

- **Offline full pass** — `ca index` (or `ca update` after a change) parses and
  analyzes source code and rewrites the SQLite database. Run it in the
  background or on a schedule; agents never trigger a full parse.
- **Online queries** — the MCP server (`ca mcp`) and the CLI query commands read
  the finished database. They never write it.

This page documents the incremental model, its invalidation limits and the
watcher/daemon design. The MCP wire protocol is in [mcp.md](mcp.md); benchmark
numbers are in `benchmarks/results/`.

## Two entry points

```sh
# offline: full pass over every TU in the compile database
ca index --repo /src/proj --compdb /src/proj/compile_commands.json \
    --db /data/proj.db --jobs 24

# incremental: refresh after editing a few files
ca update --repo /src/proj --compdb /src/proj/compile_commands.json \
    --db /data/proj.db --changed-files src/foo.c src/bar.c

# show the affected-unit plan without touching the database
ca update --repo /src/proj --compdb /src/proj/compile_commands.json \
    --db /data/proj.db --changed-files src/foo.c --dry-run
```

`ca update` maps each changed path to compilation units, then re-runs the
pipeline with the per-TU cache:

- changed TUs miss the cache and are parsed/analyzed again;
- unchanged TUs hit the cache and replay their records;
- the database is rebuilt from the cached + fresh records in one pass.

The command prints the plan and the cache statistics:

```text
[update] affected 2/1682 TUs (header_change=False), cache=/data/.ca-cache
[update] cache hits=1680/1682 misses=2; database rebuilt from cache
```

## Cache semantics

The cache directory defaults to `<db-dir>/.ca-cache` and can be set with
`--cache-dir`. One entry per TU stores the detached `ParseResult` plus its
`AnalysisResult`s. The key is a SHA-256 over:

1. the TU **main source file content**;
2. language and compiler driver;
3. the exact compile arguments;
4. the deep-analysis scope (``main``/``all``/``none``) and the extraction scope
   (``all``/``main_file``);
5. the CA tool version and schema version.

``analyze_scope=all`` is not cacheable per TU: its definition-file ownership is
corpus-wide (phase A), so `ca update` falls back to a full analysis pass for
that scope and the pipeline disables the incremental cache.

A missing, corrupt or truncated cache entry is treated as a miss: the TU is
parsed normally and the entry is rewritten. Deleting the cache directory is
always safe (`ca index --clear-cache` does it for you).

### Invalidation boundary

| Change | Detected? | Action |
|---|---|---|
| main source content | yes | that TU re-parsed on next `update` |
| compile arguments / include flags | yes | affected TU re-parsed (cache key changes) |
| tool or schema upgrade | yes | whole cache misses once |
| **header content** | yes (include facts available) | only TUs whose transitive include set contains the changed header re-parse; unrelated TUs stay cache hits |
| header/build change without include facts | fallback | old behaviour: clear the whole cache, warn, re-parse all TUs |
| build files (`Makefile`, `CMakeLists.txt`, …) | build files still force a full re-parse | header-like changes only are dependency-scoped |
| file added/removed in the compile database | next `update` run resolves a fresh compile database | new TUs parsed, removed TUs disappear only after the database rebuild |

`ca index` records each TU's repo-local direct includes plus include edges
(including header→header) from the parser; the store persists their transitive
closure in `tu_includes` (`include_path -> tu_id`). A changed header therefore
maps directly to the dependent TUs:

```text
[update] include dependencies: 2/1682 TUs include the changed header(s)
[update] dependency-aware: invalidated 2 cache entries for 2 dependent TUs
[update] cache hits=1680/1682 misses=2; database rebuilt from cache
```

Changed headers that no indexed TU includes are a no-op (`ca update` exits 2
with "not included by any indexed TU"). Databases built before include facts
(or with `record_scope`/parser versions that did not collect them) fall back to
the previous full-cache-clear behaviour with an explicit warning. `ca watch`
uses the same mapping and reports `dependency_aware`, `fallback_full`,
`affected_count`, `cache_hits` and `cache_misses` in its per-cycle JSON.

## Current limitation: database rows are rewritten

The store does not yet expose per-TU delete/replace, so every `update` rebuilds
the whole database file from cached + fresh records (`ca update` removes the old
DB first). Parse/analyze work is incremental; database writing is not. The
planned store API is `remove_units(tu_ids)` with cascading deletes of
tus/functions/types/fields/calls, function-level facts (CFG, branches,
assignments, returns, resource ops, call_targets/findings) and a finalize step
that rebuilds the FTS index. Once available, `ca update` switches to
delete-then-insert per affected TU and stops rewriting untouched rows.

## Watcher: `ca watch`

`ca watch` keeps a database fresh while agents query it. It polls source/header
`size+mtime` (portable; native inotify/FSEvents backends are future work),
debounces bursts into one change set, maps the change set onto translation units
with the same planner as `ca update`, and runs the incremental update path in
process.

```sh
# one poll cycle (tests/scripts); prints one JSON object with --json
ca watch --repo <repo> --db ca.db --compdb <compdb> --once --json

# keep watching (SIGINT/SIGTERM stop after the current cycle)
ca watch --repo <repo> --db ca.db --compdb <compdb> --interval 5 --debounce 2

# manual batch (same semantics as ca update, watch-style metrics)
ca watch --repo <repo> --db ca.db --compdb <compdb> --changed-files kernel/a.c
```

Cycle metrics (`--json` prints one JSON object per cycle):

```json
{"cycle": 2, "changed": ["kernel/syscall.c"], "changed_count": 1,
 "header_change": false, "affected_count": 1, "updated": true,
 "skipped": false, "skip_reason": "", "compdb_stale": false,
 "cache_hits": 45, "cache_misses": 1, "db_size": 4702208,
 "deep_invalidated": true, "elapsed_seconds": 0.359, "errors": []}
```

Behaviour and limits:

- **State**: `<db>.watch.json` stores the last size+mtime snapshot and is only
  advanced after a successful update; a failed cycle keeps the old snapshot so
  the same change set is retried next round.
- **Single instance**: `<db>.watch.lock` (flock). A second watcher exits with
  code 2. Queries are unaffected (SQLite WAL; watcher is the only writer).
- **Header change**: TUs whose transitive includes contain the header are
  re-parsed (dependency-aware, same as `ca update`); unrelated TUs stay cache
  hits. Build files and indexes without include facts still fall back to the
  full cache-clear rebuild.
- **Added/deleted files not in the compile database**: the compile database
  cannot know them, so the cycle reports `compdb_stale`, skips the update and
  records an error; regenerate the compile database and restart the watcher.
- **Database rewrite**: per-TU replace is still pending in the store, so a cycle
  rewrites the whole database file from cached records (parse/analyze work is
  incremental, writing is not). Keep `--interval`/`--debounce` reasonable on
  very large databases; a full `ca index` is often better after mass changes.
- **Failures**: per-cycle exceptions are recorded in `errors`, never kill the
  daemon; SIGINT/SIGTERM stop after the current cycle and release the lock.

Minimal systemd unit (Linux server):

```ini
[Unit]
Description=CA incremental index watcher
After=network.target

[Service]
User=yu
WorkingDirectory=/home/yu/CA_OH/ca
Environment=PYTHONPATH=/home/yu/CA_OH/ca/src
Environment=CA_LIBCLANG_LIBRARY=/home/yu/CA_OH/openharmony/prebuilts/clang/ohos/linux-x86_64/llvm/lib/libclang.so.15
ExecStart=/home/yu/CA_OH/venv/bin/python -m ca watch \
    --repo /home/yu/CA_OH/openharmony \
    --db /home/yu/CA_OH/openharmony.db \
    --compdb /home/yu/CA_OH/openharmony/out/rk3588/compile_commands.json \
    --interval 10 --debounce 2
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

## Failure handling

- A failed `update` leaves the cache intact; the next run retries the same
  affected TUs. The database is only replaced after the pipeline finishes
  `finalize()`.
- Corrupt cache entries are removed automatically.
- Header/build-file changes force a full re-parse instead of risking stale
  results.
- Batch limits protect against a mass change (e.g. `git checkout`) turning into
  thousands of single-file updates; use `ca index` for those.

## Validation

`ca update` is covered by end-to-end tests: dry-run plan, changed-TU re-parse
with unchanged TUs served from cache, and header dependency-aware invalidation.
`tests/test_e2e_includes.py` covers direct/transitive includes, only-dependent
re-parsing (unrelated TUs cache hits), unrelated-header no-op, the no-include
fallback and `ca watch --once` dependency metrics. `tests/test_e2e_watch.py`
adds baseline snapshot, source edit incremental update, header update, added file
missing from the compile database -> `compdb_stale` skip, deep status
invalidation, lock conflict, and failed-update snapshot retention. See
`tests/test_e2e_graph_mcp.py` and `tests/test_e2e_pipeline.py` as well.
