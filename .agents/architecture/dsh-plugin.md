# DSH plugin (`dsh/`, bundle `@local/dsh-ca`)

Status: delivered. CA core (`src/`, `tests/`, `docs/`) is unchanged: an additive
DeepSeek Harness bundle reusing CA's read-only API and CLI. Reasons:
[decision](../decisions/active/2026-10-18-dsh-plugin.md).

## Package and activation

| Piece | Responsibility |
|---|---|
| `dsh/package.json`, `dsh/cordis.patch.yml` | private bundle manifest: host entry, patch, `dsh.client`, client export, locale |
| `dsh/lib/host/bootstrap.js` | Host entry, `inject=['tools','connection','webServer']`; imports `impl.js` with a timestamped query so re-activating the plugin reloads host code (bootstrap edits need a DSH restart) |
| `dsh/lib/host/impl.js` | wiring: BackendManager → TaskManager → ExplainService, then RPC/tools/prompt registration in `ctx.effect` |
| `dsh/lib/client/index.js` → `dsh/client.js` | Client entry bundled by `dsh/scripts/build-client.mjs` into `window.__ModuleLoader__.load(...)`; mounts the `main` page (key/locale `ca`) and a `sidebar.panellist` entry |

## Host services

- **BackendManager** (`dsh/lib/host/backend/`): one status/request/exec surface.
  Local: `ca serve --db|--db-dir --port 0`, readiness = stderr URL line +
  `GET /api/health`, bounded restarts, graceful stop. Remote: ssh probe → existing
  checkout or rsync → venv → `pip install -e .` (editable mandatory: `pyproject.toml`
  has no package-data, so a wheel drops `schema.sql` and UI assets) → remote `ca serve`
  + local `ssh -N -L` tunnel → health through loopback; stop via remote PID file.
  Settings: defaults ← patch config ← user sparse patch in
  `$DSH_HOME/plugin-data/dsh-ca/settings.json` (0600); never writes the DSH profile.
- **TaskManager** (`dsh/lib/host/tasks/`): CA CLI jobs (`index`, `update`,
  `ensure_deep`, `prewarm`, `resolve_callbacks`, `watch`) via `execCa`; concurrency 2,
  per-task ring log, `queued → running → succeeded|failed|cancelled`.
- **ExplainService** (`dsh/lib/host/explain/`): native DSH model from
  `ctx.agentDefaultModel.currentSelection()` and `ctx.llm.stream`; bounded read-only
  CA evidence first (function, source, callers/callees, branches, resources, findings);
  90 s timeout; CA owns no LLM configuration.

## Client ↔ Host channel

`ctx.webServer.register({kind:'prefix', path:'/dsh-ca'})`, not `connection.rpc.handle`
(that fails to activate in a plain-JS plugin: `cannot get property "webServer" without
inject`).

```text
browser (DSH-authenticated page)        harness host                     CA backend
  fetch POST /dsh-ca/<endpoint>  ─────> connection.requestRejection
    JSON envelope                      └─> dispatcher ── ca.request ──> GET/POST /api/*
  {ok:true,value} | {ok:false,error} <── BackendManager/TaskManager/Explain
```

Endpoints: `ping`, `status`, `ca.request`,
`backend.get|configure|start|stop|restart|probe|install|installStatus`,
`tasks.start|list|get|logs|cancel`, `explain`. Statuses: business 200, bad arguments
400, oversized body 413, unknown endpoint 404, non-POST 405, trust rejection 401/403;
payloads are validated and size-bounded before reaching a service.

## Native tools and prompt

Eleven plain-JSON-Schema tools registered with `ctx.tools.register`: `ca_status`,
`ca_search`, `ca_symbol`, `ca_graph`, `ca_facts`, `ca_source`, `ca_stats`,
`ca_callbacks`, `ca_registry`, `ca_explain`, `ca_task`. Each returns bounded text
(~20k chars) and observes `exec.signal`; `ca_task` drives the TaskManager and
`ca_explain` reuses the ExplainService. `prompt.js` adds a short system-prompt section
with the usage order (status → search → detail/facts/source).

## Client pages

`main` page hash routes: `#/search`, `#/fn/<symbol>/<tab>`, `#/graph`, `#/stats`,
`#/callbacks`, `#/tasks`, `#/backend`. Views: function detail (source,
overview/branches/returns/resources/findings accordion, three-pane layout, call graph,
CFG, AI panel), call graph (direct/callback/virtual-dispatch/cycle edges), CFG
(entry/exit, T/F labels, back edges, `deep_ready` fallback). All data goes through
`/dsh-ca`; only `--dsw-*` theme tokens and `ca-` class prefixes; host DOM untouched.

## Zero-break boundary

- CA `src/`, CLI, web UI and MCP are untouched; the plugin only calls the existing
  read-only `/api/*` and runs `ca serve`/`ca` CLI commands. It adds no CA endpoint, no
  CA-side LLM configuration, and writes only to its own state directory.
- `ca ui`/`ca mcp` remain usable in parallel; one CA backend process per plugin
  instance (single `--db` or federated `--db-dir`).
- `dsh/client.js` is a build artifact; regenerate with `node dsh/scripts/build-client.mjs`.

## Limits

- CA backend is loopback and unauthenticated; remote access exists only through the SSH
  tunnel, and the page is authenticated by DSH.
- DSH internal services (`webServer.register`, `connection.requestRejection`, `slots`,
  `llm`, `agentDefaultModel`) are the integration contract; host drift is the main risk.
- No reports/file-browser pages beyond these routes; no source writes or compilation.
