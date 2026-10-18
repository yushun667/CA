# Agent platform and server deployment

Status: phase 3 acceptance in progress; phase 4 adds the unified backend.

Deployment and agent query path on the source machine; current structure only,
rationale lives in `../decisions/active/`.

## Purpose and users

The analyzer serves three AI-agent users plus human programmers:
debug agents (symbol/call-graph context), coding agents (references, impact,
call relationships) and test-generation agents (branch conditions, resource
semantics, call structure). Phase 3 targets OpenHarmony C/C++ first.

## Components

| Component | Responsibility |
|---|---|
| `ca mcp` | MCP JSON-RPC server (stdio/HTTP); 18 tools (base facts, callbacks, batch/brief, deep tools), `--serve-url` proxy; stable envelopes and `deep_ready` |
| `ca.cli.deep` + `ca.store.deep` | L0 base facts; L1 `ensure_deep` materializes CFG/branches/returns/resources/call_targets per requested function; L2 prewarm; `deep_status` keeps L0 independent of deep coverage |
| `ca.pipeline` resilience | Chunk cap 16 TUs, broken-pool retry, wave-level pool rebuild (`CA_POOL_RECYCLE_CHUNKS`), stable-id idempotence, per-TU cache |
| Parser backend | Default libclang 18; OH uses matched libclang 15 via `CA_LIBCLANG_LIBRARY` (see decisions) |
| `ca graph` (`src/ca/cli/graph.py`) | Forward/reverse/both traversal (depth/limits, path/cycle, table/JSON/DOT); merges direct + candidate edges |
| `ca.analyze.callbacks` | Indirect-call candidate resolution (assignments, arrays, params, typedefs, return pointers) filtered by function-pointer signature; emits `CallTargetRecord(kind=indirect_candidate, confidence, reason)` |
| `ca.store.graph` / `ca.store.workspace` / `ca.store.deep` | Persist `call_targets`, graph helpers, workspace metadata and `deep_status`; L0 queries work with zero deep rows |
| Offline/incremental modes | `ca index` builds a snapshot; `ca update --changed-files ...` reparses changed TUs; see [docs/incremental.md](../../docs/incremental.md) |
| `ca ui` (`src/ca/ui/server.py`) | local read-only web UI: vanilla page + JSON API + SVG graph; loopback only; `--serve-url` proxies to the backend |
| `ca serve` (`src/ca/server/`) | unified backend for browser/MCP clients; see [unified-serve-backend.md](unified-serve-backend.md) |
| `ca.build.openharmony` | OpenHarmony corpus logic: repo manifest/projects, GN/ninja command construction, `ninja -t compdb` normalization, metadata; operates on the working copy only |

## Data flow and query path

```text
OpenHarmony working copy
  -> GN generation (out/<product>/build.ninja)
  -> ninja -t compdb (real C/C++ flags, no full compile)
  -> offline batch index on the source server (L0 base + main-file deep)
  -> sharded SQLite snapshots (one DB per shard/workspace)
  -> ca serve (single backend) -> /api/* + /events, /mcp, /api/jobs
  -> AI agent tools (via /mcp or `ca mcp --serve-url`)
       \-> ensure_deep / prewarm materialize L1 facts lazily and cache them
```

L0 queries return immediately with `deep_ready=0`; deep facts are materialized for
the requested function/module and cached. Incremental updates reparse only
changed TUs and invalidate their rows; never a realtime full parse.

## Server layout (not committed)

- `/home/yu/CA_OH/openharmony`: mirrored OpenHarmony source tree (original
  `~/OH_6.1/src` stays read-only).
- `/home/yu/CA_OH/ca`: deployed CA checkout (editable install).
- `/home/yu/CA_OH/venv`: Python 3.11 environment with pinned dependencies.
- `/home/yu/CA_OH/pylib15`: isolated matched clang-15 bindings; used with
  `run_ca_oh15.sh` for OpenHarmony parse acceptance.
- `/home/yu/CA_OH/corpus/openharmony/`: `compile_commands*.json`, shard
  compdbs and `metadata.json` (product, board, manifest revision, tool
  versions, generated-header targets, stability profile).
- `/home/yu/CA_OH/index/`: per-shard acceptance DBs (`oh_sN.db`), logs,
  `acceptance_summary.json`, failed/partial archives and EVIDENCE files.

## Interface and dependency boundaries

- MCP tools return `{ok, data, error}` envelopes with stable keys; base queries
  add `deep_ready` and deep lists carry `pending`; graph edges carry `kind`,
  `confidence`, `source`, `candidate`.
- Candidate edges are heuristic `may-call` sets, never mixed into direct edges.
- Workspace metadata uses store-local IDs (`ws:`/`prj:`/`var:`) and does not
  change the core record contract.
- Server access is LAN/SSH-only; no authentication layer exists yet.

## Limits

- Cross-TU callback candidates need a whole-program symbol table (not yet
  produced); struct-array callbacks, integer-address function pointers and C++
  member pointers are not modelled.
- Candidate targets are bounded per call site/function and carry confidence;
  cross-TU registration/global callback tables remain a known coverage gap.
- Deep coverage is partial by design; acceptance reports it separately from
  complete L0 coverage.
- Full-corpus acceptance is a long offline sharded batch; incremental updates are
  per changed TU.
