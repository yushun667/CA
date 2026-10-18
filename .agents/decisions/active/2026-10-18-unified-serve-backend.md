# Decision: one backend service for UI and MCP clients

Status: active
Date: 2026-10-18

## Context

`ca ui` and `ca mcp` originally opened the database independently: a browser UI
process and one MCP process per agent. Both read the same index but cannot see
each other's work, so cross-client flows (a human asks for an AI explanation, an
agent produces it) require file polling or out-of-band IPC. The index shards must
stay read-only; the only durable mutable state is a queue of requested work.

## Decision

Run one long-lived backend, `ca serve`, that owns query access (single DB or
federated shards) and a durable job queue in a separate writable SQLite state
database. Clients are thin:

- browsers/UI use `/api/*` and `GET /events` (SSE with `Last-Event-ID` replay);
- agents use HTTP `/mcp`, or `ca mcp --serve-url` proxies stdio to it;
- `ca ui --serve-url` proxies its local UI to the backend, while `--db`
  embedded mode keeps working;
- MCP sees the normal tools plus `explain_jobs`/`submit_explanation`, closing the
  loop: UI creates an `explain` job -> agent claims/submits -> SSE `done` updates
  the UI.

Jobs use the storage-owned `JobQueue` (atomic claim/lease, restart recovery,
monotonic `job_events` ids) so the service remains the single writer.

## Reasons

- One query path and one cache of database connections avoids duplicated
  per-process startup and lets all clients observe the same job state.
- A separate state DB keeps shard indexes read-only and lets federation stay
  strictly read-only.
- Durable jobs + event log survive restarts and give SSE replay without an
  extra broker; the queue reuses the existing store schema mechanism.
- Embedded UI/MCP modes stay for one-shot local use and tests, so the change is
  additive rather than a flag day.

## Alternatives considered

- **Keep separate UI/MCP backends with polling**: no durable queue or replay,
  duplicate connections, hard to build cross-client UX.
- **MCP-only backend**: browsers cannot consume JSON-RPC tools and would need a
  second protocol bridge; UI queries are read-only and simpler over HTTP.
- **In-memory job queue**: loses claims on crash, no SSE replay after restart.

## Consequences

- `ca serve` is a single-writer process; index/deep/update executors must run
  inside it (or through the CLI) and long tasks must renew leases.
- Default loopback/no-auth model is unchanged; exposure still needs an SSH
  tunnel.
- UI keeps two modes (embedded and `--serve-url`), which the UI tests cover.
- `ensure_deep`/`prewarm`/`update` job kinds are placeholders until executors
  land; docs state this limitation.

## Validation and risks

Explain jobs were later refined by
[2026-10-18-mcp-sampling-explain.md](2026-10-18-mcp-sampling-explain.md): interactive
UI explain is synchronous MCP sampling (no job/claim); jobs remain for other tasks.

First milestone validated by `tests/test_server_serve.py` (HTTP queries, MCP
`/mcp`, explain cross-client loop, SSE replay, restart recovery, stdio proxy,
federated shard stats) and `benchmarks/results/server_bench.json`: health/search
p50 2.0/1.7 ms, MCP detail p50 4.6 ms, cross-client loop p50 5.5 ms on xv6.
Risks: single-process availability, no auth by default, and job execution still
partly manual until executors arrive.
