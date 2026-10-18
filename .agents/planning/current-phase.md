# Current phase breakdown

Current phase: 3. OpenHarmony-first agent platform

Goal: make CA usable by AI agents and humans against real OpenHarmony C/C++
sources: offline + incremental indexing, callback/function-pointer resolution and
call-graph queries, a multi-repo workspace/variant data model, and an MCP query
server on the source machine. Phase 3 closes when its core functions are
implemented and **functionally verified** on the OpenHarmony dsoftbus and camera
repositories; that functional verification is now complete. Per user direction, full-corpus acceptance is deferred until all
features are complete; those repositories are the functional-verification corpus,
not a phase-3 acceptance gate.

## Completion conditions

- **OpenHarmony corpus**: real C/C++ `compile_commands.json` from the working
  copy `/home/yu/CA_OH/openharmony`; original `~/OH_6.1/src` stays read-only.
- **Offline + realtime incremental**: offline batch builds the snapshot;
  realtime update reparses only affected TUs. Header dependencies must invalidate
  dependent TUs, not just the main TU source.
- **Call intelligence**: indirect calls get callback/function-pointer candidate
  target sets (unordered possible targets); `ca graph` merges direct + candidate edges for
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
| 8 | C++ virtual/dynamic dispatch + general indirect-call candidates | done | - |
| 9 | Header-dependency-aware incremental indexing/watch | done | - |
| 10 | Workspace/project/variant CLI/MCP exposure (optional) | pending | - |
| 11 | Functional verification on OH dsoftbus + camera repositories + docs numbers | done | - |

Functional verification (2026-10-18) used only dsoftbus (1,287 TU), camera (475
TU) and their union (1,762 TU): 91,858 / 27,261 / 115,085 functions; camera
virtual `Command::Do`→`Executing` returns 29 candidates with direct edges kept
(no-override control returns none); dsoftbus `NetBuilderMessageHandler` exposes
16 registration-table candidate edges; header dependency-aware update/watch
re-parses only the 6 dependent TUs, unrelated headers are no-ops, MCP (18 tools)
was verified on all three DBs. See [../docs/functional-verification.md](../../docs/functional-verification.md).
The optional workspace/variant selector is not implemented; full-corpus
acceptance is still deferred.

## Implementation status

- **C++ virtual/dynamic dispatch**: implemented (metadata on core records, store
  schema v7 `virtual_overrides`/`type_bases`, same-TU analyze candidates and
  cross-TU query-time merge of the possible-target set; all overrides within depth ≤3
  are kept with depth/relation evidence); multi-inheritance and unresolved dynamic
  types remain bounded heuristics (docs/cross-tu-virtual-dispatch.md).
- **Header-dependency incremental**: implemented; parser records direct includes
  plus header→header edges, store persists the transitive closure in
  `tu_includes` (schema v6→7), `ca update`/`ca watch` invalidate only dependent
  TUs and fall back safely for old indexes/build files (docs/incremental.md).
- **Workspace/variant surface (optional)**: not implemented; store model exists.
- **Functional verification**: completed on dsoftbus/camera only
  (docs/functional-verification.md); full-corpus acceptance remains deferred.

Full-corpus sharded acceptance, S2/S6/S8 completion and the final acceptance
report are **deferred**; they are not phase-3 functional gates. Phase 4+ (other
OS trees/languages, DWARF, additional UI) needs separate scope confirmation.
