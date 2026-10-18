# DSH plugin (`dsh/`, bundle `@local/dsh-ca`)

Status: delivered. Additive DSH bundle over CA's read-only API/CLI; CA core (`src/`,
`tests/`, `docs/`) unchanged. Reasons:
[decision](../decisions/active/2026-10-18-dsh-plugin.md).

## Package and activation

| Piece | Responsibility |
|---|---|
| `package.json`, `cordis.patch.yml` | private bundle manifest: host entry, patch, `dsh.client`, client export, locale |
| `lib/host/bootstrap.js` | Host entry, `inject=['tools','connection','webServer']`; cache-busted import of `impl.js` reloads host code on plugin re-activation (bootstrap edits need a DSH restart) |
| `lib/host/impl.js` | wiring: backend/tasks/explain → RPC, tools, prompt, inside `ctx.effect` |
| `lib/client/index.js` → `client.js` | bundled by `scripts/build-client.mjs` into `window.__ModuleLoader__.load(...)`; mounts `main` page (key/locale `ca`) + `sidebar.panellist` entry |

## Project model

**Project** = source tree `sourceRoot` + exactly one database (`db` file or `dbDir`
shards) + backend location (`local`, or `remote` with SSH fields), plus optional
`compdb`/`python`/`caRepoPath` (CA checkout). Users create/edit/delete named projects
(unique, case-insensitive) on `#/projects`; they persist in
`$DSH_HOME/plugin-data/dsh-ca/settings.json` as `projects[]` + `activeProjectId` (0600).
Activation maps the project to backend config, restarts `ca serve` and rolls back on
failure; a legacy settings file migrates into one default project. Paths are validated
on save/activate (local must exist; remote format-checked only). Directory scanning
(`backend.databases`/`useDatabase`) survives only as a low-level helper.

## Host services

- **BackendManager** (`lib/host/backend/`): status/request/exec + project store; local
  `ca serve --db|--db-dir --port 0` (ready = stderr URL + `/api/health`); remote
  probe → checkout/rsync → venv → `pip install -e .` (editable: a wheel loses
  `schema.sql`/UI assets) → remote serve + `ssh -N -L`
  tunnel → loopback health; stops via remote PID file.
- **TaskManager** (`lib/host/tasks/`): CA CLI jobs (`index`, `update`, `ensure_deep`,
  `prewarm`, `resolve_callbacks`, `watch`) via `execCa`; concurrency 2, ring log,
  `queued → running → succeeded|failed|cancelled`.
- **ExplainService** (`lib/host/explain/`): DSH model (`agentDefaultModel` +
  `ctx.llm.stream`) over bounded CA evidence; 90 s timeout; CA owns no LLM config.

## Client ↔ Host channel

`ctx.webServer.register({kind:'prefix', path:'/dsh-ca'})`, not `connection.rpc.handle`
(that fails activation in a plain-JS plugin: `cannot get property "webServer" without
inject`).

```text
browser (DSH-authenticated page)        harness host                     CA backend
  fetch POST /dsh-ca/<endpoint>  ─────> connection.requestRejection
    JSON envelope                      └─> dispatcher ── ca.request ──> GET/POST /api/*
  {ok:true,value} | {ok:false,error} <── BackendManager/TaskManager/Explain
```

Endpoints: `ping`, `status` (includes active `project`), `ca.request`,
`backend.get|configure|start|stop|restart|probe|install|installStatus`,
`projects.list|save|remove|activate`, `tasks.start|list|get|logs|cancel`, `explain`.
Statuses: business 200, bad arguments 400, oversized 413, unknown 404, non-POST 405,
trust rejection 401/403.

## Native tools and prompt

Twelve plain-JSON-Schema tools via `ctx.tools.register`: `ca_status`, `ca_project`,
`ca_search`, `ca_symbol`, `ca_graph`, `ca_facts`, `ca_source`, `ca_stats`,
`ca_callbacks`, `ca_registry`, `ca_explain`, `ca_task`; bounded text (~20k chars),
`exec.signal` cancellation. `ca_status` reports the active project; `ca_project`
lists/activates existing ones (create/edit/delete stay in the UI). `prompt.js` adds a
short system-prompt section (status → search → detail/facts/source).

## Client pages

Top bar: project selector (activation reconfigures, restarts, reloads) + nav. Hash
routes: `#/search`, `#/projects` (project management), `#/fn/<symbol>/<tab>`, `#/graph`,
`#/stats`, `#/callbacks`, `#/tasks`, `#/backend` (low-level status/config). Views:
function detail (source, accordion, three-pane, graph, CFG, AI panel), call graph, CFG. Data via `/dsh-ca`; only `--dsw-*` tokens and `ca-` prefixes; host DOM untouched.

## Zero-break boundary

- CA `src/`, CLI, web UI, MCP untouched; the plugin only calls existing read-only
  `/api/*` and runs `ca serve`/`ca` CLI; adds no CA endpoint or LLM config; writes only
  its own state directory.
- `ca ui`/`ca mcp` stay usable; one CA backend process per plugin instance (single
  `--db` or federated `--db-dir`).
- `client.js` is a build artifact: `node dsh/scripts/build-client.mjs`.

## Limits

- CA backend is loopback + unauthenticated; remote only through the SSH tunnel; the page
  is DSH-authenticated.
- DSH internal services (`webServer.register`, `requestRejection`, `slots`, `llm`,
  `agentDefaultModel`) are the integration contract; host drift is the main risk.
- No reports/file-browser pages; no source writes or compilation.
