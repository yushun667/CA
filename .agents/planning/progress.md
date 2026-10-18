# Phase progress

Overall: phase 3 acceptance in progress (OpenHarmony-first). Phase 1/2 are
archived and delivered; local suite is 288 passed / 3 skipped and server tests
stay green while acceptance runs.

## Done in phase 3

- Server reconnaissance: OpenHarmony 6.1 tree (`~/OH_6.1/src`, 500 repo
  projects, product opi5plus/board rk3588, 105 GB source, original read-only),
  Ubuntu 22.04 host with 24 cores / 62 GB RAM / 1.2 TB free, Docker builder
  images and prebuilt gn/ninja/clang available.
- Working copy started at `/home/yu/CA_OH/openharmony` (rsync from the original
  tree, excluding build output and repo object store); original tree untouched.
- CA deployed to `/home/yu/CA_OH/ca`; server venv at `/home/yu/CA_OH/venv`
  (Python 3.11) with libclang working and `ca` console script installed.
- Contract extension: `CallTargetRecord`, `AnalysisResult.call_targets` and
  `call_target_id` added for callback/function-pointer candidate edges.
- Phase-3 task board created (tasks 11-15) and five agents restarted with
  disjoint scopes.
- Callback/function-pointer candidate resolution delivered: registration,
  arrays/designated initializers, parameter passing, typedef chains and returned
  pointers; labeled fixture precision = recall = 1.0; xv6 yields 22 candidate
  edges from the syscall table (confidence 0.65, may-call semantics).
- Call-graph CLI delivered: forward/reverse/both traversal, depth/path/cycle
  queries, table/JSON/DOT; xv6 `main` depth1 = 54 nodes/178 edges, depth2 =
  94/375 with one cycle; direct and candidate edges are merged with
  kind/confidence/source.
- `call_targets`, `workspaces/projects/variants`, graph query helpers and
  `store_schema_version=2` delivered with idempotent old-DB upgrade;
  100k-node/300k-edge graph queries stay sub-millisecond at depth 1 and
  ~0.33 ms for both-direction depth 2.
- MCP server delivered and verified on the source server: stdio + HTTP
  JSON-RPC, 12 read-only tools, stable envelopes/pagination/error codes;
  `scripts/server_bootstrap.sh` reports SERVER_OK.
- Offline + incremental interaction delivered: `ca update --changed-files`
  reparses only changed TUs, per-TU cache hits for unchanged ones, header
  changes clear the cache; watcher/daemon design documented.
- Linux portability fix: tests no longer hard-code a macOS `.dylib`; server
  pytest is 242 passed / 1 expected relocation failure / 3 skipped.
- Local suite: 246 tests pass; server bootstrap and MCP smoke pass.

## In progress

- Phase-3 acceptance: sharded offline index of the unique-source corpus
  (65,023 source TUs / 70,346 unique files; 204,422 source commands) with the
  matched libclang 15 backend, `--record-scope main_file --analyze-scope main`,
  2 shards x jobs6 and wave-level pool recycling; ETA about 3.6 h. A shard counts
  only when it has `.done` + exit 0 + stats.
- Failure classification: S4 has a stable ~27-29% failed/degraded band (missing
  generated headers, systemic); S3's rate is being attributed. The final report
  groups failures into target/environment, not silently dropped.
- Completed capabilities: real compdb (214,543 commands; 3.4 GB), matched
  backend, call graph/callbacks, schema v3 + deep_status, MCP 15 tools, layered
  L0/L1/L2, 45-run real-agent matrix (0 hallucinated locations), decisions and
  the OOM/recycle incident record.
- Remaining: 8-shard stats/graph/MCP summary, acceptance report, commit, then the
  agent-driven P0 optimization and OH agent-eval extension.

## Next

- After acceptance: commit phase-3 baseline (code/tests, then docs/acceptance
  results), implement P0 improvements (compact/projection, batch queries,
  `ca brief`, header declarations, returns/dominance/constants) and re-run the
  same 45-run matrix with a context-median <=23.8 KB and no F1 regression gate.
- Then confirm phase 4 scope (other OS trees/languages; human UI last; optional
  PCH/native indexer).

## Risks and blockers

- Deep analysis of every TU is infeasible; the layered lazy path is the
  architecture, and deep coverage is reported separately from complete L0.
- The real-agent matrix shows CA improves structured fact coverage but does not
  consistently reduce context: `detail` verbosity, missing header declarations,
  collapsed return blocks, missing lock dominance/constants/refcounts/IRQ are
  known gaps.
- Callback candidates cover same-TU registrations only; cross-TU tables and
  C++ virtual dispatch remain open.
- MCP is LAN/SSH-only with no authentication; human UI is a later phase.
