# DSH plugin (`dsh/`, bundle `@local/dsh-ca`)

Status: delivered; additive DSH bundle over CA's read-only API/CLI, CA core unchanged.
Reasons:
[decision](../decisions/active/2026-10-18-dsh-plugin.md).

## Package and activation

| Piece | Responsibility |
|---|---|
| `package.json`, `cordis.patch.yml` | private bundle manifest: host entry, patch, `dsh.client`, client export, locale |
| `lib/host/bootstrap.js` | Host entry, `inject=['tools','connection','webServer']`; cache-busted import of `impl.js` reloads host code on re-activation (bootstrap edits need a restart) |
| `lib/host/impl.js` | wiring: backend/tasks/explain → RPC, tools, prompt, inside `ctx.effect` |
| `lib/client/index.js` → `client.js` | bundled by `scripts/build-client.mjs` into `window.__ModuleLoader__.load(...)`; mounts `main` page (key/locale `ca`) + `sidebar.panellist` |

## Project model

**Project** = source tree `sourceRoot` + exactly one database (`db` file or `dbDir`
shards) + backend location (`local`, or `remote` with SSH fields), plus optional
`compdb`/`python`/`caRepoPath` (CA checkout). Users create/edit/delete named projects
(unique, case-insensitive) on `#/projects`; they persist in
`$DSH_HOME/plugin-data/dsh-ca/settings.json` as `projects[]` + `activeProjectId` (0600).
Activation maps it to backend config, restarts `ca serve`, rolls back on failure; legacy
settings migrate into one default project. Paths are validated on save/activate (remote
only format-checked). Directory scanning (`backend.databases`) is a low-level helper.

## Host services

- **BackendManager** (`lib/host/backend/`): status/request/exec + project store; local
  `ca serve --db|--db-dir --port 0` (ready = stderr URL + `/api/health`).
- **Installer** (`lib/host/backend/installer/`): installs CA locally
  (Win/mac/Linux) or on remote Linux from the bundled wheel in `backend/wheels/`
  (+ optional wheelhouse);
  Python >= 3.11 is detected up front. Remote uploads use scp/ssh; legacy `source` mode
  keeps rsync + editable. Install summaries persist in `install-state.json`; remote
  `ca serve` runs behind `ssh -N -L`.
- **TaskManager** (`lib/host/tasks/`): CA CLI jobs (`index`, `update`, `ensure_deep`,
  `prewarm`, `resolve_callbacks`, `watch`) via `execCa`; concurrency 2, ring log, queued/running/succeeded/failed/cancelled.
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

Endpoints: `ping`, `status` (active `project`), `ca.request`,
`backend.get|configure|start|stop|restart|probe|install|installLocal|installStatus`,
`projects.list|save|remove|activate`, `tasks.start|list|get|logs|cancel`, `explain`.
Bad arguments 400, oversized 413, unknown 404, non-POST 405, trust rejection 401/403.

## Native tools and prompt

Twelve plain-JSON-Schema tools via `ctx.tools.register`: `ca_status`, `ca_project`,
`ca_search`, `ca_symbol`, `ca_graph`, `ca_facts`, `ca_source`, `ca_stats`,
`ca_callbacks`, `ca_registry`, `ca_explain`, `ca_task`; bounded text (~20k chars),
`exec.signal` cancellation. `ca_status` reports the active project; `ca_project`
lists/activates existing ones (create/edit/delete stay in the UI). `prompt.js` adds a
short system-prompt section (status → search → detail/facts/source).

## Client pages

Top bar: project selector (activate = reconfigure, restart, reload) + nav; `#/backend`
has install/repair, probe and low-level config. Hash routes: `#/search`, `#/projects`,
`#/fn/<symbol>/<tab>`, `#/graph`, `#/stats`, `#/callbacks`, `#/tasks`, `#/backend`.
Views: function detail (source, accordion, three-pane, graph, CFG, AI), call graph, CFG. Data via `/dsh-ca`; only `--dsw-*` tokens and `ca-` prefixes; host DOM
untouched.

## Zero-break boundary

- CA `src/`, CLI, web UI, MCP untouched; the plugin only calls existing read-only
  `/api/*` and runs `ca serve`/`ca` CLI; adds no CA endpoint/LLM config; writes only
  its state directory.
- `ca ui`/`ca mcp` stay usable; one CA backend process per plugin instance.
- `client.js` is a build artifact: `node dsh/scripts/build-client.mjs`.

## Limits

- CA backend is loopback + unauthenticated; remote only via the SSH tunnel; page is
  DSH-authenticated.
- DSH internal services (`webServer.register`, `requestRejection`, `slots`, `llm`,
  `agentDefaultModel`) are the integration contract; host drift is the main risk.
- No reports/file-browser pages; no source writes or compilation.
