# DSH plugin (`@local/dsh-ca`)

CA also ships as a DeepSeek Harness bundle: the CA
page inside the Harness Web UI, eleven native `ca_*` tools for the agent, a local or
remote Linux `ca serve` backend, long indexing tasks and AI explanations. CA itself is
unchanged: the plugin only calls CA's existing read-only HTTP API and CLI, and it does
not change CA's test baseline (497 passed / 4 skipped at the time of writing).

## Installation (local directory bundle)

The bundle lives in `dsh/` at the repository root and is not published to a registry.

1. After changing `dsh/lib/client/`, rebuild the client artifact the Harness loads:
   `node dsh/scripts/build-client.mjs` (do not edit `dsh/client.js` by hand).
2. In the Harness plugin manager choose **install bundle from local directory** and
   point it at `<repo>/dsh`. This creates a link to the checkout, not a packaged copy,
   so later edits are picked up by the reload steps below.
3. Check the installation read-only: `node dsh/scripts/verify/verify-install.mjs`
   (`--json` for machine output). It verifies the manifest/patch/client artifact/locale/
   icon and the profile registration; exit code 0 means no FAIL.
4. Enable the bundle in the target profile. **Installed but not activated is not
   success**: the page, tools and prompt appear only when the plugin is active.

Reload rules: Host changes (`dsh/lib/host/**`) need a Harness restart (or
disable+re-enable the plugin) because the entry module is cached; Client changes need a
rebuild of `client.js` plus a page refresh (a stale page keeps the old bundle); changes
to `dsh/lib/host/bootstrap.js` always need a full Harness restart.

## Configuration

Deployment config lives in the Cordis patch row (`dsh/cordis.patch.yml`); edits made in
the backend page are persisted as a sparse user patch in
`$DSH_HOME/plugin-data/dsh-ca/settings.json` (0600). Merge order: defaults ← patch row
← user patch. Nothing is written into the DSH profile.

| Field | Default | Meaning |
|---|---|---|
| `backend` | `auto` | `auto` prefers a configured remote host, else a local DB; `local`/`remote` force a mode |
| `autoStart` | `true` | start the backend when the plugin activates |
| `requestTimeoutMs` | `30000` | CA HTTP request timeout |
| `readyTimeoutMs` | `20000` | max wait for `ca serve` readiness |
| `local.repoRoot` | auto | CA checkout root (discovered from cwd when unset) |
| `local.python` | auto | interpreter; falls back to `<repo>/.venv/bin/python`, then `python3` |
| `local.db` / `local.dbDir` | — | single SQLite index **or** federated shard directory (mutually exclusive) |
| `local.stateDb` | CA default | writable job-state DB for `ca serve` |
| `local.repo` / `local.compdb` | — | repo root / `compile_commands.json` for deep tools |
| `local.host` / `local.port` | `127.0.0.1` / `0` | bind address and port; `0` = OS-assigned |
| `local.maxRestarts` | `3` | bounded crash restarts |
| `remote.ssh.host` / `user` / `port` / `keyPath` | — | SSH target (alias or `user@host`), port, local private-key path; no passwords |
| `remote.repoPath` | — | existing CA checkout on the Linux host (preferred) |
| `remote.installDir` | — | rsync destination when `repoPath` is absent |
| `remote.python` | auto (`python3`) | remote interpreter |
| `remote.db` / `remote.dbDir` | — | remote single DB or shard directory |
| `remote.serveRemotePort` / `remote.localPort` | `0` / `0` | remote serve port and local tunnel port (0 = auto) |
| `local.env` / `remote.env` | `{}` | extra child-process environment |
| `log.*` | bounded | plugin log rotation and in-memory ring settings |

### Choosing the database / project

The top bar always shows the active database (a single `.db` file or a federated
shard directory, with its full path in the tooltip). Its selector lists databases
discovered on the backend host — for a remote backend it scans the configured
directory over one SSH round trip — plus the workspace/project/variant entries a
CA registry database exposes. The backend page repeats the same list under
"Available databases". Selecting an entry calls `backend.useDatabase`, which
persists the new target in the user patch, restarts `ca serve` (and the SSH
tunnel for remote targets) and reloads the page; a failed switch keeps the
previous database. Nothing about the queried index is compiled into the plugin:
it is always the configured local path or the remote path reached through SSH.

