# Issue: OpenHarmony full index OOM, broken pools and a pool-recycle hang

Status: resolved
Opened: 2026-10-18
Resolved: 2026-10-18

## Symptom and impact

Three acceptance attempts failed while indexing the 214k-command OpenHarmony
compile database on the 62 GB server:
1. `2 shards x jobs10`: memory rose from 38 GB to 54 GB, free hit 0, the
   machine lost SSH for ~9 minutes, `BrokenProcessPool` killed S2 (17,571 TUs
   reported as failed) and S1/S3 made no commits.
2. `2 shards x jobs4`: workers grew to 4-10 GB RSS; S4 degraded from 17% to
   29% parse failures and available fell to 5 GB.
3. After adding `ProcessPoolExecutor(max_tasks_per_child=16)`, S3 hung at
   1521/11039 TUs with no worker alive (parent sleeping on a futex, load 0.12,
   memory normal). The 400-TU stability run could not expose this because it
   never crossed the recycle boundary.

## Root causes

- Per-worker libclang AST memory accumulates across TUs; the same workers kept
  parsing whole OpenHarmony TUs until RSS reached several GB.
- The long-lived process pool turned one OOM into a whole-pool failure; the
  pipeline initially had no broken-pool retry or fine-grained chunk checkpoint.
- CPython 3.11 `max_tasks_per_child` deadlocked at its first recycle boundary
  with the call-queue feeder under real load; this is a runtime defect, not
  merely memory pressure.

## Fixes

- Split work into chunks of at most 16 TUs; retry a broken pool with halved
  workers and then one TU at a time; yield only successful chunk results so
  retries are idempotent (stable ids de-duplicate in the store).
- Replace `max_tasks_per_child` with wave-level pool rebuilds
  (`CA_POOL_RECYCLE_CHUNKS=2`): each worker handles at most two chunks, the leaf
  pool is rebuilt, and a fresh worker releases the libclang AST.
- Accept only complete shard DBs (`.done + exit 0 + stats`); archive partial
  DBs and keep EVIDENCE; run single-shard serial or 2 x jobs6 with a
  `MemAvailable`/load guard (20 GB/36), recycling env and `MALLOC_ARENA_MAX=2`.

## Evidence and validation

- Archived evidence: `index/failed_20261018/` and
  `index/failed_20261018_hang/` (DBs, logs, progress, EVIDENCE.md).
- Re-running six "no records" S4 TUs with lean libclang 15 succeeded in
  0.03-1.18 s/TU, proving those were runtime memory failures, not parse gaps.
- 400-TU stability: jobs6 153 s, min_avail 54 GB, no broken; 1600-TU long gate:
  540 s, 2.96 TU/s, min_avail 49 GB, peak worker 2.0 GB, broken=0, no hang.
- Final acceptance profile: 2 shards x jobs6, wave recycling, guard 20 GB/36.

## Lessons

- Long-lived parser workers need bounded lifetime even when the average TU is
  small; a 400-TU sample does not exercise recycle or OOM boundaries.
- "Parsed" counters and process liveness do not prove progress: require
  per-shard done markers plus stats, and watch 5-minute stalls.
- Keep the database per shard so a failure cannot corrupt the whole acceptance
  snapshot; archive partials instead of reusing them.
