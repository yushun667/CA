# CA web UI

`ca ui` serves a **local, read-only web UI** over a CA index: a single-page
browser interface plus a small JSON API. It reuses the same store queries as the
CLI and MCP server (single database through `ca.store`, several shard databases
through `ca.store.federated.FederatedStore`), so every number on the page has
the same meaning as the corresponding CLI output.

The frontend is plain HTML/CSS/JavaScript: no CDN, no external fonts, no Node
build step, no third-party runtime dependency and no network request other than
this server. The HTTP server uses the Python standard library (`http.server`).

`ca ui` runs in one of two modes:

- **embedded** (`--db` / `--db-dir`, the default): the UI process opens the
  index itself and answers `/api/*` directly.
- **remote** (`--serve-url`, thin client): the UI process only serves the static
  assets and **reverse-proxies** `/api/*` and `/events` to a running
  [`ca serve`](server.md) backend on the same browser origin. The browser never
  talks to the backend directly, so no CORS/CSP changes are needed and the UI
  stays read-only.

## Starting the UI

```sh
# a single database (exactly the CLI single-DB semantics: no shard column)
.venv/bin/ca ui --db ca.db

# pick another port or bind address (127.0.0.1 is the default and the safe one)
.venv/bin/ca ui --db ca.db --port 9000
.venv/bin/ca ui --db ca.db --host 127.0.0.1

# several shard databases (federated queries, rows carry a shard column)
.venv/bin/ca ui --db-dir /home/yu/CA_OH/index
.venv/bin/ca ui --db oh_S1.db --db oh_S3.db

# module form when the console script is not on PATH
PYTHONPATH=src .venv/bin/python -m ca ui --db ca.db

# thin client for the unified backend (task/SSE/agent collaboration)
.venv/bin/ca serve --db ca.db --port 8765       # backend: /api/*, /events, /mcp
.venv/bin/ca ui --serve-url http://127.0.0.1:8765 --port 8766
```

The server prints the exact URL on stderr, e.g.
`[ui] CA read-only web UI listening on http://127.0.0.1:8766/`; open it in a
browser and press `Ctrl-C` to stop (SIGINT/SIGTERM shut it down gracefully).
Exit code is 0 after a clean stop and 1 when the database selector is invalid or
the port cannot be bound.

| Option | Meaning |
|---|---|
| `--db PATH` | SQLite database (repeatable; more than one value means federated) |
| `--db-dir DIR` | directory of shard `*.db` files (federated; unreadable shards are skipped) |
| `--serve-url URL` | thin-client mode: proxy `/api/*` + `/events` to a running `ca serve` (cannot be combined with `--db`/`--db-dir`) |
| `--host HOST` | bind address (default `127.0.0.1`) |
| `--port PORT` | bind port (default `8766`; `0` lets the OS pick a free port) |

## Remote mode (unified backend)

`ca ui --serve-url http://127.0.0.1:8765` serves the same single-page UI but
forwards queries and events to a long-lived [`ca serve`](server.md) process:

- `GET /api/ui` (local) reports `{mode:"remote", serve_url, features}`; older
  embedded servers return `{mode:"embedded"}`.
- Query endpoints (`/api/search`, `/api/function`, `/api/graph`, `/api/cfg`,
  `/api/source`, …) are proxied verbatim; non-whitelisted POSTs
  return `405 READ_ONLY`, and a query timeout is 30 s. If the backend is down,
  the UI answers `502 SERVE_UNAVAILABLE` with the standard error envelope.
- `GET /events` remains available through the proxy (no buffering, heartbeat,
  `Last-Event-ID` replay) for other job types/future clients.
- A connection badge in the top bar shows `connecting / connected / reconnecting
  / disconnected`, derived from `/api/ui` and the success/failure of API
  requests; clicking the badge retries the connection. There is no EventSource in
  the UI.
- Everything stays read-only; the UI proxy cannot write to the index. Remote
  `--serve-url` values should be loopback or a trusted tunnel; the backend has no
  authentication in this milestone.

## Pages

The single page has a search bar with a floating result dropdown and a detail
pane; navigation state lives in the URL hash, so a function can be bookmarked or
shared:

