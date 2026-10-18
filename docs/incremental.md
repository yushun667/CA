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

- changed/affected TUs are parsed/analyzed again (cache misses);
- unchanged TUs keep their existing database rows (they are cache hits and are
  not parsed, replayed or rewritten);
- only the affected TUs' rows are replaced in place (`remove_units` cascade +
  insert in one transaction), including incremental FTS5 maintenance;
- cross-TU callback candidates are refreshed after the write when callback
  registrations or indirect call sites exist.

The command prints the plan and the cache statistics:

```text
[update] affected 2/1682 TUs (header_change=False), cache=/data/.ca-cache
[update] db_write: incremental tus=2 rows -136/+141 fts -9/+10
[update] callback_refresh: rerun units=1682 registrations=391 candidate_edges=196 links=196
[update] cache hits=1680/1682 misses=2 (unaffected TUs left untouched)
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
[update] cache hits=1680/1682 misses=2 (unaffected TUs left untouched)
```

Changed headers that no indexed TU includes are a no-op (`ca update` exits 2
with "not included by any indexed TU"). Databases built before include facts
(or with `record_scope`/parser versions that did not collect them) fall back to
the previous full-cache-clear behaviour with an explicit warning. `ca watch`
uses the same mapping and reports `dependency_aware`, `fallback_full`,
`affected_count`, `cache_hits` and `cache_misses` in its per-cycle JSON.

## In-place database writes

`ca update` no longer removes and rebuilds the database. It parses/analyzes only
the affected translation units and applies them with the store's per-TU
delete+insert primitives (`remove_units`/`replace_units`): the cascade removes
the TU's `tus`/functions/types/fields/calls/CFG/data-flow facts/call targets/
callbacks/links/deep status/includes, then writes the new records in one
transaction. Untouched TUs keep their existing row ids and contents, and the
FTS5 index is maintained incrementally (delete/insert for touched functions;
no whole-database `finalize()`).

Build files, databases without include facts, and `analyze_scope=all` still use
the conservative full-rebuild path (the latter because definition ownership is
corpus-wide). Header-inline definitions are attributed to the first TU that
inserted them, so the update also includes contributing TUs that own rows for a
changed header, not only the dependency-plan TUs.

After the write, callback candidates are refreshed when the database has
callback registrations or indirect call sites: the existing callback pass reruns
with `finalize=False`, and candidates linked only to replaced registrations are
removed first so no stale edges survive. That rerun reparses the translation
units for the callback facts; it is skipped when no callback data exists.

Update/watch metrics:

```json
{"db_write": {"mode": "incremental", "tu_count": 2,
              "removed_rows": 136, "added_rows": 141,
              "fts_deleted": 9, "fts_inserted": 10},
 "incremental_rows": {"removed": 136, "added": 141},
 "callback_refresh": {"mode": "rerun", "units": 1682, "registrations": 391,
                      "candidate_edges": 196, "links": 196}}
```

`db_write.mode` is `incremental`, `full_rebuild`, `noop` or absent for errors;
`incremental_rows` reports the affected-TU row deltas and
`callback_refresh.mode` is `rerun`/`skipped`/`error` with counts and elapsed
time. Failed incremental writes roll back the transaction and leave the old rows
in place; a failed callback refresh is reported as a warning, the index rows
remain valid and the next full `ca index`/`ca resolve-callbacks` can repair the
candidate set.

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
 "db_write": {"mode": "incremental", "tu_count": 1, "removed_rows": 63,
              "added_rows": 64, "fts_deleted": 3, "fts_inserted": 3},
 "incremental_rows": {"removed": 63, "added": 64},
 "callback_refresh": {"mode": "skipped", "reason": "no callback registrations or indirect calls"},
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
- **In-place writes**: a cycle only replaces the affected TUs' rows; untouched
  rows and their ids stay stable. Callback registrations/indirect calls trigger
  a callback refresh pass (reparses TUs for AST-level facts) after the write;
  cycles without callback data skip it. Mass changes and build files still use
  the full rebuild path, so `ca index` remains the right tool for those.
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

- A failed incremental update rolls back the open transaction and leaves the
  old rows intact; the cache remains usable and the next run retries the same
  affected TUs.
- Corrupt cache entries are removed automatically.
- Header/build-file changes force a full re-parse instead of risking stale
  results.
- Batch limits protect against a mass change (e.g. `git checkout`) turning into
  thousands of single-file updates; use `ca index` for those.

## Validation

`ca update` is covered by end-to-end tests: dry-run plan, in-place
`replace_units` writes, changed-TU re-parse with untouched TU rows/ids, FTS
search after the update, callback-candidate equality with a fresh full index,
header dependency-aware invalidation and the full-rebuild fallback.
`tests/test_e2e_includes.py` covers direct/transitive includes, only-dependent
re-parsing (unrelated TUs cache hits), unrelated-header no-op, the no-include
fallback and `ca watch --once` dependency metrics;
`tests/test_e2e_incremental_write.py` covers source/header in-place replacement,
FTS, callback refresh and watch metrics. `tests/test_e2e_watch.py`
adds baseline snapshot, source edit incremental update, header update, added file
missing from the compile database -> `compdb_stale` skip, deep status
invalidation, lock conflict, and failed-update snapshot retention. See
`tests/test_e2e_graph_mcp.py` and `tests/test_e2e_pipeline.py` as well.
