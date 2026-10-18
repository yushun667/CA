# Decision: ship CA as a DSH plugin (`@local/dsh-ca`)

Status: active
Date: 2026-10-18

## Context

CA ships a CLI, read-only web UI and stateless MCP endpoint; DSH users want CA's index,
facts, graphs and indexing tasks inside the Harness GUI, with AI explanations. Two
constraints shape the design: the removed explanation feature established that CA must
not own LLM credentials/provider configuration, and noted a future design would be an
agent-side tool; CA has no plugin/extension surface, so agent features in `src/ca/` would
couple the analyzer to one host.

## Decision

Ship CA capabilities as a separate DSH bundle, `dsh/` (`@local/dsh-ca`), outside the
Python package:

- Host half: BackendManager (project store + local/remote `ca serve` lifecycle), a
  `/dsh-ca` same-origin HTTP channel, 12 native `ca_*` tools, a system-prompt section, a
  CA CLI task manager and an explain service.
- **Project model replaces the scan-first flow**: users create named projects (source tree +
  database/shard directory + backend location) on a dedicated page; activation configures
  and restarts the backend. Directory scanning
  (`backend.databases`) remains only a low-level helper. Only the user can bind a database to
  its source tree; a scan cannot infer that, and projects match how DSH works.
- Client half: full CA page in the DSH `main` slot plus `sidebar.panellist`, with
  search/projects/function/graph/stats/callbacks/tasks/backend pages and the AI panel; the
  top bar is a project selector.
- AI explanations come from the DSH session's native model (`agentDefaultModel` +
  `ctx.llm`) over bounded read-only CA evidence; CA keeps no LLM configuration and no
  explain endpoint/tool.
- Client↔Host uses a DSH same-origin HTTP route (`ctx.webServer.register` prefix
  `/dsh-ca`) guarded by `ctx.connection.requestRejection`, because
  `ctx.connection.rpc.handle` fails activation in a plain-JS plugin with
  `cannot get property "webServer" without inject`.
- Remote Linux backend: probe → checkout/rsync → venv → `pip install -e .` (editable
  only: a wheel loses `schema.sql`/UI assets) → remote `ca serve` + local SSH tunnel.
- CA `src/`, CLI, web UI and MCP are not modified.

## Reasons

- CA stays host-independent: reuse `ca serve` `/api/*` and the `ca` CLI; CA stays
  LLM-free per the earlier decision.
- DSH already provides the page, React slots, native tools, model service and prompt;
  rebuilding them in CA would put agent concerns into the analyzer.
- A separate bundle installs/removes per profile and versions independently.
- Local vs remote is a GUI deployment concern, not a `ca serve` one.
- Explicit projects keep queries reproducible: each pins one source/database/backend
  triple, so a query cannot silently hit the wrong index.

## Alternatives considered

- **Extend `src/ca/` with a plugin/UI host and explain**: rejected — no extension
  points, CA must stay LLM-free, couples the analyzer to DSH.
- **Explain inside CA with a configured LLM**: already rejected in
  [remove-ai-explain](2026-10-18-remove-ai-explain.md) (credentials/provider ownership).
- **Use `ctx.connection.rpc.handle`**: tested and rejected — activation fails with
  `cannot get property "webServer" without inject`.
- **Iframe the existing `ca ui` page**: rejected — cross-origin/tunnel/theme/auth
  duplication, native tools/slots still missing.
- **Keep scan-based discovery primary**: rejected — a scan lists databases but cannot
  know their source tree/backend; users asked for explicit projects.
- **MCP-only integration**: rejected — no DSH page, model or lifecycle.

## Consequences

- A DSH profile gets projects + page + tools + AI with zero CA changes;
  `ca ui`/`ca mcp`/CLI keep working and CA's test baseline is unaffected.
- The plugin depends on DSH internal services (`webServer.register`,
  `connection.requestRejection`, `slots`, `llm`, `agentDefaultModel`); host drift must be
  re-verified.
- Remote use needs Linux + SSH/rsync/venv and an editable CA checkout; the CA backend
  stays loopback-only and unauthenticated, so the SSH tunnel is the boundary.
- `dsh/client.js` is generated: client changes need a rebuild, host changes a Harness
  restart or plugin re-activation.
- Legacy settings migrate into a default project; users should review it.
- Partially supersedes the "future requests need a new design" risk note in
  `remove-ai-explain.md`.

## Validation and risks

Governance validators, the `dsh/` node:test suite and CA's Python suite (497 passed / 4
skipped baseline) pass; real smoke checks are in
[docs/dsh-plugin.md](../../../docs/dsh-plugin.md). Risks: DSH API drift, remote SSH
variance, UI regressions, loopback trust, stale project paths.

## Related and supersession

- Related architecture: [dsh-plugin](../../architecture/dsh-plugin.md).
- Partially supersedes: [remove-ai-explain](2026-10-18-remove-ai-explain.md) (only its
  future-design note; that decision stays active because CA still has no explain).
- Superseded by: none.
