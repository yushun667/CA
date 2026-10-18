# DSH plugin (`@local/dsh-ca`)

CA also ships as a DeepSeek Harness bundle: the CA page inside the Harness Web UI,
twelve native `ca_*` tools for the agent, a self-installed local or remote Linux
`ca serve` backend, long indexing tasks and AI explanations. CA itself is
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

This installs the **plugin** itself; the CA analyzer **backend** is installed
separately by the plugin's backend installer (next sections).

## Adding a project (one step)

The project page asks for one thing:

- **Local**: click **Choose directory…** and pick a source tree. The plugin installs
  or reuses the bundled CA backend, creates the database under
  `$DSH_HOME/plugin-data/dsh-ca/data/<name>/index.db`, starts `ca serve` and activates
  the project.
- **Remote Linux**: enter username, host/IP, port (22), and either a password or a
  key / none (免密); click **Connect**, browse the server directories and click
  **Choose this directory**. The plugin installs the backend under
  `~/dsh-ca/backend` on the server, creates `~/.dsh-ca/data/<name>/index.db`, starts
  `ca serve` behind the SSH tunnel and activates the project.

Password authentication uses a transient `SSH_ASKPASS` helper on the DSH host: the
password is never logged or persisted, so it must be entered again after a restart.
Windows hosts should use a key or the SSH agent (password mode is rejected there).
Everything else — venv, wheel file, DB path, ports, compile database detection — is
automatic; the old full form stays available under **Advanced** for special cases.

## Configuration

Deployment config lives in the Cordis patch row (`dsh/cordis.patch.yml`); edits made in
the backend page are persisted as a sparse user patch in
`$DSH_HOME/plugin-data/dsh-ca/settings.json` (0600). Merge order: defaults ← patch row
← user patch. Nothing is written into the DSH profile.

| Field | Default | Meaning |
|---|---|---|
| `backend` | `auto` | `auto` prefers a configured remote host, else a local DB; `local`/`remote` force a mode |
| `autoStart` | `true` | start the backend when the plugin activates |
| `projects` / `activeProjectId` | `[]` / `null` | saved projects (see above); the active project drives the backend target |
| `requestTimeoutMs` | `30000` | CA HTTP request timeout |
| `readyTimeoutMs` | `20000` | max wait for `ca serve` readiness |
| `local.repoRoot` | auto | CA checkout root (discovered from cwd when unset) |
| `local.python` | auto | interpreter; falls back to `<repo>/.venv/bin/python`, then `python3` |
| `local.db` / `local.dbDir` | — | single SQLite index **or** federated shard directory (mutually exclusive) |
| `local.stateDb` | CA default | writable job-state DB for `ca serve` |
| `local.repo` / `local.compdb` | — | repo root / `compile_commands.json` for deep tools |
| `local.host` / `local.port` | `127.0.0.1` / `0` | bind address and port; `0` = OS-assigned |
| `local.maxRestarts` | `3` | bounded crash restarts |
| `remote.ssh.host` / `user` / `port` / `keyPath` | — | SSH target (alias or `user@host`), port, local private-key path; optional transient password (askpass; never persisted) |
| `remote.repoPath` | — | existing CA checkout on the Linux host (source mode / manual installs) |
| `remote.installDir` | — | rsync destination and legacy source-mode install target; wheel installs use the installer form's own directory |
| `remote.python` | auto (`python3`) | remote interpreter |
| `remote.db` / `remote.dbDir` | — | remote single DB or shard directory |
| `remote.serveRemotePort` / `remote.localPort` | `0` / `0` | remote serve port and local tunnel port (0 = auto) |
| `local.env` / `remote.env` | `{}` | extra child-process environment |
| `log.*` | bounded | plugin log rotation and in-memory ring settings |

The backend installer runs automatically when a project is prepared; the low-level
options (`mode: wheel|source`, `offline`, `wheelhousePath`, `installDir`, `python`) are
internal RPC parameters (`backend.install`) rather than a user-facing form. The last
successful install is recorded in
`$DSH_HOME/plugin-data/dsh-ca/install-state.json` (0600) and surfaced through
`backend.get.install`.

### Projects (source root + database + backend location)

CA is a general analyzer, so the plugin is organized around **projects**, like
opening a project in DSH. One project binds three things that always belong
together:

| Field | Meaning |
|---|---|
| `name` | unique, human-chosen label |
| `sourceRoot` | the source tree the index was built from (absolute path on the backend host) |
| `db` **or** `dbDir` | one SQLite index, or a directory of federated shards |
| `backend` + `ssh` | run the index locally or on a remote Linux host (SSH target) |
| `compdb`, `python`, `caRepoPath` | optional: compile database and CA/venv locations |

Projects are created, edited and removed **by hand** on the `#/projects` page and
persisted in the user patch; nothing is inferred from a scanned directory. The
top bar always shows the active project; switching it calls
`projects.activate`, which reconfigures the backend, restarts `ca serve` (and
the SSH tunnel) and reloads the page. A failed switch rolls back to the previous
configuration. When upgrading from the older single-target configuration, the
plugin migrates it into one default project named after the database; check its
`sourceRoot` and edit it if the source tree lives elsewhere.