- `#/fn/<name-or-id>/<tab>` — function page with the given accordion section
  expanded (`overview`, `branches`, `returns`, `resources`, `findings`);
  omit `/<tab>` to keep all sections collapsed
- `#/stats` — database statistics
- `#/callbacks` — cross-TU callback registrations

What you can inspect:

- **Symbol search** — exact `id`/name/qualified-name hits first, then substring
  matches (the store's FTS5 trigram index; short patterns fall back to `LIKE`).
  Results open in a floating dropdown under the search box (no permanent results
  sidebar) and the dropdown closes when a symbol is selected, when the query is
  cleared, on `Esc` or when clicking outside. Results show
  definition/declaration, `static`, source location and, in federated mode, the
  shard.
- **Function page layout** — the top panel is the **complete source** of the
  opened function (line numbers, read-only, syntax highlighted, collapsible and
  scrollable, with `file:line`, shard and the function line range highlighted).
  It stays fixed when you interact with the call graph. Below it, a collapsible
  **accordion sidebar** on the left lists `overview`, `branches`, `returns`,
  `resources` and `findings` with count badges: clicking a
  label expands that section's content **inside the sidebar** (click again or
  click another label to collapse it); the sidebar content scrolls and the
  collapse state is remembered in `localStorage`. The **call graph is always
  visible in the middle column** (direction/depth/rebuild controls stay
  available). The **right panel only shows the selected graph-node source** (or
  a "click a node" hint); tab content and the CFG never render there.
