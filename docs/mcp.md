# CA MCP query interface

CA runs on the machine that owns the sources; AI agents query it instead of
copying source trees around. `ca mcp` exposes the indexed database through the
Model Context Protocol (MCP) over JSON-RPC 2.0.

Two transports are available:

| Transport | Use case | Command |
|---|---|---|
| `stdio` (default) | local agent process, or an agent invoking CA through SSH | `ca mcp --db /path/index.db` |
| `http` | LAN client, or an agent that only speaks streamable HTTP | `ca mcp --db /path/index.db --transport http --port 8765` |

`stdio` reads newline-delimited JSON-RPC messages from stdin and writes one
JSON-RPC response per line to stdout; diagnostics go to stderr. The HTTP
transport serves `POST /mcp` (one JSON-RPC request or batch per call) and
`GET /health`, and binds `127.0.0.1` by default — keep it local and use an SSH
tunnel, or open a firewall rule deliberately.

For a human-facing view of the same database, `ca ui --db <db>` serves a local
read-only browser UI (also loopback-only); see [ui.md](ui.md).

## Server-side setup

```sh
# on the source server (once)
scripts/bootstrap.sh                       # venv + pinned deps + editable install
.venv/bin/ca index --repo /home/yu/CA_OH/openharmony \
    --compdb /home/yu/CA_OH/openharmony/out/rk3568/compile_commands.json \
    --db /home/yu/CA_OH/openharmony.db --jobs 24

# offline batch indexing is separate from serving queries
.venv/bin/ca mcp --db /home/yu/CA_OH/openharmony.db            # stdio
.venv/bin/ca mcp --db /home/yu/CA_OH/openharmony.db \
    --transport http --host 127.0.0.1 --port 8765               # HTTP
```

`scripts/server_bootstrap.sh` wraps the checks: verifies the venv, runs the
full test suite and performs a stdio MCP smoke test against a temporary
database.

## Tools

All tools are read-only. List tools return
`{"items": [...], "count": N, "limit": L, "offset": O}`; `graph` returns the
schema below; `detail` and `stats` return their objects directly. Limits are
clamped to `1..1000`; `offset` paginates by fetching `limit + offset` rows and
slicing (queries are indexed, so this stays cheap for modest offsets).

| Tool | Arguments | Returns |
|---|---|---|
| `find_symbol` | `pattern` (required), `limit`, `offset` | functions matching exact id/name/qualified name, then substring |
| `functions` | same as `find_symbol` | alias of `find_symbol` |
| `callers` | `symbol` (required), `limit`, `offset` | call sites whose callee matches the symbol (direct + candidate edges) |
| `callees` | `symbol` (required), `limit`, `offset` | call sites whose caller matches the symbol (direct + candidate edges) |
| `graph` | `symbol` (required), `direction` (`forward`/`reverse`/`both`, default `forward`), `depth` (0–12, default 1), `limit` (default 500) | normalized call graph, see below |
| `branches` | `symbol` (required), `limit` | branch/condition facts inside the function |
| `assignments` | `symbol` (required), `limit` | assignment facts inside the function |
| `returns` | `symbol` (required), `limit` | explicit return facts |
| `resources` | `symbol` (required), `limit` | acquire/release resource operations |
| `leaks` | `kind` (default `leak`), `resource_kind`, `limit` | data-flow findings, optionally filtered by resource kind |
| `detail` | `symbol` (required), `fields` (optional projection), `compact` (default false) | function record + branches/assignments/returns/resources/findings; projection shrinks the payload |
| `brief` | `symbol` (required) | compact summary: signature/location, caller/callee counts, branch/return/resource counts, key condition, deep_ready |
| `batch` | `kind` (required), `symbols` or `symbols_file`, `limit` | one call for several symbols; per-symbol results in stable order |
| `stats` | – | table row counts, database size, schema version, `callbacks` registration/link stats |
| `callbacks` | `path` (object_path prefix), `field`, `module`, `limit` | cross-TU callback registration facts (object path, target function, registration kind, evidence) |
| `ensure_deep` | `symbols` (required), `jobs`, `wait` (default true) | L1 lazy deep analysis: parse owning TUs, analyze the requested functions, mark deep_ready; per-symbol status/latency/facts |
| `deep_status` | `symbols` (optional) | deep_ready coverage (`total_functions/deep_ready/pending/failed/coverage`) and per-symbol status |
| `prewarm` | `limit`, `jobs`, `background` | analyze the hottest queried pending symbols (L2 queue); background=true returns immediately |

