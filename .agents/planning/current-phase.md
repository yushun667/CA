# Current phase breakdown

Current phase: 3. OpenHarmony-first agent platform

Goal: make CA usable by AI agents and humans against real OpenHarmony C/C++
sources: offline full indexing plus realtime incremental updates, callback /
function-pointer resolution and call-graph queries, a multi-repo
workspace/variant data model, and an MCP query server running on the source
machine. The human UI is deliberately last.

## Completion conditions

- **OpenHarmony corpus**: a real C/C++ `compile_commands.json` is generated from
  the current OpenHarmony snapshot (product opi5plus / board rk3588) inside the
  working copy `/home/yu/CA_OH/openharmony`; the original `~/OH_6.1/src` stays
  read-only. Dev validation uses a subset of repos; acceptance uses the full
  C/C++ corpus.
- **Offline full + realtime incremental**: an offline batch index builds the
  full baseline snapshot; a realtime update path reparses only changed
  translation units against that snapshot. No realtime full-tree parsing.
- **Call intelligence**: indirect calls get callback/function-pointer candidate
  targets with confidence; `ca graph` answers forward/reverse/path/depth
  queries in table/JSON/DOT form, merging direct and candidate edges.
- **Agent interface**: an MCP server (`ca mcp`) runs on the source machine and
  exposes symbol/reference/call-graph/context queries to debug, coding and
  test-generation agents; a minimal MCP client test and bilingual docs exist.
- **Data model**: `CallTargetRecord` is persisted; workspace/project/variant
  metadata is stored (500 OpenHarmony projects, product/board/version) with a
  documented sharding/federation path for full-corpus scale.
- **Quality gates**: all tests pass, the qizai validator is green, and
  docs/README carry the phase-3 numbers and limits.

## Steps

| Step | Deliverable | Status | Blocked by |
|---|---|---|---|
| 1 | Server reconnaissance, working copy, CA venv and `CallTargetRecord` contract (Lead) | done | - |
| 2 | OpenHarmony real C/C++ compdb and corpus (task-11) | done | - |
| 3 | MCP server, `ca graph`, offline/incremental interaction (task-12) | done | - |
| 4 | Callback/function-pointer candidate resolution (task-13) | done | - |
| 5 | `call_targets` persistence, graph queries, workspace/variant model (task-14) | done | - |
| 6 | OpenHarmony C/C++ parse compatibility (task-15) | done | - |
| 7 | Integration, sharded acceptance batch, governance (Lead) | in progress | - |

Phase 4+ (human UI last, other OS trees and languages) is proposed in
[roadmap.md](roadmap.md) and needs a separate scope confirmation.
