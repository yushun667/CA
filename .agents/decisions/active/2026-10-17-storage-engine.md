# Decision: storage engine

Status: active
Date: 2026-10-17

## Context

The analyzer must ingest hundreds of thousands to millions of AST/CFG/data-flow
records and answer symbol, caller/callee, branch, and resource queries quickly.
It is a single-user CLI tool, must be portable, and should not require a server.
The database is the system of record and is also used by accuracy evaluation and
benchmarks.

## Decision

Use SQLite as the primary engine: WAL journal mode, normalized tables keyed by
stable TEXT IDs, batched writes inside explicit transactions, indexes created at
`finalize()` followed by `ANALYZE`. DuckDB is allowed only for optional
analytical experiments and exports; no server database is introduced in phase 1.

## Reasons

SQLite ships with Python, is a single portable file, handles one writer plus
concurrent readers well (which matches the pipeline: workers produce results,
the main process writes), and answers indexed point/range queries in
microseconds. Building indexes after bulk load keeps ingest fast.

## Alternatives considered

- **DuckDB as primary**: excellent columnar analytics and bulk-load speed, but a
  single writer, less mature row-level update patterns, and a heavier dependency
  for point lookups; kept as an optional analytical path rather than the system
  of record.
- **PostgreSQL**: rich query planner and concurrency, but server operations and
  credentials are out of scope for a local CLI.
- **Graph database (Neo4j/Kuzu)**: natural for call graphs, but schema/ingest
  complexity and operational weight are not justified by phase-1 queries.
- **LMDB/RocksDB**: fast key-value storage, but poor ad-hoc relational queries
  and no SQL for the CLI.

## Consequences

- Indexed queries are fast; large unindexed analytical scans are slower than a
  columnar engine. Phase 2 may add a DuckDB export if analytics dominate.
- The pipeline naturally has one writer; concurrent CLI readers use WAL.
- The database file is portable and easy to inspect with standard tools.

## Validation and risks

Measure bulk write throughput and query p50/p95 on a synthetic dataset with at
least 500k functions and 1M calls, and verify key queries with
`EXPLAIN QUERY PLAN`. Risk: over-indexing slows writes; measure and keep only
indexes that serve actual queries.

## Relations and supersession

- Related architecture: [overview.md](../../architecture/overview.md)
- Related decision: [analysis stack](2026-10-17-analysis-stack.md)
- Supersedes: none
- Superseded by: none
