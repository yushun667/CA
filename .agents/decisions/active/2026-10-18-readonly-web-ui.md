# Decision: local read-only web UI

Status: active
Date: 2026-10-18

## Context

The tool's users include human programmers; the agreed priority puts a human
UI after the agent-facing layers. CA already exposes a read-only store, a
federated shard query, CLI and MCP on the source machine. A UI must be usable
without adding a build toolchain, a server dependency or write paths, and must
not expose source outside the local machine/LAN.

## Decision

- Ship `ca ui --db DB` / `ca ui --db-dir DIR [--host 127.0.0.1 --port 8766]`
  as a local, read-only web UI built on Python's standard-library
  `ThreadingHTTPServer`.
- Frontend is a single page of vanilla HTML/CSS/JS under `src/ca/ui/static`,
  with no CDN, font or external network requests and no build step. It offers
  symbol search, function detail (overview/callers/callees/branches/returns/
  resources/assignments/findings), an SVG call graph with dashed candidate
  edges and confidence, callbacks, stats and shard badges.
- The server exposes a stable JSON API (`/api/health`, search, function,
  callers, callees, graph, stats, callbacks, fact lists) with `{ok,data}` /
  `{ok:false,error}` envelopes; non-GET requests return 405; static files are
  whitelisted with a self-only CSP. Single-DB mode uses a query-only
  connection; federated mode reuses `FederatedStore`.
- Bind loopback by default; remote viewing uses an SSH tunnel. No
  authentication or write operations are implemented.

## Alternatives

- Desktop/Electron or Sourcetrail-style native UI: heavy dependency and build
  chain, rejected.
- React/Vite SPA: needs Node/toolchain and package data handling for a
  read-only viewer, rejected.
- Authenticated remote web server: unnecessary for a loopback tool and expands
  the security surface, rejected.
- Notebook/HTML report only: weaker navigation and no interactive graph,
  rejected.

## Consequences

- Zero new Python dependencies; the UI ships with the package (wheel builds
  need package-data for `ca.ui/static`).
- SQLite connections are opened read-only per request; this is an interactive
  inspection tool, not a high-throughput service.
- No auth: do not bind beyond loopback; tunnel for remote access.
- UI changes need the same test/review gates as CLI/MCP; browser screenshots
  are not available in the agent sandbox, so HTTP/static tests are the gate.

## Validation

`tests/test_ui.py` covers health/search/detail/graph/stats/callbacks/static/
read-only/federated/CLI wiring (20 tests); full suite 357 passed / 3 skipped;
manual xv6 single-DB and two-shard curl smoke, POST 405, SIGTERM exit 0,
port-conflict exit 1.
