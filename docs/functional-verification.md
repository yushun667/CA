# Phase-3 functional verification: OpenHarmony dsoftbus + camera

Status: phase-3 functional verification (2026-10-18), scope limited by the user
to the OpenHarmony **dsoftbus** and **camera** repositories. The full 8-shard
acceptance run and the S2/S6/S8 shard completion are explicitly deferred; this
document is not an acceptance report for the whole OpenHarmony tree.

## Scope and environment

- Code baseline: `28e1fdc` (core schema v2, store schema v7; virtual-dispatch
  query-time merge fix included).
- Source machine: `/home/yu/CA_OH/openharmony` (read-only working copy), Python
  venv `/home/yu/CA_OH/venv`, matching OH **libclang15**
  (`CA_LIBCLANG_LIBRARY=.../llvm/lib/libclang.so.15`), `PYTHONPATH` includes
  `/home/yu/CA_OH/pylib15:/home/yu/CA_OH/ca/src`.
- Index parameters: `ca index --repo /home/yu/CA_OH/openharmony --compdb
  <subset>.json --db <db> --jobs 6 --record-scope main_file --analyze-scope
  main`; pool settings `CA_POOL_RECYCLE_CHUNKS=2`, `MALLOC_ARENA_MAX=2`,
  `PYTHONMALLOC=malloc`.
- Corpora: `/home/yu/CA_OH/corpus/openharmony/functional/{dsoftbus,camera,
  dsoftbus_camera}.json`; baseline DBs in `/home/yu/CA_OH/index/functional/`;
  incremental copies `dsoftbus_incr.db`/`camera_incr.db`; evidence
  `index/functional_dsoftbus_camera.md`, `metadata.json`, `validation.json`.

## Index and query baseline

| corpus | TUs | functions | calls | types | call_targets | tu_includes | parse failed | DB |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| dsoftbus | 1,287 | 91,858 | 414,309 | 22,218 | 389 (209 indirect + 180 virtual) | 109,228 | 1 | 577 MB |
| camera | 475 | 27,261 | 119,764 | 12,050 | 8 | 69,021 | 0 | 188 MB |
| combined | 1,762 | 115,085 | 526,914 | 27,626 | 397 | 178,249 | 1 | 752 MB |

`stats` reports CFG/branch/resource/deep coverage too (dsoftbus: 41,558
branches, 194,268 CFG blocks, 41,115 deep_ready; camera: 8,357 branches, 51,102
CFG blocks, 10,342 deep_ready; combined deep_ready 51,457 = 44.7%). Degraded
TUs (missing generated headers/vendor SDK) are reported and do not abort the
run; parse failures are 1/0/1 for the three corpora.

Queries verified with the CLI on each DB:

```sh
ca stats --db .../dsoftbus.db --json
ca query functions NetBuilderMessageHandler --db .../dsoftbus.db --json
ca query detail NetBuilderMessageHandler --db .../dsoftbus.db --json
ca graph --from NetBuilderMessageHandler --db .../dsoftbus.db --depth 1 --format json
```

`search` returns the expected function/file/line, `detail` returns
function/branches/assignments/returns/resources/findings and `deep_ready=true`
for the analysed symbol, and `graph` returns bounded nodes/edges. MCP was
verified on all three DBs with stdio and HTTP (`scripts/verify_shard_mcp.py`):
18 tools, `detail`/`graph`/`deep_status`/`ensure_deep`/`prewarm` ok; stdio call
≈0.15 s per session (`verify_mcp_{dsoftbus,camera,dsoftbus_camera}.json`).

## C++ virtual dispatch candidates

Camera contains a real cross-TU virtual dispatch chain used as the domain
sample: `OHOS::CameraStandard::DeferredProcessing::Command::Do` calls
`Command::Executing` (27 known overrides in 10 files); after the virtual query
fix (`store/virtual_dispatch.py`: LIMIT binding + recursive direction) the CLI
returns:

- `ca query callees fn:6e2268c6d1c202d014c3f31e --db camera.db --json`:
  37 rows = 8 direct call edges (including direct virtual `Executing`, kept with
  `source="call"`) + **29 `virtual_candidate` edges** (27 depth-1, 2 depth-2)
  across 10 different TU files (`video_process_command.cpp`, `photo_command.cpp`,
  `notify_video_job_command.cpp`, `service_died_command.cpp`, ...).
- reverse check `ca query callers <override-id> --db camera.db`: the override
  `Executing` (video_process_command.cpp:45) reports 1 `virtual_candidate`
  caller: `Command::Do` at command.cpp:37.
- graph: `ca graph --from <Command::Do id> --depth 1 --format json` returns 37
  nodes / 37 edges, 29 candidate edges; MCP `graph` returns the same data
  (18 tools).
- no-override control: `IsBframeSupported` → `GetCapability`
  (`dynamic_libs/av_codec/src/av_codec_adapter.cpp:38`) has no override
  metadata; the query returns only the direct virtual edge and **0
  virtual_candidate rows** (no invented edges).

Combined-corpus query-time statistics (read-only, same fixed code): 7,672
virtual call sites, **3,784 candidate edges** for 1,531 calls (20%), depth1
3,739 / depth2 45; only 201 edges (5.3%) are test/gtest-related. dsoftbus-only
materialised `call_targets` are mostly gtest (`TestBody`→`what`) because its
domain virtuals are rare; query-time merging now surfaces domain candidates as
well. Every known override within depth ≤3 is included in the possible-target
set; candidates carry `reason`/`source="virtual_candidate"` with override
depth/relation as evidence, and direct edges are never replaced. Candidates are
not ranked or scored.