All remote paths must be absolute or start with `~/`.

Advanced optional overrides are read from the same config object:
`maxToolOutputChars` (or `tools.maxOutputChars`, 4000–60000, default 20000),
`maxConcurrentTasks` (or `tasks.maxConcurrent`, 1–8, default 2) and
`maxTaskLogBytes` (or `tasks.maxLogBytes`, 64 KiB–16 MiB, default 1 MiB).

## Local backend

1. Set `local.db` (or `local.dbDir` for federated shards); `local.repoRoot` and
   `local.python` are optional when the CA checkout and its venv are discoverable.
2. Open `#/backend` and start the backend. The Host runs
   `ca serve --host 127.0.0.1 --port 0 --db <db>`, parses the readiness URL from stderr
   and verifies `GET /api/health`; when `--port 0` cannot be parsed it retries once with
   a locally chosen free port.
3. Use **Probe local** before starting to see the discovered interpreter, CA version,
   candidate databases and libclang status.

The Host owns the process: it stops when the plugin is disposed, crash restarts are
bounded, and a missing database fails with an actionable error.

## Remote Linux backend

Use this when DSH runs on macOS but sources and indexes live on a Linux server.

1. Make sure key-based SSH works non-interactively (`ssh -i <key> user@host true`).
2. In `#/backend` fill the SSH fields, then set either `remote.repoPath` (existing CA
   checkout on the server) or `remote.installDir` (the plugin rsyncs this checkout
   there, excluding `.git`, `.venv`, `.tmp`).
3. Run **Probe remote**: one SSH round trip reports OS, Python/pip, rsync, libclang,
   path writability and free disk.
4. Run **Install**: `probe → source → rsync → venv → pip install -r requirements.txt →
   pip install -e . → ca version --json`. Editable install is mandatory because the
   project has no package-data: a wheel would miss `schema.sql` and the UI assets. The
   installation is idempotent and its steps/logs are polled through
   `backend.installStatus`.
5. Start: the Host runs `ca serve` on the remote loopback interface and opens a local
   `ssh -N -L <localPort>:127.0.0.1:<remotePort>` tunnel; health checks and all queries
   go through that tunnel. Stop terminates the remote process via its PID file, because
   closing the local SSH client alone does not reliably stop it.

Long remote indexing is run through `#/tasks` or `ca_task`; those execute `ca` on the
remote host over SSH.

## UI pages

The plugin registers the full page in the Harness `main` slot (key/locale `ca`) and an
entry in the `sidebar.panellist` sidebar. Routes:

- `#/search` — symbol search with a result dropdown (`Enter` opens, `Esc` closes, `/`
  focuses). Default landing page.
- `#/fn/<symbol>/<tab>` — function detail: full source with line numbers and syntax
  highlighting; left accordion with overview, branches (embedded CFG), returns,
  resources and findings; resizable three-pane layout; call graph plus selected-node
  source. `Alt+Left` goes back in the function history.
- `#/graph` — standalone call graph. Solid = direct call, purple dashed = callback
  candidate, teal dashed = virtual-dispatch candidate, red = cycle; direction
  forward/reverse/both, depth 0–12, node limit.
- `#/stats` — table row counts, database size, schema/SQLite versions, callback
  statistics and federated shard info.
- `#/callbacks` — cross-TU callback registrations with path/field/module/kind filters
  and pagination.
- `#/tasks` — long-task list, start, incremental logs and cancel.
- `#/backend` — backend status, start/stop/restart, local/remote probe, remote install,
  editable configuration and a `/api/health` check.
- Function page → **Explain with AI** panel, see below.

## Native tools

Eleven tools are visible to the agent; each returns bounded text and observes the
tool-call cancellation signal.

