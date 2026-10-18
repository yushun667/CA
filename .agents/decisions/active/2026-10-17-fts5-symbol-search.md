# Decision: FTS5 symbol search

Status: active
Date: 2026-10-17

## Context

Symbol substring queries (`query_functions`) currently use `LIKE '%pattern%'`,
which scans the whole `functions` table: 60–75 ms p50 on a synthetic 500k
function database. Phase 2 targets p50 < 10 ms for substring search without
changing query semantics or keys.

## Decision

Add an SQLite FTS5 virtual table over function `name` and `qualified_name`,
populated in `finalize()`, using the trigram tokenizer so arbitrary substrings
match. Exact `id`/`name`/`qualified_name` lookups keep their current indexed
path and remain first priority; FTS is used only for substring resolution.
Fallback to `LIKE` when FTS5 or the trigram tokenizer is unavailable. Existing
databases are upgraded at `finalize()`. The public query API and returned dict
keys do not change.

## Reasons

FTS5 ships with SQLite (including the Python bundled SQLite), needs no external
service, and trigram indexing matches arbitrary substrings rather than only
token prefixes. Building the index at `finalize()` keeps bulk ingest fast.

## Alternatives considered

- **unicode61/prefix FTS**: fast but cannot answer arbitrary substring queries
  (e.g. a middle-of-identifier fragment); rejected as the primary tokenizer.
- **External search engine**: operational weight unjustified for a local CLI.
- **`LIKE` with a narrowed candidate set**: no reliable narrowing for arbitrary
  substrings; rejected.
- **In-memory cache of names**: memory-heavy and not persistent across runs.

## Consequences

- Database size and `finalize()` time grow modestly; bulk-write throughput is
  measured to confirm no significant regression.
- FTS5 availability depends on the SQLite build; runtime detection and `LIKE`
  fallback keep behavior correct everywhere.
- The FTS index must be maintained when symbol rows change (only at ingest /
  finalize in the current pipeline).

## Validation and risks

Acceptance: substring p50 < 10 ms on 500k functions, unchanged exact-match
results, fallback tests, and measured write/DB-size overhead. Risk: FTS5 trigram
behavior with non-ASCII identifiers and special characters; covered by tests.

## Relations and supersession

- Related architecture: [overview.md](../../architecture/overview.md)
- Related decisions: [storage engine](2026-10-17-storage-engine.md)
- Supersedes: none
- Superseded by: none
