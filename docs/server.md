# CA unified backend service (`ca serve`)

`ca serve` is a long-lived backend for browser and agent clients. It owns
read-only query access (single database or federated shards), a durable generic
job queue (separate writable state database) and the stateless MCP endpoint
`/mcp`; browsers consume `/api/*` + `/events`, agents consume `/mcp` or the stdio
proxy `ca mcp --serve-url`. There is no built-in AI explanation feature.

## Start

```sh
# single DB: queries are read-only; generic jobs go to <db>.serve-state.db by default
.venv/bin/ca serve --db /home/yu/CA_OH/openharmony.db --port 8765

# federated shards: generic jobs default to <db-dir>/.ca/jobs.db
.venv/bin/ca serve --db-dir /home/yu/CA_OH/index --port 8765

# custom state DB / repo context for deep tools
.venv/bin/ca serve --db ca.db --state-db /tmp/ca-jobs.db --repo /path/to/repo
```

- Default bind `127.0.0.1:8765`; `--host 0.0.0.0` is for trusted networks only
  (there is no authentication).
- Shard databases stay read-only: the service never writes them.
- SIGINT/SIGTERM stops the server gracefully; interrupted `running` jobs are
  requeued on the next startup.

## HTTP API

| Method | Path | Description |
|---|---|---|
| GET | `/api/health` | status, database, `features:{jobs,sse,mcp,federated}` |
| GET | `/api/search?q=&limit=&offset=` | symbol search (same as local UI) |
| GET | `/api/function/<name_or_id>` or `/api/function?symbol=` | function detail |
| GET | `/api/callers`、`/api/callees`、`/api/graph`、`/api/stats`、`/api/callbacks` | existing query endpoints |
| GET | `/api/jobs?kind=&status=&limit=&offset=` | generic job list + `stats` |
| GET | `/api/jobs/<id>` | one job |
| POST | `/api/jobs` | create `{kind,payload?}` |
| POST | `/api/jobs/<id>/claim` | atomically claim a queued job (`worker`) |
| POST | `/api/jobs/<id>/result` | complete a running job with `{result}` |
| POST | `/api/jobs/<id>/fail` | fail a running job with `{error}` |
| POST | `/mcp` | stateless MCP JSON-RPC (initialize/tools/list/tools/call) |

Success envelope `{"ok":true,"data":...}`; errors `{"ok":false,"error":{"code","message"}}`.
`POST /api/jobs` returns 201; unknown job 404; state conflicts 409.

```sh
curl -s http://127.0.0.1:8765/api/health
curl -s -X POST http://127.0.0.1:8765/api/jobs \
  -H 'Content-Type: application/json' \
  -d '{"kind":"update","payload":{"note":"generic background task"}}'
curl -s 'http://127.0.0.1:8765/api/jobs?status=queued'
curl -s -N http://127.0.0.1:8765/events
```

## Generic jobs and SSE

Jobs are a generic background-task queue (kinds are free-form strings, e.g.
`ensure_deep`, `prewarm`, `update`; no explain kind or AI payload is special).
States: `queued → running → done | failed`; `requeue` re-queues a failed/running
job. Job keys: `id,kind,status,payload,result,error,claimed_by,attempts,created_at,updated_at`.
The queue is durable (`JobQueue` in the state DB, atomic claim/lease, restart
recovery) and generic; no worker is bundled except the HTTP claim/result API.

`GET /events` is an SSE stream of job changes:

```
id: 1
data: {"type":"job.updated","event":"created","event_id":1,"job":{...}}

id: 2
data: {"type":"job.updated","event":"claimed","event_id":2,"job":{...}}

id: 3
data: {"type":"job.updated","event":"done","event_id":3,"job":{...}}
```

- `event` ∈ `created|claimed|done|failed|requeued`, `job` is the full snapshot.
- 15s `: ping` heartbeats; reconnect with `Last-Event-ID` or `?since_id=N` to
  replay missing events from `job_events`.
- Event ids are globally monotonic; the last 10000 events are kept.

## MCP endpoint (stateless)

`/mcp` is a standard stateless JSON-RPC endpoint (initialize/tools/list/tools/call)
with the same 18 read-only tools as `ca mcp --db`. `ca mcp --serve-url URL` proxies
stdio JSON-RPC lines to it; no session state or server-initiated requests exist.

```sh
ca mcp --serve-url http://127.0.0.1:8765
```

## Verification and performance

- `tests/test_server_serve.py`: health/query endpoints, stateless MCP (18 tools),
  generic job loop + SSE replay, restart requeue, stdio proxy, federated shard
  stats, removed-explain CLI/API checks.
- Full suite: see the project test summary (locally `pytest -q`).
- Local benchmark (xv6 DB, `benchmarks/results/server_bench.json`): health/search
  p50 ≈2 ms, MCP detail p50 ≈5 ms, generic job create→claim→done p50 ≈5 ms.

## Limits and security

- Single-writer for jobs; queries are read-only and concurrent. Shards never
  participate in write transactions (no cross-shard transactions).
- Deep/index executors are not bundled: claim/complete generic jobs via HTTP or
  keep using the CLI for `ensure_deep`/`prewarm`/`update`.
- No authentication: loopback only (SSH tunnel for remote). Single-file shard
  copies use the read-only `immutable=1` fallback and must be static while queried.
- The state DB is a service-owned SQLite file (WAL); copy `-wal/-shm` together
  when backing it up.
