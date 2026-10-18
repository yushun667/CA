# Decision: incremental indexing cache

Status: active
Date: 2026-10-17

## Context

Every index run re-parses and re-analyzes every translation unit. At kernel
scale this dominates wall time and makes iteration expensive (xv6 alone parses
0.67 s CPU and analyzes 1.34 s CPU per full run; kernel subsets are much
larger). Phase 2 needs a second run over an unchanged repository to be fast.

## Decision

Cache per-TU worker results (the detached `ParseResult` plus its
`AnalysisResult`s) keyed by SHA-256 over the TU source file content, the
normalized compile arguments, the snapshot schema version and the tool version.
On an incremental run, unchanged TUs skip parse/analyze and reuse cached records
when writing the database; changed TUs are re-parsed. Cache lives next to the
database (`.ca-cache/`) or in an explicit directory. A corrupted/mismatched
cache entry falls back to a full parse of that TU. Invalidation is limited to
the tracked inputs: header changes are not tracked, so users must run a full
index after header/build changes (documented).

## Reasons

Parse and analysis dominate cost; database writes are cheap (0.10 s for xv6).
A content-addressed per-TU cache gives large second-run speedups without
changing the database format or query behavior, and it fits naturally into the
existing worker/`TuWorkResult` boundary.

## Alternatives considered

- **Full rebuild every run**: correct but wastes the dominant cost; rejected.
- **mtime-only invalidation**: unsound when content changes without mtime or
  when headers change; rejected as the primary key.
- **Database delta without a cache**: still requires parsing to detect changes.
- **Content-addressed object store of records**: more moving parts than needed
  for a CLI; rejected for now.

## Consequences

- Cache disk usage under `.ca-cache/`; safe to delete at any time (falls back to
  full parse).
- Hash computation adds a small per-TU cost; still far below parsing.
- Correctness boundary is documented: source + compile-argument changes are
  detected, header/toolchain changes require a full run.
- Database rows are still rewritten each run; incremental speedup comes from the
  parse/analyze stages.

## Validation and risks

Acceptance: measured hit rate and second-run speedup on xv6 and a kernel subset;
tests for second run, one changed TU, and corrupt cache fallback. Risk: stale
results after header edits; mitigated by documented full-run requirement and a
`--full`/cache-clear flag.

## Relations and supersession

- Related architecture: [overview.md](../../architecture/overview.md)
- Related decisions: [storage engine](2026-10-17-storage-engine.md)
- Supersedes: none
- Superseded by: none