## Registration-based indirect calls

dsoftbus `NetBuilderMessageHandler`
(`core/bus_center/lnn/net_builder/src/lnn_net_builder_process.c:893`) dispatches
through the global table at line 906 (`g_messageProcessor[msg->what]`). The
graph/query returns **16 `indirect_candidate` edges** with the registration
reason (e.g. `ProcessDeviceVerifyPass`, `ProcessMasterElect`,
`ProcessSetReSyncDeviceName`, ...); the direct call edges remain the
`HiLogPrint`/`__builtin_strrchr` calls. This validates the registration-table
path; pointer arithmetic/dynamic indices remain heuristic and candidate-only.

## Header-dependency incremental update/watch

Incremental copies were warmed once with `ca index --incremental` (cache under
`index/functional/.ca-cache`); real headers were backed up, edited to append a
unique declaration, verified, then restored (baseline DBs untouched).

- camera `common/utils/camera_simple_timer.h` (6 dependent TUs):
  `ca update --changed-files` → `include dependencies: 6/475`,
  `dependency-aware: invalidated 6 cache entries`, `cache hits=426/475
  misses=49` (the 6 dependents plus 43 TUs without a usable cache entry), new
  declaration queryable in `camera_incr.db`.
- dsoftbus `core/common/include/softbus_bitmap.h` (6 dependent TUs):
  dry-run `dependency_aware=true, fallback_full=false, affected 6`; actual
  update → `include dependencies: 6/1287`, invalidated 6, `cache hits=1252/1287
  misses=35` (dependents + failed/degraded TUs without cache), new declaration
  queryable. The cache hit rate is dominated by the 1,281 unchanged TUs.
- unrelated header (added, included by no indexed TU): `ca update
  --changed-files` exits **2** with "changed header(s) are not included by any
  indexed TU" and no re-parse. An index without include facts falls back to the
  old full-cache clear with a warning (covered by tests).
- `ca watch --once --json` on camera: baseline cycle ok; after the header edit
  it reports `header_change=true`, `dependency_aware=true`,
  `fallback_full=false`, `affected_count=6`, `cache_hits=426`,
  `cache_misses=49`, `updated=true`, `deep_invalidated=true`, elapsed 181.7 s
  (whole-DB rewrite; parse/analyze is incremental, writing is not yet per-TU).
- Store coverage: `tu_includes` 69,021 camera / 109,228 dsoftbus / 178,249
  combined rows; include dependency map survives schema upgrade (store v7) and
  old databases fall back safely.

## MCP / UI

- MCP stdio/HTTP verified on the three DBs: 18 tools; `detail`, `graph`,
  `deep_status`, `ensure_deep`, `prewarm` return stable envelopes; MCP graph on
  camera shows the same 29 virtual candidates as the CLI. No AI explanation
  endpoint/tool exists (removed feature).
- UI (`ca ui`) was not exercised in this server-only pass; the same read-only
  API and MCP backend are covered by the local UI tests. Start it with
  `ca ui --db ...` (embedded) or `ca ui --serve-url ...` if desired.

## Quality gates

- Local full suite: `pytest -q` → **455 passed / 3 skipped** (parser/include/
  virtual/store/e2e tests included).
- `scripts/check_artifacts.py` → i18n pairs=70, benchmark results=17, errors=0;
  `.agents/validate.py --mode structure` → valid; precommit validation run on a
  temporary staged index (real worktree not git-added) → valid + expected
  review/gate prompts.
- No `git add/commit`; no index/update/watch ran against baseline DBs
  (`dsoftbus.db`, `camera.db`, `dsoftbus_camera.db`) — they were only queried
  (SQLite may touch WAL/meta mtime on open). Header experiments used the
  `*_incr.db` copies and every edited header was restored.

## Limitations

- Only dsoftbus/camera (and their union) were verified; no 8-shard full OH
  acceptance, and S2/S6/S8 remain deferred.
- Virtual candidates are possibilities (dynamic type unknown); multiple
  inheritance/templates/no override metadata produce conservative/no edges;
  virtual destructors are not tracked. dsoftbus-only materialised candidates
  are test-heavy; use query-time results/combined corpus for domain checks.
- 1 parse failure + several degraded TUs per corpus (missing generated
  headers/vendor SDK) are reported but out of scope.
- Header incrementality still rewrites the whole DB per update; `ca watch`
  cycles with a header change are noticeably slower than a cache-hit source
  edit even though only dependent TUs re-parse.
- MCP/server are loopback/no-auth; no cross-shard transactions.

## Reproduce

```sh
# index (already done; needs OH libclang15 env)
JOBS=6 ./functional_subset_index.sh
# queries
ca stats --db index/functional/dsoftbus.db --json
ca query functions NetBuilderMessageHandler --db index/functional/dsoftbus.db --json
ca graph --from fn:6e2268c6d1c202d014c3f31e --db index/functional/camera.db --depth 1 --format json
# header incremental (backup header first; use *_incr.db, not baseline DBs)
ca update --repo $OH --db index/functional/camera_incr.db \
  --compdb corpus/openharmony/functional/camera.json --jobs 6 \
  --changed-files foundation/multimedia/camera_framework/common/utils/camera_simple_timer.h
ca watch --repo $OH --db index/functional/camera_incr.db \
  --compdb corpus/openharmony/functional/camera.json --json --once
```
