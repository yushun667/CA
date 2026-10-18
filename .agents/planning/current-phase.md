# Current phase breakdown

Current phase: 3. OpenHarmony-first agent platform

Goal: make CA usable by AI agents and humans against real OpenHarmony C/C++
sources: offline + incremental indexing, callback/function-pointer resolution and
call-graph queries, a multi-repo workspace/variant data model, and an MCP query
server on the source machine. Phase 3 closes when its core functions are
implemented and **functionally verified** on the OpenHarmony dsoftbus and camera
repositories. Per user direction, full-corpus acceptance is deferred until all
features are complete; those repositories are the functional-verification corpus,
not a phase-3 acceptance gate.

## Completion conditions

- **OpenHarmony corpus**: real C/C++ `compile_commands.json` from the working
  copy `/home/yu/CA_OH/openharmony`; original `~/OH_6.1/src` stays read-only.
- **Offline + realtime incremental**: offline batch builds the snapshot;
  realtime update reparses only affected TUs. Header dependencies must invalidate
  dependent TUs, not just the main TU source.
- **Call intelligence**: indirect calls get callback/function-pointer candidates
  with confidence; `ca graph` merges direct + candidate edges for
  forward/reverse/path/depth queries in table/JSON/DOT. C++ virtual/dynamic
  dispatch must be covered in addition to registration-style callbacks.
- **Agent interface**: `ca mcp` exposes symbol/reference/call-graph/context
  queries (18 read-only tools) to debug/coding/test-generation agents; stdio,
  HTTP and `--serve-url` proxy verified.
- **Data model**: `CallTargetRecord` persisted; workspace/project/variant
  metadata stored with a documented sharding/federation path. A user-facing
  workspace/variant selector is optional but preferred.
- **Quality gates**: tests and qizai validator pass; README/docs record current
  functional numbers and limits. Functional verification uses only the OpenHarmony
  dsoftbus and camera repositories; full-corpus acceptance is a later milestone.

## Steps

| Step | Deliverable | Status | Blocked by |
|---|---|---|---|
| 1 | Server reconnaissance, working copy, CA venv, `CallTargetRecord` contract | done | - |
| 2 | OpenHarmony real C/C++ compdb and corpus | done | - |
| 3 | MCP server, `ca graph`, offline/incremental interaction | done | - |
| 4 | Callback/function-pointer candidate resolution (registration patterns) | done | - |
| 5 | `call_targets`, graph queries, workspace/variant model | done | - |
| 6 | OpenHarmony C/C++ parse compatibility (matched libclang backend) | done | - |
| 7 | Integration, unified backend, read-only UI, P0 query UX, agent evaluation | done | - |
| 8 | C++ virtual/dynamic dispatch + general indirect-call candidates | pending | - |
| 9 | Header-dependency-aware incremental indexing/watch | pending | - |
| 10 | Workspace/project/variant CLI/MCP exposure (optional) | pending | - |
| 11 | Functional verification on OH dsoftbus + camera repositories + docs numbers | pending | steps 8-9 |

## Implementation plan for the remaining gaps

- **C++ virtual/dynamic dispatch**: extend type extraction with base classes and
  virtual method/override relationships; persist overrides; when a call target
  is a virtual method, emit candidate edges to statically resolvable overrides
  (same-TU and cross-TU), with lower confidence than direct calls; merge into
  `call_targets` and graph queries. Multi-inheritance and unresolved dynamic
  types remain bounded heuristics.
- **Header-dependency incremental**: record direct includes per TU during
  parsing and persist a TU→header dependency map; on `update`/`watch`, resolve
  changed files to dependent TUs and invalidate only those cache entries before
  reparsing. Functional test: editing a header reparses dependents, while an
  unrelated header skips them.
- **Workspace/variant surface (optional)**: CLI/MCP filters for project and
  variant; no storage change needed.
- **Functional verification**: use only the OH dsoftbus and camera repositories,
  not the full 8-shard acceptance run; record command, counts and limits in README/docs.

Full-corpus sharded acceptance, S2/S6/S8 completion and the final acceptance
report are **deferred**; they are not phase-3 functional gates. Phase 4+ (other
OS trees/languages, DWARF, additional UI) needs separate scope confirmation.