- **Navigation and jump history** — double-clicking a graph node (or using the
  node panel's Open-detail button) records the current function and tab in a
  session-scoped history (deduplicated, capped at 50 entries, persisted in
  `sessionStorage`). The next function page shows a `← Back to previous
  function` button; the button, `Alt+Left` and `Alt+Backspace` all return to the
  previous function and restore its tab, while a plain search/open entry has no
  back button. Function/stat/callbacks views push hash entries, so the browser's
  native Back button walks the same history; hash deep links and the
  `hashchange` router keep working.
- **Overview** — signature, return type, file:line range, linkage, USR/id/TU ids
  and parameters; badge for `deep facts` vs `deep facts pending` (base/L0
  databases only carry symbols and calls until `ca ensure-deep` runs).
- **Call graph** — SVG traversal (direction `both`/forward/reverse, depth 1–6).
  Edge colours encode type: direct calls solid grey, indirect callback candidates
  purple dashed, virtual candidates teal dashed, cycles red. Candidate edges show
  only the confidence number (0–1, heuristic); the legend explains it as the
  candidate confidence, not a probability. **Single-clicking** a node (or pressing Enter on it)
  opens that function's **complete source in the right panel** without leaving
  the page: line numbers, a `file:line` header, the owning shard badge,
  independent scrolling, plus Close and Open-detail buttons; when the node's
  lines are inside the fixed top source window they are also highlighted and
  scrolled into view there, otherwise the top panel shows a subtle
  "Node is at `<file>:<line>`" hint instead. **Double-clicking** a node (after a
  ~250 ms single-click delay is cancelled) navigates to that function's detail
  page. Closing the node source returns the right panel to its "click a node"
  hint. On narrow screens the sidebar becomes a full-width drawer and the graph
  and node-source panel stack.
- **Branches** — expanding this accordion section renders an SVG control-flow
  graph **inside the left sidebar**, built from `cfg_blocks`/`cfg_edges` with
  branch conditions attached: entry/exit blocks are highlighted, true/false
  edges use different colours and are labelled `T`/`F`, back edges are dashed,
  and the graph scrolls horizontally and zooms (40–180 %). Functions without
  deep CFG facts show a "deep facts pending" fallback instead of a table.
- **Returns / resources / findings** — the deep facts extracted by the
  CFG/data-flow analysis, with locations.
- **Stats** — table row counts, database size, schema/SQLite versions, callback
  statistics and, in federated mode, per-shard size/schema/counts.
- **Callbacks** — function-pointer registration facts (object path, target,
  registration kind, confidence, module); filter by object-path prefix, field or
  module.
- Keyboard: `/` focuses search, `Esc` clears it.

Both code viewers share a local C/C++ tokenizer (`highlight.js`): keywords,
types, comments (including multi-line block comments), string/char literals,
numbers and preprocessor lines are highlighted on the client, with no CDN or
external request. Everything is rendered with HTML escaping; the UI never
interprets database content as HTML.

## Resizable panels and full viewport

The page fills the whole viewport (no fixed max-width container); the shell keeps
only an 8 px gutter and each content area scrolls on its own. On the function
page three splitter handles resize the workspace:

- **Horizontal splitter** below the header — drag the top source area's height
  (default `32vh`, minimum 140 px); double-click resets it.
- **Two vertical splitters** — resize the left accordion sidebar, the middle call
  graph and the right node-source column (defaults left `22%`, right `26%`,
  middle auto; minimum widths 220 / 420 / 280 px). Double-click resets the
  neighbouring column.
- Sizes and the collapsed/expanded state are stored in `localStorage` under
  `ca.ui.layout` and restored on reload; the header's **Reset layout** button
  restores every default (and expands both columns).
- The left and right columns can be collapsed from the header buttons; the call
  graph then fills the freed space. A collapsed right column expands
  automatically when a graph node is selected.
- Each splitter is `role="separator"` with `aria-orientation`/`aria-valuenow`,
  and focused separators respond to the arrow keys (`↑`/`↓` for the source
  height, `←`/`→` for the side columns).
- Below 1100 px the splitters are disabled and the three areas stack (the
  sidebar becomes a full-width drawer) so touch layouts stay usable.

## Language

The UI is bilingual (English / Simplified Chinese); data itself is never
translated — function names, file paths, signatures, code snippets and raw
field values stay exactly as indexed.

- Default: the browser language (`navigator.languages`); a Chinese browser opens
  in Chinese, anything else in English.
- Override: add `?lang=zh` or `?lang=en` to the page URL.
- Switch: the `中文 / EN` control in the top bar updates the page instantly
  (static labels, tabs, tables, graph legend, empty states and error messages),
  sets `<html lang>`, and stores the choice in `localStorage` under
  `ca.ui.lang`, so a reload keeps it. The JSON API and its payloads are
  identical in both languages; only the browser UI is localized.

## JSON API

All endpoints return the same envelope and are `GET`-only:

```json
{"ok": true, "data": { }}
{"ok": false, "error": {"code": "NOT_FOUND", "message": "no function matching 'x'"}}
```

| Endpoint | Parameters | Data |
|---|---|---|
| `GET /api/health` (alias `/health`) | — | status, version, uptime, database paths/shards/skipped, `db_ok`, table counts |
| `GET /api/search` | `q` (required), `limit` (≤500), `offset` | `{query, items, count, limit, offset, federated}` |
| `GET /api/function/<name-or-id>` | path segment; or `?symbol=` | `{function, deep_ready, shard?, branches, assignments, returns, resource_ops, findings}` |
| `GET /api/callers` | `symbol` (required), `limit` (≤2000), `offset` | merged direct + candidate caller edges |
| `GET /api/callees` | `symbol` (required), `limit` (≤2000), `offset` | merged direct + candidate callee edges |
| `GET /api/graph` | `symbol`, `direction=both\|forward\|reverse`, `depth=0..12`, `limit` | normalized graph: `root, direction, depth, nodes[], edges[], cycles, truncated, stats, found` |
| `GET /api/stats` | — | table counts, `db_bytes`, schema version, `callbacks`, federated `shards` |
| `GET /api/callbacks` | `path` (prefix), `field`, `module`, `kind`, `limit`, `offset` | registration facts (target, evidence, confidence) |
| `GET /api/source` | `symbol` (required), `context` (0–10, default 3) | read-only source window: `{symbol, function{id,name,qualified_name,kind,file,line,end_line,signature}, file, language, start, end, total_lines, function_start, function_end, context, truncated, lines[{line,text}], shard, path, repo_root}` |
| `GET /api/cfg` | `symbol` (required) | read-only control-flow graph: `{symbol, function{id,name,qualified_name,kind,file,line,end_line}, deep_ready, shard, federated, blocks[], edges[], branches[], truncated, stats}`; blocks carry `index/kind/terminator/line/end_line/statement_count`, branch annotations (`condition`, `branch_kind`, `true_block`, `false_block`) and `is_entry`/`is_exit`; edges carry a display `label` (`true`/`false` for branch edges) |
| `GET /api/branches`, `/api/assignments`, `/api/returns`, `/api/resources` | `symbol` (required) | per-function fact rows (additive convenience endpoints) |

`/api/cfg` resolves the function through the normal detail query and returns its
CFG blocks/edges plus decoded branch rows (federated mode merges the owning
shard's rows with a `shard` field). It is read-only; unknown symbols return
`404 NOT_FOUND` and functions without deep analysis return empty lists with
`deep_ready: false`.

`/api/source` resolves the function through the normal detail query, reads the
owning translation unit's `repo_root` from the `tus` table (in federated mode
it uses the shard that holds the function), and reads `repo_root/file`. The
resolved real path must stay inside that repository root: `..` components,
absolute paths, symlinks pointing outside the root and missing files all return
`404 NOT_FOUND`. Windows are capped at 400 lines and `context` is clamped to
`0–10`; the response is read-only and never writes to the database.

Example:

```sh
curl -s 'http://127.0.0.1:8766/api/search?q=syscall&limit=5' | python3 -m json.tool
curl -s 'http://127.0.0.1:8766/api/function/syscall' | python3 -m json.tool
curl -s 'http://127.0.0.1:8766/api/graph?symbol=main&direction=both&depth=2' \
  | python3 -m json.tool
```

Error codes: `INVALID_ARGUMENT` (400), `NOT_FOUND` (404), `READ_ONLY` (405 for
any non-GET method), `DB_ERROR`/`INTERNAL` (500), `DB_UNAVAILABLE` (503 when no
database/shard is readable). Unknown paths return `NOT_FOUND`.

## Read-only guarantees and safety

- **The UI never writes to the database.** A single database is opened through a
  connection with `PRAGMA query_only=ON`, so any accidental write fails inside
  SQLite; federated shards are attached read-only by `FederatedStore`. The UI
  intentionally does not reuse `ca.cli.dbsource.open_query_store` for a single
  database because that helper opens a read-write `SqliteStore` whose `open()`
  applies DDL and meta upserts.
- **No write endpoints exist.** Every non-GET method returns `405` with
  `{"ok": false, "error": {"code": "READ_ONLY", ...}}`; there is no indexing,
  `ensure-deep`, callback-resolution or workspace mutation through HTTP. Use the
  CLI/MCP for those.
- **Loopback by default.** The server binds `127.0.0.1` and has no
  authentication. Do not bind a public interface. To view a remote index, run
  CA on the source host and forward the port, e.g.
  `ssh -N -L 8766:127.0.0.1:8766 user@source-host`, then open
  `http://127.0.0.1:8766/` locally.
- **Static files are a fixed whitelist** (`index.html`, `app.js`, `style.css`,
  `favicon.svg`); request paths never touch the filesystem directly. Responses
  carry `Content-Security-Policy: default-src 'self'; script-src 'self'; ...`,
  `X-Content-Type-Options: nosniff` and `Cache-Control: no-store`.
- Each request opens a fresh short-lived read-only connection, which keeps the
  threaded server simple and safe while another process (e.g. `ca watch`)
  refreshes the database. This is an interactive inspection tool, not a
  benchmark or a high-throughput service.
- Federated shards can be queried in place with their `-wal`/`-shm` sidecars, or
  as static single-file copies (read-only `immutable=1` fallback; works but is
  slightly slower). Do not point the UI at a shard that another process is
  actively checkpointing.
- A base/L0 index shows symbols and calls only; deep lists stay empty and the
  function header shows `deep facts pending`. Run
  `ca ensure-deep --db DB --repo REPO --compdb COMPDB --symbols NAME` (or index
  with `--layer deep`) to fill them.

## Tests

The UI tests build a temporary database through `ca.store` (no Clang/compile
database needed), start the server on a random loopback port inside a thread and
assert health/search/detail/graph/stats/callbacks/static pages, the read-only
guarantees and federated shard provenance:

```sh
.venv/bin/python -m pytest tests/test_ui.py -q
```

Related documentation: the CLI in [usage.md](usage.md), the agent interface in
[mcp.md](mcp.md), lazy deep analysis in [layered-indexing.md](layered-indexing.md)
and callback candidate edges in
[cross-tu-callbacks.md](cross-tu-callbacks.md).