### Unified backend (`ca serve`)

Instead of opening the database per process, agents can proxy stdio MCP to a
long-lived backend: `ca mcp --serve-url http://127.0.0.1:8765`. The backend
`/mcp` is a stateless MCP HTTP JSON-RPC endpoint with the same 18 read-only tools
as `ca mcp --db`; there is no session state, server-initiated request or AI
explanation feature.

### Federated mode (`--db-dir`)

`ca mcp --db-dir <dir>` (or repeated `--db`) serves a read-only federated view
over all shard databases in the directory; every merged row adds a `shard`
field and `stats` returns aggregate totals plus `shards`/`skipped`. `graph`
merges nodes/edges across shards and candidate edges keep
`source/candidate/kind` plus `shard`; dashed candidate edges are the possible-target set. The server still exposes the
same 18 tools. Write-oriented deep tools are rejected with
`FEDERATED_READ_ONLY` because deep state is shard-local: start a single-`--db`
server for `ensure_deep`/`prewarm`; `deep_status` returns read-only per-symbol
`deep_ready`/`shard` information. Unreadable shard files are skipped (with
stderr warnings); if no shard is readable the server exits 1. Shard databases
may be served in place (with `-wal`/`-shm`) or as single-file copies (read-only
`immutable=1` fallback, slightly slower; shards must be static while queried).

```sh
ca mcp --db-dir /home/yu/CA_OH/index
# HTTP: ca mcp --db-dir /home/yu/CA_OH/index --transport http --port 8765
```

### `graph` result schema

```json
{
  "root": "syscall",
  "direction": "forward",
  "depth": 2,
  "nodes": [{"id": "fn:..", "name": "syscall", "file": "kernel/syscall.c", "line": 131, "depth": 0, "direction": "forward"}],
  "edges": [{"caller_name": "syscall", "callee_name": "sys_open", "kind": "direct",
             "source": "direct", "candidate": false,
             "is_resolved": true, "call_file": "kernel/syscall.c", "call_line": 143,
             "depth": 1, "cycle": false}],
  "cycles": [], "truncated": false,
  "stats": {"nodes": 54, "edges": 178, "cycles": 0}
}
```

Direct calls have `source = "direct"`; function-pointer / virtual candidates
from `call_targets` have `source = "candidate"`, `candidate = true`, their
resolution `kind` and a human-readable `reason` (registration site, override
depth/relation, argument passing, ...). Candidate edges are the set of possible
targets at an indirect call site, not a scored prediction; they are rendered
dashed as an unordered target set. Nodes and edges are sorted
deterministically; `truncated` means the node limit stopped the traversal.

## Protocol examples

Minimal stdio session (initialize, list tools, call one tool, then EOF):

```sh
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"demo","version":"0"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
  '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"graph","arguments":{"symbol":"syscall","depth":2}}}' \
  | .venv/bin/ca mcp --db /home/yu/CA_OH/openharmony.db
```

`detail`, `graph`, `branches`, `assignments`, `returns` and `resources` add a
`deep_ready` flag (the fact lists also add `pending: true` when a function has
not been deep-analyzed yet); `leaks` reports `deep_ready: null` because findings
span functions. The server must be started with `--repo` and `--compdb` to serve
`ensure_deep`/`prewarm`; without them those tools return `DEEP_UNAVAILABLE`.
Layered-indexing semantics are documented in [layered-indexing.md](layered-indexing.md).

Every `tools/call` result is a text content block containing one JSON envelope:

```json
{"content":[{"type":"text","text":"{\"ok\": true, \"tool\": \"callers\", \"data\": {\"items\": [...], \"count\": 3, \"limit\": 50, \"offset\": 0}}"}],"isError": false}
```

Tool-level errors set `isError: true` and preserve the same envelope:

| Error code | Meaning |
|---|---|
| `INVALID_ARGUMENT` | missing/blank argument, bad `direction`/`depth`/`limit` |
| `NOT_FOUND` | `detail` symbol not found |
| `UNKNOWN_TOOL` | tool name not in `tools/list` |
| `DB_NOT_FOUND` | `--db` path is missing |
| `DB_ERROR` | store/database failure |

