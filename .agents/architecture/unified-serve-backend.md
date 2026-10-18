# Unified backend (`ca serve`)

Status: phase 4 first milestone (single DB + HTTP queries + `/mcp` + one explain
job loop + SSE; UI thin client landing in parallel).

## Components

| Component | Responsibility |
|---|---|
| `ca serve` (`src/ca/server/app.py`) | one `ThreadingHTTPServer` process: `/api/*` read-only query endpoints (reused from `ca.ui.server`), `/mcp` JSON-RPC, `/events` SSE, `/api/jobs*`; loopback by default, no auth |
| `ca.server.service` (`ServeService`) | owns the query service (single DB or federated shards), the job service and one `McpSession`; adds MCP job tools `explain_jobs`/`submit_explanation`; job create/claim/submit/fail endpoints |
| `ca.server.jobs` (`JobService`) | thin per-thread adapter over `ca.store.jobs.JobQueue` (separate writable state DB, WAL) plus an in-process `EventBus` that forwards newly appended store events to SSE subscribers |
| `ca.store.jobs` (`JobQueue`) | durable `jobs` + `job_events` (monotonic ids), atomic claim/lease, restart recovery, event replay; opened either as standalone state DB or on a writable `SqliteStore` |
| `ca mcp --serve-url URL` | stdio JSON-RPC proxy: each stdin line is POSTed to the backend `/mcp`; agents keep using the normal MCP tool interface |
| `ca ui --serve-url URL` | browser thin client (ui-engineer): local static server proxies `/api/*` + `/events` to the backend (same-origin, no CORS); `--db` embedded mode unchanged |

## Data flow

```text
index shards (read-only) ──┐
                           ├─> ca serve (single process, loopback)
job state DB (writable) ───┘        |-> /api/* + /events (SSE)  <- browser UI
                                     |-> /mcp                    <- AI agent via ca mcp --serve-url
                                     |-> /api/jobs + JobQueue    <- explain/deep/update ownership
```

The query service is read-only: federated shards are opened with per-request
read-only connections and never written. Writes (jobs only) go to a separate
state database (`<db-dir>/.ca/jobs.db` for federated mode, `<db>.serve-state.db`
for a single DB). The service is the single writer; claim/complete are atomic
`BEGIN IMMEDIATE` transactions, and each server thread uses its own `JobQueue`
connection.

## Cross-client job loop

1. Browser UI creates `POST /api/jobs {type:"explain", symbol}`.
2. MCP agent calls `explain_jobs {action:"claim", worker}`; the job becomes
   `running` with a lease and `attempts+1`.
3. Agent calls `submit_explanation {job_id, explanation, evidence}`; the job
   becomes `done` and a `done` event is appended.
4. `GET /events` streams `{type:"job.updated", event:"created|claimed|done|
   failed|requeued", job:{...}}` with `id:` = event id; clients reconnect with
   `Last-Event-ID` / `?since_id=` and replay missing events.

`ensure_deep`/`prewarm`/`update` job kinds exist as queued placeholders until
their executors land; those operations still run through the CLI. Graceful
SIGINT/SIGTERM stops the server and closes queue connections; on startup
`recover_running()` requeues interrupted `running` jobs.

## Limits

- No authentication: loopback only (SSH tunnel for remote), same trust model as
  `ca ui`/`ca mcp`.
- No cross-shard transactions; shards never written. Single-file shard copies
  use the read-only `immutable=1` fallback and must be static while queried.
- No direct LLM execution yet: explain jobs are completed by an MCP agent (or a
  future in-server worker); UI falls back to the synchronous `/api/explain` when
  no agent is available.
