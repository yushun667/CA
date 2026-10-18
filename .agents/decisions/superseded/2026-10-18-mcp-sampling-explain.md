# Decision: synchronous explain via MCP sampling (no backend LLM, no job queue)

Status: superseded
Date: 2026-10-18

Superseded by: [2026-10-18-remove-ai-explain.md](../active/2026-10-18-remove-ai-explain.md)
(the feature was removed entirely; no sampling/explain path remains).

## Context

The Web UI used an explain job: the browser created a job, an MCP agent claimed it
and submitted the explanation, and the UI updated over SSE. Users rejected that
flow for interactive explanations (queued/claiming adds latency and needs a
running agent), and also rejected the follow-up idea of configuring a backend
LLM endpoint (``CA_AGENT_URL``/``CA_LLM_*``) in ``ca serve``: the model should be
the one already used by the user's MCP agent. The agent is the client; the server
must not own model credentials or provider configuration.

## Decision

Explain requests are **synchronous server -> client MCP sampling** calls:

1. a sampling-capable MCP agent keeps a long-lived session open with
   standard MCP ``/mcp`` sessions: ``initialize`` with
   ``capabilities.sampling`` returns ``Mcp-Session-Id`` and ``GET /mcp`` (SSE)
   is the server->client reverse channel;
2. the Web UI calls ``POST /api/explain/agent {symbol,lang}``;
3. the server collects bounded function facts/source and sends the standard
   JSON-RPC ``sampling/createMessage`` request to the oldest sampling session;
4. the agent answers with its own model via
   ``POST /mcp`` with the same session header (``CreateMessageResult``);
5. the server returns ``provider:"agent"``, the agent's ``model``, explanation and
   evidence in the same HTTP response.

Without a connected sampling agent, or on timeout/agent error, the endpoint
returns HTTP 200 with the local heuristic and ``fallback_reason``. There is no
job/claim/queued state for explain, and no backend LLM configuration. The job
queue remains for other work (``ensure_deep``/``prewarm``/``update`` placeholders
and legacy explain automation via ``ca agent-worker``).

## Reasons

- The agent already has a model and credentials; forwarding sampling requests
  keeps the server credential-free and provider-agnostic.
- Synchronous request/response removes queue latency and the "no agent running"
  stuck state; a heuristic fallback keeps the page responsive.
- Standard MCP sampling means real MCP agents participate without a custom API;
  no extra agent-session CLI or demo client exists; the connected MCP client is
  the agent.
- The long-lived session + SSE side channel is needed because MCP sampling is a
  server-initiated request; plain HTTP ``/mcp`` request/response has no channel
  for server->client messages.

## Alternatives considered

- **Explain job + claim (previous design)**: rejected by users for interactive
  explain; kept only for legacy/background automation.
- **Backend-configured OpenAI-compatible endpoint** (``CA_AGENT_URL``): rejected
  because it duplicates the agent's model/credentials and lets the backend call
  an external model; removed from the serve path.
- **Sync HTTP call to an agent URL**: rejected for the same credential/model
  ownership reason; sampling keeps the agent in control.

## Consequences

- Interactive explain requires a connected sampling agent for ``provider:"agent"``;
  otherwise users get the local heuristic (200) with a clear fallback reason.
- Sessions are long-lived and heartbeat/TTL-managed (idle >120s pruned); a
  disconnected agent makes explain fall back until it reconnects.
- The server may forward bounded source/facts to the connected agent; privacy
  docs must state this and that no external request happens without an agent.
- Job/SSE APIs stay stable for other tasks, so existing clients/tests keep
  working.

## Validation and risks

``tests/test_server_serve.py`` covers mock sampling success (asserts the MCP
request shape), timeout fallback, no-session fallback, session lifecycle, no job
creation with a standard MCP client (initialize capabilities.sampling + SSE +
response). No-session heuristic fallback measured
p50 ≈4 ms on xv6. Risks: agents that do not implement sampling see only the
heuristic; a slow agent consumes an HTTP worker for up to the timeout; session
state is in-process only (restart requires the agent to reconnect).
