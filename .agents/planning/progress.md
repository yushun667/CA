# Phase progress

Overall: phase 3 functional scope is complete and functionally verified on
OpenHarmony dsoftbus + camera; phase 4 scope confirmation is next. Phase 1/2 are
archived and delivered; the local suite is 497 passed / 4 skipped.
Full-corpus acceptance is deferred until all features are complete, per user
direction; functional verification uses only the OH dsoftbus and camera
repositories, not a phase-3 acceptance gate.

## In progress

- Native Windows/Linux/macOS packages: CLI/MCP/browser, installers and portable
  archives. Native workflow results and downloadable artifacts remain required;
  see [binary guide](../../docs/binaries.md).

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
- Confidence scoring was removed end to end: candidates are a target set with
  kind/reason/evidence; schema v8 drops the DB fields.
- C++ virtual/dynamic dispatch: metadata persisted (store schema v7), same-TU
  candidates + cross-TU query-time merge; camera `Command::Do` returns 29
  candidates (no-override control returns none); combined corpus 3,784 candidate
  edges across 1,531 calls (20%).
- Header-dependency-aware incremental: `tu_includes` transitive closure,
  `ca update`/`ca watch` invalidate only dependent TUs; measured 6 dependent TUs
  per header, unrelated headers no-op, MCP/CLI/graph verified.
- In-place incremental DB writes: `ca update`/`ca watch` use
  `remove_units`/`replace_units` for affected TUs (untouched rows keep ids),
  incremental FTS5 maintenance, callback-candidate refresh when registrations or
  indirect calls exist, and `db_write`/`incremental_rows`/`callback_refresh`
  cycle metrics; build files/old indexes fall back to full rebuild.
- Workspace/project/variant registry: CLI CRUD + index registration, query/graph
  `--variant` routing to registered read-only DBs, MCP `workspaces`/`projects`/
  `variants` (21 tools), serve `GET /api/registry` + `POST /api/registry/active`
  atomic in-memory switching; UI selector is task-47.
- Functional verification on dsoftbus (1,287 TU), camera (475 TU) and combined
  (1,762 TU): baseline stats/search/detail/graph, MCP 18 tools, virtual/indirect
  candidates and header-incremental update/watch all pass; write-up in
  docs/functional-verification.md.
- DSH plugin (`@local/dsh-ca`): CA page/projects, 12 tools, local/remote backend,
  tasks and explanations; wheel/offline install and automatic preparation.
  Linux offline install verified; tests separate. See [guide](../../docs/dsh-plugin.md).

## Deferred / optional

- Optional workspace/project/variant UI selector (task-47); CLI/MCP/serve
  registry APIs are implemented. Not a phase-3 gate.
- Full 8-shard acceptance remains deferred by user direction; S2/S6/S8
  completion and the final acceptance report are not phase-3 functional gates.

## Next

Confirm phase 4 scope (other OS trees and languages) with the user, keep the
functional-verification numbers/docs in sync, and defer full-corpus acceptance
until all features are complete.

## Risks and blockers

- C++ virtual dispatch needs class hierarchy/override data and new candidate
  generation; large C++ code may produce noisy candidates.
- Header dependency tracking needs include-graph storage and cache invalidation;
  false negatives are correctness bugs for incremental indexing.
- OH giant C++/IDL TUs are slow and memory-heavy (S2 partial archive exists).
- Deep coverage is partial by design; findings are intra-function heuristics.
- Callback candidates are registration-based; MCP/UI have no auth; SQLite has a
  single writer.
- DSH plugin relies on Harness internal services (`webServer`/`slots`/`llm`);
  Harness upgrades need re-verification, and the remote Linux path needs
  SSH/rsync/venv on the server.