All remote paths must be absolute or start with `~/`.

Advanced optional overrides are read from the same config object:
`maxToolOutputChars` (or `tools.maxOutputChars`, 4000–60000, default 20000),
`maxConcurrentTasks` (or `tasks.maxConcurrent`, 1–8, default 2) and
`maxTaskLogBytes` (or `tasks.maxLogBytes`, 64 KiB–16 MiB, default 1 MiB).

## Installing the CA backend (local or remote)

CA is pure Python: **nothing is compiled**, and the runtime dependencies
(`clang`/`libclang`, `duckdb`, `pytest` and transitive wheels) are prebuilt
wheels. The plugin installs the backend for you from a bundled CA wheel
(`dsh/backend/wheels/ca_analyzer-<version>-py3-none-any.whl`, about 360 KB)
plus, for offline machines, a per-platform wheelhouse of dependency wheels.

Requirements on every backend host: **Python >= 3.11** with `venv`/`pip`
(Windows: `py -3.13`/`py -3.12`/`py -3.11`; macOS/Linux: auto-detected
`python3.13`/`3.12`/`3.11`), and working SSH for remote backends (`scp`
recommended; the legacy source mode also needs `rsync`). Online installs need
network access; offline installs need a wheelhouse for the target platform.

### Install / repair

Installation is automatic while a project is prepared (or re-prepared) — there is no
backend configuration page. Both installers are idempotent: a working venv is reused
and re-preparing the project repairs or recreates a broken one; steps and logs are
available through `backend.installStatus`. The installer goes online by default and
uses a wheelhouse automatically when one is present.