JSON-RPC level errors use standard codes: `-32700` parse error, `-32600`
invalid request, `-32601` method not found, `-32602` invalid params, `-32603`
internal error.

## Connecting from an agent

Local (CA installed in the same environment):

```python
# any MCP-capable client; conceptually:
client.start_stdio(["/home/yu/CA_OH/venv/bin/ca", "mcp", "--db", "/home/yu/CA_OH/openharmony.db"])
client.call_tool("find_symbol", {"pattern": "OHOS_", "limit": 20})
```

Remote over SSH without opening ports (recommended; CA code and DB stay on the
source server):

```sh
ssh -o BatchMode=yes yu@192.168.0.100 \
  '/home/yu/CA_OH/venv/bin/ca mcp --db /home/yu/CA_OH/openharmony.db'
```

The agent spawns that SSH command as its stdio MCP server. For clients that
need HTTP, tunnel the HTTP port instead:

```sh
ssh -N -L 8765:127.0.0.1:8765 yu@192.168.0.100
# client endpoint: http://127.0.0.1:8765/mcp , health: http://127.0.0.1:8765/health
```

If you must bind to a LAN address (`--host 0.0.0.0`), put a firewall in front of
port 8765; there is no authentication layer.

## Full-corpus shards

Phase-3 acceptance indexes OpenHarmony in eight shards under
`/home/yu/CA_OH/index/` (`oh_S1.db` … `oh_S8.db`, compdb
`/home/yu/CA_OH/corpus/openharmony/shards/shard_S<N>.json`, main analyze
scope). Each shard is a complete CA database; point one MCP server at the shard
that contains the module you are investigating, or run one server per shard on
different ports:

```sh
# on the server (stdio is the recommended agent transport)
/home/yu/CA_OH/venv/bin/python -m ca mcp \
    --db /home/yu/CA_OH/index/oh_S1.db \
    --repo /home/yu/CA_OH/openharmony \
    --compdb /home/yu/CA_OH/corpus/openharmony/shards/shard_S1.json

# HTTP per shard (localhost only; use SSH tunnels from the developer machine)
/home/yu/CA_OH/venv/bin/python -m ca mcp --db /home/yu/CA_OH/index/oh_S1.db \
    --repo /home/yu/CA_OH/openharmony \
    --compdb /home/yu/CA_OH/corpus/openharmony/shards/shard_S1.json \
    --transport http --port 8765
# from the developer machine:
ssh -N -L 8765:127.0.0.1:8765 yu@192.168.0.100
# MCP endpoint: http://127.0.0.1:8765/mcp    health: /health
```

Verify a shard quickly with
`python scripts/verify_shard_mcp.py --db <shard.db> --repo <repo> --compdb
<shard.json> --out <report.json>`: it drives the real stdio server (all 15
tools, `deep_status`, `ensure_deep` first call and cache-hit latency, `graph`)
and an HTTP server (`/health` + `POST /mcp` tools/list) with a pure-stdlib
client. Candidate-edge sampling for analyze review uses
`python scripts/sample_candidates.py --db <shard.db> ...` (totals, kind/reason samples, direct-call control, graph/DOT integration). Lazy deep analysis needs `--repo`/`--compdb` exactly
as above; `ensure_deep` parses only the symbol's TU, so it is safe to call
against a finished shard while other shards are still indexing.

## Server verification

```sh
rsync -az --delete --exclude .git --exclude .venv --exclude .tmp \
    ./ yu@192.168.0.100:/home/yu/CA_OH/ca/
ssh yu@192.168.0.100 'cd /home/yu/CA_OH/ca && /home/yu/CA_OH/venv/bin/python -m pytest -q'
ssh yu@192.168.0.100 'cd /home/yu/CA_OH/ca && bash scripts/server_bootstrap.sh'
```

The smoke test drives the same stdio protocol as above and fails the script if
`tools/list` or `tools/call` does not answer with the expected envelopes.

## Limitations

- Queries reflect the last `ca index`/`ca update` run; the MCP server does not
  watch the filesystem by itself (see [incremental.md](incremental.md) for the
  offline/online model and watcher design).
- One database per server process; multi-project/workspace union queries are a
  later phase.
- Candidate edges require an index built by a store version that persists
  `call_targets`; older databases serve direct calls only and report
  `source = "direct"` on every edge.
- Pagination uses offset slicing, not cursors; very large offsets fetch extra
  rows before slicing.
