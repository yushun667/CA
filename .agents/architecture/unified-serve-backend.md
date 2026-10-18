# Unified backend (`ca serve`)

Status: phase 4 (single DB + HTTP queries + `/mcp` + job queue/SSE + synchronous
MCP-sampling explain; UI thin client landed).

## Components

| Component | Responsibility |
|---|---|
| `ca serve` (`src/ca/server/app.py`) | one `ThreadingHTTPServer` process: `/api/*` read-only query endpoints (reused from `ca.ui.server`), `/mcp` JSON-RPC, `/events` SSE, `/api/jobs*`; loopback by default, no auth |
| `ca.server.service` (`ServeService`) | owns the query service (single DB or federated shards), the sampling gateway, the job service and one `McpSession`; synchronous explain, MCP job tools for compatibility, job create/claim/submit/fail endpoints |
| `ca.server.sampling` (`SamplingGateway`) | long-lived agent sessions (`POST /agent/session` + SSE events) and server→agent JSON-RPC `sampling/createMessage`; correlates the agent response with the waiting HTTP explain request |
| `ca agent-session` (`src/ca/cli/agent_session.py`) | reference MCP sampling client: keeps a session open, answers `sampling/createMessage` with the agent's own model (or `--agent-command`), heartbeats and closes cleanly |
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
                                     |-> /api/explain/agent      <- synchronous sampling explain (no queue)
                                     |-> /agent/session + SSE   <- sampling agent side channel
                                     |-> /api/jobs + JobQueue    <- deep/update tasks (legacy explain jobs kept)
```

The query service is read-only: federated shards are opened with per-request
read-only connections and never written. Writes (jobs only) go to a separate
state database (`<db-dir>/.ca/jobs.db` for federated mode, `<db>.serve-state.db`
for a single DB). The service is the single writer; claim/complete are atomic
`BEGIN IMMEDIATE` transactions, and each server thread uses its own `JobQueue`
connection.

## Synchronous explain via MCP sampling

The Web UI explain tab does **not** create a job or wait for a claim. The user
request `POST /api/explain/agent {symbol,lang}` is served synchronously:

1. The backend collects function facts (detail/source/branches/returns/
   resources/findings/call counts/CFG summary).
2. A sampling-capable agent has a long-lived side channel: `POST /agent/session`
   with `capabilities.sampling`, then `GET /agent/session/<id>/events` (SSE).
3. The backend sends the standard JSON-RPC `sampling/createMessage` request
   (messages/systemPrompt/includeContext/modelPreferences/temperature/maxTokens/
   metadata) to the oldest sampling session; the agent's own model answers.
4. The agent posts the JSON-RPC response to
   `POST /agent/session/<id>/response`; the backend pairs it with the waiting
   request and returns `provider:"agent"`, the agent's `model`, explanation and
   evidence in the same HTTP response.
5. No session, timeout (default 30s, `--sampling-timeout`) or agent error ->
   HTTP 200 with the local heuristic and `fallback_reason`; the page never hangs.

Jobs/SSE remain for other work (`ensure_deep`/`prewarm`/`update` placeholders and
legacy `explain` compatibility via `ca agent-worker`); they are not used by the
UI explain flow. No backend LLM configuration (`CA_AGENT_URL`/`CA_LLM_*` is not
part of this path). Graceful SIGINT/SIGTERM stops the server and closes sampling
sessions and queue connections; on startup `recover_running()` requeues
interrupted `running` jobs.

## Limits

- No authentication: loopback only (SSH tunnel for remote), same trust model as
  `ca ui`/`ca mcp`.
- No cross-shard transactions; shards never written. Single-file shard copies
  use the read-only `immutable=1` fallback and must be static while queried.
- No backend LLM: synchronous explain needs a connected sampling-capable agent;
  without one the endpoint returns the local heuristic (HTTP 200). Jobs remain
  for deep/update tasks and legacy explain automation only.
