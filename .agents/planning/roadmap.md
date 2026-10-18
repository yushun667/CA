# Roadmap

Status: phase 3 functional completion in progress (OpenHarmony first); the human
UI from phase 5 was delivered early. Full-corpus acceptance is deferred until
all features are complete, per user direction.

The roadmap holds phase goals, dependencies and observable completion
conditions. The current phase's steps live in [current-phase.md](current-phase.md).

| Phase | Goal | Depends on | Completion condition | Status |
|---|---|---|---|---|
| 0. Governance | Root-driven `AGENTS.md` project system | - | structure validator passes | done |
| 1. MVP pipeline | Real compile arguments + AST + CFG/data flow + SQLite + CLI on real OS corpora | 0 | xv6/FreeRTOS indexed end to end; accuracy/speed baselines recorded | done |
| 2. Precision and scale | Kernel subset, FTS5, incremental re-index, C++ coverage, packaging/CI | 1 | Linux kernel compdb + subset index; FTS5/incremental/C++ verified; CI accuracy gate | done |
| 3. OpenHarmony-first agent platform | OpenHarmony v6.1 C/C++ offline + incremental index, callback/function-pointer call graph, workspace model, MCP query server | 2 | Real OH compdb; core functions implemented and functionally verified on the OpenHarmony dsoftbus and camera repositories; call graph/candidates; MCP; workspace model; tests/validator green. Full-corpus run/acceptance deferred until all features are complete. | in progress |
| 4. Multi-tree and languages | AOSP/Yocto/Ubuntu drivers, sharded scale index, Java/Kotlin and ArkTS/TS frontends, DWARF symbolization, coverage/test mapping | 3 | Each tree has a reproducible corpus driver and functional evidence; multi-language schema in use | not started |
| 5. Human UI | Query explorer, call-graph visualization, source overlays, dashboards | 3-4 | UI covers indexing/queries/graph/context; humans operate without CLI | in progress (query explorer/call graph/source delivered; dashboards/index management pending) |

The human UI is explicitly last, per the user's prioritization; the read-only
explorer was delivered early. Status values are limited to: not started, in
progress, done, blocked. Acceptance is not a phase-3 gate until all features are
complete.