| Tool | Purpose |
|---|---|
| `ca_status` | backend state, health, database path, index counts, task summary |
| `ca_search` | resolve a function name/id/qualified name/substring (paged) |
| `ca_symbol` | one function/type record plus deep-fact counts and key conditions |
| `ca_graph` | callers/callees/both around a symbol; candidate edges are marked |
| `ca_facts` | one fact kind: branches, assignments, returns, resources or findings |
| `ca_source` | recorded source window (function ± context, CA caps it at 400 lines) |
| `ca_stats` | index-wide statistics and federated shards |
| `ca_callbacks` | cross-TU callback registrations with filters |
| `ca_registry` | registered workspaces/projects/variants; activate a variant in memory |
| `ca_explain` | AI explanation from bounded CA evidence (DSH model, see below) |
| `ca_task` | start/list/status/logs/cancel long CA CLI jobs |

`ca_task` returns immediately after starting; poll `action=logs` with the returned
`nextOffset` instead of blocking. A system-prompt section tells the model this order
(status → search → detail/facts/source).

## Long-running tasks

Task kinds: `index`, `update`, `ensure_deep`, `prewarm`, `resolve_callbacks` and
`watch`. Jobs run as CA CLI child processes (locally or over SSH), at most two at a
time; the state machine is `queued → running → succeeded|failed|cancelled`, and every
task keeps a bounded ring log (about 1 MiB) with offset pagination. Use `#/tasks` or
`ca_task`; cancel aborts the CLI process. Full indexing can take minutes to hours.

## AI explanations

The function page's **Explain with AI** button and the `ca_explain` tool use the DSH
session's default model (the `agentDefaultModel` selection and the `llm` service) — the
plugin stores no LLM configuration. Before calling the model it collects bounded,
read-only CA evidence for one symbol (signature, source window, callers/callees,
branches, resources, findings) and instructs the model to answer only from that
evidence, in the requested language (en/zh), as Markdown. The answer shows the model and
the evidence count; the renderer escapes all model output and never injects raw HTML.
When the session has no default model the feature reports `EXPLAIN_UNAVAILABLE` and the
rest of the plugin keeps working.

## Troubleshooting

- **Backend not configured** — set `local.db`/`local.dbDir` (or remote SSH + database
  fields), then start; `ca_status` returns the actionable hint.
- **Database/repository not found** — run **Probe local** and use the reported paths, or
  set `local.repoRoot`/`local.python` explicitly; index a repository first.
- **Remote install fails** — read the failing step
  (`probe/source/rsync/venv/dependencies/editable/verify`) and its log; check rsync,
  Python/venv and disk. Do not replace the editable install with a wheel.
- **Tunnel or health fails** — confirm the remote `ca serve` listens on loopback and SSH
  key forwarding (`-L`) is allowed; a stale remote process is cleaned up through its PID
  file on restart.
- **Page or tools missing** — re-run the verify script and re-enable the bundle; restart
  the Harness after Host changes, rebuild `client.js` and refresh after Client changes.
- **AI explanation unavailable** — pick a default model in the Harness, or fall back to
  `ca_symbol`/`ca_facts`/`ca_source` and explain manually.

## Security boundaries

- The CA backend binds loopback only and has no authentication (the same trust model as
  [ca ui](ui.md) and [ca mcp](mcp.md)). Never expose it on a public interface.
- The DSH page and the `/dsh-ca` route are authenticated by the Harness page session;
  the route additionally calls `connection.requestRejection` and rejects foreign
  origins (401/403).
- Remote access is only the SSH tunnel. No credentials are stored in the repository or
  in settings; `remote.ssh.keyPath` is a local path to a private key, and passwords are
  not supported on purpose.
- The plugin reads CA data and writes only its own settings, logs and task state; it
  never writes the DSH profile or CA source/index.

## Non-goals

- Not a replacement for the `ca` CLI, [ca ui](ui.md), [ca mcp](mcp.md) or MCP; they
  stay unchanged and usable side by side.
- No source editing, compilation or index writes: CA describes an existing index.
- No reports or file-browser pages beyond the routes above.
- No CA-owned LLM configuration and no CA explain endpoint.
- Not published as a package; installation is a local directory link.
