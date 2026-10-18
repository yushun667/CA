# Unified backend (`ca serve`)

Status: delivered (phase 3/4 infrastructure); the AI explain feature has been removed (see
`../decisions/active/2026-10-18-remove-ai-explain.md`).

## Components

| Component | Responsibility |
|---|---|
| `ca serve` (`src/ca/server/app.py`) | one `ThreadingHTTPServer` process: `/api/*` read-only query endpoints (reused from `ca.ui.server`), stateless MCP JSON-RPC at `/mcp`, `/events` SSE and generic `/api/jobs*`; loopback by default, no auth |
| `ca.server.service` (`ServeService`) | owns the query service (single DB or federated shards), the generic job service and one stateless `McpSession`; job create/list/claim/result/fail endpoints; no AI/explain/sampling state |
| `ca.server.jobs` (`JobService`) | thin per-thread adapter over `ca.store.jobs.JobQueue` (separate writable state DB, WAL) plus an in-process `EventBus` that forwards newly appended job events to SSE subscribers |
| `ca.store.jobs` (`JobQueue`) | durable generic `jobs` + `job_events` (monotonic ids), atomic claim/lease, restart recovery, event replay; opened as a standalone state DB |
| `ca mcp --serve-url URL` | stdio JSON-RPC proxy: each stdin line is POSTed to the backend `/mcp`; stateless, 18 tools |
| `ca ui --serve-url URL` | browser thin client (ui-engineer): local static server proxies `/api/*` + `/events` to the backend (same-origin, no CORS); `--db` embedded mode unchanged |

## Data flow

```text
index shards (read-only) ──┐
                           ├─> ca serve (single process, loopback)
job state DB (writable) ───┘        |-> /api/* + /events (SSE)  <- browser UI
                                     |-> /mcp (stateless)       <- AI agent / ca mcp --serve-url
                                     |-> /api/jobs + JobQueue   <- generic background tasks
```

The query service is read-only: federated shards are opened with per-request
read-only connections and never written. Writes (generic jobs only) go to a
separate state database (`<db-dir>/.ca/jobs.db` for federated mode,
`<db>.serve-state.db` for a single DB). The service is the single job writer;
claim/complete are atomic `BEGIN IMMEDIATE` transactions, and each server thread
uses its own `JobQueue` connection.

## Generic jobs

`/api/jobs` is a generic background task queue with free-form `kind` strings
(e.g. `ensure_deep`, `prewarm`, `update`), states `queued → running → done |
failed` and `requeue`. Events (`job_events`, monotonic ids) are replayed over
`/events` SSE with `Last-Event-ID` / `?since_id=`. No worker or executor is
bundled: consumers claim/complete jobs over HTTP; index/deep/update execution
still happens through the CLI. The queue survives restarts
(`recover_running()` requeues interrupted jobs).

## MCP endpoint

`/mcp` is a stateless JSON-RPC endpoint (initialize/tools/list/tools/call) with
the 18 read-only tools; `ca mcp --serve-url` proxies stdio to it. There is no
MCP session state, server-initiated request, sampling or explain tool.

## Limits

- No authentication: loopback only (SSH tunnel for remote), same trust model as
  `ca ui`/`ca mcp`.
- No cross-shard transactions; shards never written. Single-file shard copies
  use the read-only `immutable=1` fallback and must be static while queried.
- Job executors are not bundled; the queue is infrastructure for future workers.