- **Local backend installation** (Windows/macOS/Linux): optional install
  directory (default: the plugin's private `backends/local`) and Python
  executable, then choose **Online (PyPI)** or **Offline (wheelhouse)**. Steps:
  environment → wheel → venv → dependencies → verify (`ca version --json`).
- **Remote installation** (Linux over SSH): choose install method **wheel
  (recommended)** or **source tree (legacy)**, plus Online/Offline. Wheel
  steps: probe → Python check → upload CA wheel → upload wheelhouse (offline)
  → remote venv → pip install → verify. Default remote install directory
  `~/dsh-ca/backend`; uploads use `scp` when available and otherwise stream
  over SSH (a wheelhouse directory is streamed as a tar). The remote
  `ca serve` then runs behind the local SSH tunnel.

The installer records its last successful run in
`$DSH_HOME/plugin-data/dsh-ca/install-state.json` (0600) and reports platform,
venv Python, CA version and install directory through `backend.get.install`;
after installing, create or activate a project that points at the installed CA.

### Offline installation (air-gapped host)

Build the dependency wheelhouse once on a machine with network:

```sh
node dsh/scripts/build-wheelhouse.mjs --platform <id> [--tar]
node dsh/scripts/build-wheelhouse.mjs --list-platforms
```

Platform ids: `linux-x64`, `linux-arm64`, `macos-arm64`, `macos-x64`,
`windows-x64`. Output goes to `dsh/artifacts/wheelhouse/<platform>/`
(dependency wheels + a copy of the CA wheel + `manifest.json`); `--tar` also
writes `<platform>.tar.gz`. Sizes: about 41 MB for `macos-arm64` and 46 MB for
`linux-x64`.

For a local offline install, copy the directory to the plugin state default
`$DSH_HOME/plugin-data/dsh-ca/wheelhouse/<platform>/` or point the form field
at it; a `.tar.gz` must be extracted first because `pip --find-links` needs a
directory. For a remote offline install, select the local wheelhouse directory
in the form and the installer uploads it to the Linux host. Offline pip runs
with `--no-index --find-links`, so no network is used.

Measured: a macOS arm64 local offline install takes about **1.4 s**; a real
Linux server offline wheel install takes about **6.4 s**, including the upload
of a 46 MB wheelhouse.

### Platform support

| Host | Local backend | Remote backend | Notes |
|---|---|---|---|
| macOS arm64 / x64 | code path supported; arm64 verified | target must be Linux | primary development host |
| Linux x64 / aarch64 | code path supported | verified on a real Linux server | `pythonX.Y-venv` needed for venv creation |
| Windows x64 | code path implemented, not verified on real hardware | target must be Linux | use `py -3.11+`; CA `watch` is unavailable on Windows |

The dependency wheels decide the platform matrix; the remote backend only
targets Linux.

### Developer fallback (source mode)

The previous flow still works for development: point a project at an existing
CA checkout/venv/index, or create a venv manually and install editable
(`python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/pip install -e .`;
`scripts/bootstrap.sh` does the same on macOS/Linux). Selecting **source tree
(legacy)** as the remote install method keeps the rsync + editable flow
(requires `rsync` on both sides). Prefer the wheel installer for new
deployments; source mode is retained for compatibility and debugging.

## Local backend

1. On `#/projects`, click **Choose directory…** and pick the source tree. The plugin
   installs a private CA venv under `$DSH_HOME/plugin-data/dsh-ca/backends/local`
   (no system Python is touched), creates the database and starts `ca serve`.
2. Nothing else is required: the database path, Python interpreter and ports are
   derived automatically.
3. Activate the project. The Host maps it onto the backend configuration and runs
   `ca serve --host 127.0.0.1 --port 0 --db <db>`, parses the readiness URL from stderr
   and verifies `GET /api/health`; when `--port 0` cannot be parsed it retries once with
   a locally chosen free port.
4. Diagnostics (discovered interpreter, CA version, checkout and libclang state) are
   available through `backend.probe` / `backend.get` if something fails; there is no
   backend settings page in the product UI.

The Host owns the process: it stops when the plugin is disposed, crash restarts are
bounded, and a missing database fails with an actionable error.

## Remote Linux backend

Use this when DSH runs on macOS/Windows but sources and indexes live on a Linux
server.

1. On `#/projects`, choose **Remote server**, enter username, host/IP, port and either
   a password or a key / agent (免密), then press **Connect**.
2. Browse the server directories and press **Choose this directory**. The plugin
   installs `~/dsh-ca/backend` on the server (wheel, online or from a wheelhouse),
   creates `~/.dsh-ca/data/<name>/index.db`, starts `ca serve` and opens the SSH tunnel;
   no rsync or source checkout is required.
   Activate it: the Host configures the backend and runs `ca serve` on the remote
   loopback interface, opening a local
   `ssh -N -L <localPort>:127.0.0.1:<remotePort>` tunnel; health checks and all queries
   go through that tunnel. Stop terminates the remote process via its PID file, because
   closing the local SSH client alone does not reliably stop it.
4. **Probe remote** reports OS, Python/pip, libclang, path writability and disk before
   you install or start. The **source tree (legacy)** method keeps the old rsync +
   editable flow (requires `rsync` on both sides); prefer wheel mode.

Long remote indexing is run through `#/tasks` or `ca_task`; those execute `ca` on the
remote host over SSH.

## UI pages

The plugin registers the full page in the Harness `main` slot (key/locale `ca`) and an
entry in the `sidebar.panellist` sidebar. Routes:

- `#/search` — symbol search with a result dropdown (`Enter` opens, `Esc` closes, `/`
  focuses). Landing page when a project is active.
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
- `#/projects` — the only place to add or manage projects: **local = choose a directory**
  (in-app directory browser served by the Host, `local.browse`);
  **remote = username/IP/password or key → log in → browse the server and choose a
  directory**. The backend, database path and activation are automatic; the page is the
  landing page when no project exists.
- Function page → **Explain with AI** panel, see below.

## Native tools

Twelve tools are visible to the agent; each returns bounded text and observes the
tool-call cancellation signal.

| Tool | Purpose |
|---|---|
| `ca_status` | backend state, health, active project, database path, index counts, task summary |
| `ca_project` | list/use/status projects; `use` activates an existing project (creation and editing stay in the UI) |
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

- **Backend not configured** — install the backend (above), then create and activate a
  project on `#/projects`; `ca_status` returns the actionable hint.
- **Database/repository not found** — run the `backend.probe` diagnostic (or re-add the
  project), then use the
  reported paths in the project form or set the interpreter explicitly; index the
  repository first.
- **Install fails** — read the failing step in `backend.installStatus`. Wheel steps are
  `probe/python/upload-wheel/upload-wheelhouse/venv/pip/verify` (local:
  `environment/wheel/venv/dependencies/verify`); check SSH/scp, Python >= 3.11 with
  venv, and disk space. Source mode additionally needs `rsync`.
- **`UNSUPPORTED_PYTHON`** — install Python 3.11+ (`python3.11-venv` on Debian/Ubuntu,
  `py -3.11` on Windows) or set the interpreter explicitly in the install form /
  `remote.python`; Ubuntu 22.04 ships Python 3.10 by default.
- **Offline install cannot find wheels** — the wheelhouse must be a directory (extract
  `.tar.gz` first), built for the target platform id with `--platform`, and contain the
  CA wheel plus all dependency wheels.
- **Tunnel or health fails** — confirm the remote `ca serve` listens on loopback and SSH
  key forwarding (`-L`) is allowed; a stale remote process is cleaned up through its PID
  file on restart.
- **Page or tools missing** — re-run the verify script and re-enable the bundle; restart
  the Harness after Host changes, rebuild `client.js` and refresh after Client changes.
- **AI explanation unavailable** — pick a default model in the Harness, or fall back to
  `ca_symbol`/`ca_facts`/`ca_source` and explain manually.
- **No project selected / migrated project points at the CA checkout** — open
  `#/projects`, edit `sourceRoot` to the real source tree and pick `db`/`dbDir`, then
  activate; the migrated project keeps the old target working until you do.

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
