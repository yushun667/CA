# Decision: ship CA as a DSH plugin (`@local/dsh-ca`)

Status: active
Date: 2026-10-18

## Context

CA ships a CLI, a read-only web UI and a stateless MCP endpoint; users work through AI
agents in DeepSeek Harness and want CA's index, facts, graphs and indexing tasks inside
that GUI, together with AI explanations. Two earlier constraints shape the design: the
removed explanation feature established that CA must not own LLM
credentials/provider configuration, and left a note that a future explanation design
would likely be an agent-side tool; and CA has no plugin/extension surface, so adding
agent-specific features to `src/ca/` would couple the analyzer to one host.

## Decision

Ship the CA capabilities as a separate DeepSeek Harness bundle, `dsh/`
(`@local/dsh-ca`), outside the Python package:

- Host half: BackendManager (local/remote `ca serve` lifecycle), a `/dsh-ca`
  same-origin HTTP channel, 11 native `ca_*` tools, a system-prompt section, a
  long-running CA CLI task manager and an explain service.
- Client half: full CA page in the DSH `main` slot plus a `sidebar.panellist` entry,
  with search/function/graph/stats/callbacks/tasks/backend pages and the AI panel.
- AI explanations come from the DSH session's native model (`agentDefaultModel` +
  `ctx.llm`) over bounded, read-only CA evidence; CA keeps no LLM configuration and no
  explain endpoint/tool.
- Client↔Host uses a DSH same-origin HTTP route (`ctx.webServer.register` prefix
  `/dsh-ca`) guarded by `ctx.connection.requestRejection`, because
  `ctx.connection.rpc.handle` fails activation in a plain-JS plugin with
  `cannot get property "webServer" without inject`.
- Remote Linux backend: probe → existing checkout or rsync → venv → `pip install -e .`
  (editable only, because a wheel loses `schema.sql`/UI assets) → remote `ca serve` +
  local SSH tunnel.
- CA `src/`, CLI, web UI and MCP are not modified.

## Reasons

- Keeps CA host-independent: the plugin reuses `ca serve` `/api/*` and the `ca` CLI,
  and CA stays LLM-free per the earlier decision.
- DSH already provides what the feature needs (authenticated web page, React slots,
  native tools, model service, system prompt); re-implementing them in CA would be
  larger and would put agent concerns into the analyzer.
- A separate bundle is installable/removable per profile and versioned independently.
- Local vs remote backend is a GUI deployment concern, so it belongs in the plugin
  rather than in `ca serve`.

## Alternatives considered

- **Extend `src/ca/` with a plugin/UI host and explain**: rejected — no extension
  points, CA must stay LLM-free, and it couples the analyzer to DSH.
- **Explain inside CA with a configured LLM**: already rejected in
  [remove-ai-explain](2026-10-18-remove-ai-explain.md) (credentials/provider ownership;
  users rejected it).
- **Use `ctx.connection.rpc.handle` for the channel**: tested and rejected — activation
  fails with `cannot get property "webServer" without inject`.
- **Embed the existing `ca ui` page in an iframe**: rejected — cross-origin, tunnel and
  theme/auth integration would be duplicated, and native tools/slots would still be
  missing.
- **MCP-only integration**: rejected — MCP gives tools but no DSH page, model or
  lifecycle; the removed sampling path had already shown client-side model access was
  unavailable.

## Consequences

- A DSH profile gets a full CA workspace (page + tools + AI) with zero CA changes;
  `ca ui`/`ca mcp`/CLI keep working, and CA's test baseline is unaffected.
- The plugin depends on DSH internal services (`webServer.register`,
  `connection.requestRejection`, `slots`, `llm`, `agentDefaultModel`); host version
  drift can break it and must be re-verified.
- Remote use needs a Linux host with SSH/rsync/venv and an editable CA checkout; the CA
  backend stays loopback-only and unauthenticated, so the SSH tunnel is the security
  boundary.
- `dsh/client.js` is a generated artifact: client changes need a rebuild, host changes
  need a Harness restart or plugin re-activation before they load.
- Partially supersedes the "future requests need a new design" risk note in
  `remove-ai-explain.md`.

## Validation and risks

Governance validators (`validate.py`, `check_artifacts.py`), the `dsh/` node:test suite
and CA's Python suite (497 passed / 4 skipped baseline) pass; real local/remote smoke
checks are in [docs/dsh-plugin.md](../../../docs/dsh-plugin.md). Risks: DSH API drift,
remote SSH environment variance, UI visual regressions without browser automation, and
the loopback trust model.

## Related and supersession

- Related architecture: [dsh-plugin](../../architecture/dsh-plugin.md).
- Partially supersedes: [remove-ai-explain](2026-10-18-remove-ai-explain.md) (only its
  future-design note; that decision stays active because CA still has no explain).
- Superseded by: none.
