# Roadmap

Status: phase 2 delivered on 2026-10-17; phase 3 in progress (OpenHarmony first).

The roadmap holds phase goals, dependencies, and observable completion
conditions only. The current phase's steps live in [current-phase.md](current-phase.md).

| Phase | Goal | Depends on | Completion condition | Status |
|---|---|---|---|---|
| 0. Governance | Root-driven `AGENTS.md` project system | - | structure validator passes | done |
| 1. MVP pipeline | Real compile arguments + AST + CFG/data flow + SQLite + CLI on real OS corpora | 0 | xv6 and FreeRTOS indexed end-to-end; accuracy/speed baselines recorded | done |
| 2. Precision and scale | Kernel subset, FTS5, incremental re-index, C++ coverage, packaging/CI | 1 | Linux kernel compdb + subset index; FTS5/incremental/C++ verified; CI accuracy gate | done |
| 3. OpenHarmony-first agent platform | OpenHarmony v6.1 C/C++ offline full + realtime incremental index, callback/fn-pointer call graph, multi-repo workspace model, MCP query server on the source machine | 2 | Real OH compdb; dev subset index; call graph + candidates; MCP verified; acceptance full-corpus batch; tests/validator green | in progress |
| 4. Multi-tree and languages | AOSP / Yocto / Ubuntu build drivers, sharded scale index, Java/Kotlin & ArkTS/TS frontends, debug/test enrichment (DWARF symbolization, coverage/test mapping) | 3 | each tree has a reproducible corpus driver and acceptance numbers; multi-language schema in use | not started |
| 5. Human UI | Query explorer, call-graph visualization, source overlays, dashboards | 3-4 | UI covers indexing/queries/graph/context; humans can operate without CLI | not started |

The human UI is explicitly last, per the user's prioritization.
Status values are limited to: not started, in progress, done, blocked.
Later phases must be added from project facts, not invented from a template.
