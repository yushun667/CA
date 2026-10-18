# Phase 1 archive — MVP pipeline

Completed: 2026-10-17
Goal: turn the project goal into a measurable end-to-end analyzer on real
operating-system source corpora.

## Delivered

- Contracts: records, stable IDs, inter-module interfaces, package scaffold.
- `ca.build`: compile_commands loading, compiler-wrapper capture, Linux kbuild
  `.cmd` reconstruction, explicit synthesized fallback; real xv6 RISC-V compile
  database captured (46 TU, replayable).
- `ca.parse`: libclang AST extraction (functions/types/fields/calls/locations),
  cross-TU stable IDs, RISC-V/ARM target inference, GCC sysroot handling;
  xv6 46/46 TU parse with zero diagnostics.
- `ca.analyze`: CFG, branch conditions, assignments, return paths, resource
  operations and intraprocedural leak/double-release findings; guard-pairing and
  loop-termination tuning cut xv6 findings 163→2 (both known cross-function FPs)
  and FreeRTOS 16→0.
- `ca.store`: 13-table SQLite schema, batched WAL ingest, 17 indexes + ANALYZE,
  full query API and JSONL export.
- `ca.pipeline` + CLI (`index/query/stats/export/version`), e2e tests, accuracy
  harness, parse/DB benchmarks, bilingual user guide.

## Measured baseline (macOS arm64, 18 cores, Python 3.11, libclang 18.1.1)

- xv6 (46 TU, real compdb): 0.79 s wall; unique 394 functions / 2,829 calls /
  1,423 branches / 1,679 assignments / 339 returns / 575 resource ops / 2
  findings; DB 4.2 MiB; benchmark 66.2 TU/s, 32.3k LOC/s.
- FreeRTOS-Kernel (10 TU, real CMake compdb): 201 functions / 368 calls / 554
  branches / 25 resource ops / 0 findings; DB 2.0 MiB.
- Accuracy: 7/7 categories precision = recall = F1 = 1.0 on labeled fixture.
- Database: 157k synthetic rows at 528,933 rows/s; 6.86M rows at 158,153 rows/s
  (1.7 GiB), indexed point queries sub-millisecond; substring search 60–75 ms
  (fixed in phase 2 with FTS5).
- Tests: 126 passed; `pytest tests/test_e2e_*.py -q` 14 passed; qizai validator
  green.

## Artifacts and references

- Architecture: [../architecture/overview.md](../../architecture/overview.md)
- Decisions: [../decisions/active/AGENTS.md](../../decisions/active/AGENTS.md)
- Resolved issue:
  [xv6 compdb parse failure](../../issues/resolved/2026-10-17-xv6-compdb-parse-failure.md)
- User guide: [docs/usage.md](../../../docs/usage.md)
