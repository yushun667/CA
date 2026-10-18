# Phase progress

Overall: phase 3 functional completion is in progress (OpenHarmony first).
Phase 1/2 are archived and delivered; the local suite is 421 passed / 3 skipped.
Full-corpus acceptance is deferred until all features are complete, per user
direction; OH is the representative functional-verification corpus, not a
phase-3 acceptance gate.

## Done

- Server setup and read-only OH working copy at `/home/yu/CA_OH/openharmony`;
  real compdb (214,543 commands / 3.4 GB) and matched libclang 15 backend.
- Offline index pipeline ran on 5/8 shards as **functional evidence only**
  (41,897 TUs, 82 failures, 11.23 GB); this is not acceptance.
- AST extraction: functions/types/fields/calls/source locations; CFG, branches,
  assignments, returns and resource operations stored in SQLite (schema v5).
- Cross-TU callback candidates and direct/candidate call-graph queries;
  workspace/project/variant model and federated shard reads.
- Layered L0/L1/L2 deep facts (`ensure_deep`/`prewarm`/`deep_status`); polling
  `ca watch` and `ca update --changed-files` with main-TU incremental cache.
- MCP server: 18 read-only tools, stdio/HTTP and `--serve-url` proxy.
- Unified `ca serve` backend (query API, generic jobs/SSE, `/mcp`); read-only
  web UI with query explorer, call graph, source/CFG panes and remote mode.
- P0 query UX (`brief`, compact/fields, batch, header declarations); 45-run
  real-agent matrix on xv6 with zero hallucinated locations.
- AI explanation feature was added and then removed end to end; no explain or
  sampling endpoints/tools remain.

## Remaining phase-3 core functions

- C++ virtual/dynamic dispatch and general function-pointer candidates beyond
  registration-style callbacks.
- Header-dependency-aware incremental indexing: current cache tracks only the
  main TU source, so header edits miss dependent TUs.
- Optional workspace/project/variant CLI/MCP selector; the store model exists
  but has no user-facing surface.
- Deploy the latest code to the source server and run functional checks against
  an OH shard subset (not full acceptance).

## Next

Implement the two required gaps, functionally verify them on representative OH
corpora, update README/docs numbers, then confirm phase 4 scope (other OS trees
and languages). Full 8-shard acceptance stays deferred until all features are
complete.

## Risks and blockers

- C++ virtual dispatch needs class hierarchy/override data and new candidate
  generation; large C++ code may produce noisy candidates.
- Header dependency tracking needs include-graph storage and cache invalidation;
  false negatives are correctness bugs for incremental indexing.
- OH giant C++/IDL TUs are slow and memory-heavy (S2 partial archive exists).
- Deep coverage is partial by design; findings are intra-function heuristics.
- Callback candidates are registration-based; MCP/UI have no auth; SQLite has a
  single writer.
