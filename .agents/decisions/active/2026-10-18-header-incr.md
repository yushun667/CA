# Decision: header dependency aware incremental indexing

Status: active
Date: 2026-10-18

## Context

`ca update`/`ca watch` cached one `ParseResult` per TU keyed by main-source
content and compile arguments; header contents were deliberately not in the key.
Any changed header (or build file) therefore cleared the entire incremental
cache and re-parsed every TU. On OpenHarmony-scale trees this made small header
edits expensive and forced full re-indexing, and `ca watch` could not refresh
after an ordinary header change.

## Decision

The parser records each TU's repo-local include facts:

- `ParseResult.include_files`: direct includes of the TU main file (repo-relative
  POSIX paths, sorted/deduplicated, no system headers, main file excluded);
- `ParseResult.include_edges`: repo-local `(parent, child)` include edges,
  including header→header.

`ca index` persists the transitive closure per TU in the store table
`tu_includes(tu_id, include_path)` (schema bump, idempotent `CREATE TABLE IF NOT
EXISTS` upgrade; `include_path` indexed). `ca update --changed-files` maps a
changed header to dependent TU ids and deletes only those TUs' cache entries;
unrelated TUs stay cache hits. Changed headers that no indexed TU includes are a
no-op. `ca watch` uses the same mapping and reports `dependency_aware`,
`fallback_full`, affected TUs and cache hits/misses.

If the index has no include facts (old database / parser without include
collection), the old behaviour is the safe fallback: clear the whole cache,
warn and re-parse all TUs. Build files (Makefile/CMakeLists, etc.) still force
the full rebuild; transitive closure is computed by the store writer from
direct includes plus include edges.

## Reasons

- Header edits are the common incremental case; scoping re-parsing to dependent
  TUs keeps update/watch fast without changing record semantics.
- Persisting the transitive closure once keeps the query trivial
  (`include_path -> tu_id`) and avoids recursive SQL or a header graph at update
  time; transitive closure is required for indirect header edits.
- Keeping the full-cache fallback preserves correctness for old indexes and
  unknown build-file changes instead of silently missing updates.
- The parser remains the owner of include extraction; the pipeline/store only
  persist and consume the frozen contract.

## Alternatives considered

- **Hash header contents into the cache key**: correct but invalidates every TU
  including the header (the current all-TU cost) and has no per-TU mapping.
- **Recursive SQL over a header edge table at update time**: more schema/query
  complexity for little benefit; the closure is small per TU and computed once
  during ingest.
- **Only direct includes**: misses changes to a header included by another
  header; rejected because correctness matters more than the modest closure work.

## Consequences

- `tu_includes` grows with unique (TU, transitive include) pairs; ingest adds a
  small closure computation per TU and disk space proportional to include facts.
- Indexes built before this change fall back to full cache clearing until
  re-indexed; the warning says so.
- Build-file changes and unknown/unmatched paths remain conservative full
  rebuilds.
- Incremental cache key version was bumped so older cached ParseResults without
  include fields are not replayed (they would otherwise persist no include facts).

## Validation and risks

`tests/test_store_includes.py` covers direct/transitive closure, dependent
queries and old-database idempotent upgrade. `tests/test_e2e_includes.py`
indexes a three-TU/header-chain fixture and checks: index writes direct+transitive
includes; a changed included header re-parses only the two dependent TUs
(`cache hits=1/3 misses=2`); a transitive deep header maps back to the TU;
unrelated headers are a no-op; a database without include rows falls back to a
full rebuild with a warning; `ca watch --once` reports dependency-aware metrics.
Risk: databases not re-indexed after the upgrade silently use fallback until
warned; build-file changes stay full rebuilds.
