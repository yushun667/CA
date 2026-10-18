# Decision: remove the AI explanation feature

Status: active
Date: 2026-10-18

## Context

The Web UI offered an "AI explanation" tab backed by either a job/claim loop, a
backend-configured LLM endpoint, or MCP sampling to a connected agent. Users
rejected all variants: they do not want CA to own an LLM configuration, the job
queue/claim path is the wrong interaction model for explanations, and requiring a
sampling-capable MCP client in practice made the feature unavailable (no
available sampling client, and server-initiated sampling added substantial
protocol/session complexity). The decision is to remove the feature entirely
rather than keep half-working entry points.

## Decision

Delete the whole AI explanation feature end to end:

- UI: remove the explain tab, explain state/requests/i18n keys and the proxy
  whitelist for explain endpoints.
- Backend: remove `/api/explain`, `/api/explain/agent`, `api_explain` and
  `agent_explain`; remove MCP sampling (sessions, `sampling/createMessage`,
  reverse SSE channel, health/CLI/env fields) and restore `/mcp` to a stateless
  JSON-RPC endpoint with the 18 read-only tools.
- MCP tools: remove `explain_function`, `explain_jobs`, `submit_explanation`.
- CLI: remove `ca agent-worker` and related auto-complete-explain logic.
- Keep the generic `JobQueue` / `/api/jobs` / `/events` SSE infrastructure for
  future background tasks (free-form kinds such as `ensure_deep`/`prewarm`/
  `update`), without any explain type or example.

## Reasons

- No usable sampling client existed in practice; keeping the plumbing would be a
  half-working feature and a maintenance burden.
- CA should not own LLM credentials/provider configuration; analysis facts and
  queries remain the product surface.
- The generic job queue is independently useful for future background work and
  is kept; only the explain-specific semantics are removed.
- Removing the UI entry point avoids confusing users with a feature that cannot
  reliably work.

## Alternatives considered

- **Keep the MCP sampling path**: rejected because no sampling-capable client was
  available and the session/reverse-request protocol was too much for an
  always-degraded feature.
- **Backend-configured LLM**: rejected earlier (credentials/provider ownership,
  user explicitly rejected).
- **Hide the UI tab but keep APIs**: rejected; dead code and half-working
  endpoints create confusion.

## Consequences

- The Web UI has no AI explanation entry point; users query facts and read code
  themselves or use their own agent workflow outside CA.
- `/mcp` is stateless again (18 tools); no MCP session maintenance is needed.
- The generic job queue/SSE infrastructure remains and is documented as
  background-task plumbing; no worker is bundled.
- Tests/docs/decisions for explain/sampling are removed or marked superseded.

## Validation and risks

`tests/test_server_serve.py` covers health/query endpoints, stateless MCP with 18
tools, generic job loop + SSE replay, restart requeue, stdio proxy and federated
stats, and asserts explain endpoints/tools/CLI are gone. Risk: future requests for
AI explanations must stay outside CA; the DSH plugin now provides them through the
host model (see [dsh-plugin](2026-10-18-dsh-plugin.md), partial supersession).
