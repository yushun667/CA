# Agent platform and server deployment

Status: phase 3 acceptance in progress (OpenHarmony first).

This document describes how CA is deployed on a source machine and queried by AI
agents. It is a current-structure document; phase rationale lives in
`../decisions/active/`.

## Purpose and users

The analyzer serves three AI-agent users plus human programmers:
debug agents (symbol/call-graph context), coding agents (references, impact,
call relationships) and test-generation agents (branch conditions, resource
semantics, call structure). Phase 3 targets OpenHarmony C/C++ first.

## Components

| Component | Responsibility |
|---|---|
| `ca mcp` | MCP JSON-RPC server (stdio/HTTP); 15 tools: base facts (symbols, callers, callees, graph, detail, stats) + `ensure_deep`, `deep_status`, `prewarm`; stable envelopes and `deep_ready` |
| `ca.cli.deep` + `ca.store.deep` | L0 base facts; L1 `ensure_deep` materializes CFG/branches/returns/resources/call_targets per requested function; L2 prewarm; `deep_status` keeps L0 independent of deep coverage |
| `ca.pipeline` resilience | Chunks <=16 TUs, broken-pool retry (halved then per-TU), wave-level pool rebuild (`CA_POOL_RECYCLE_CHUNKS`), stable-id idempotence and per-TU cache |
| Parser backend | Default libclang 18; OH uses matched libclang 15 via `CA_LIBCLANG_LIBRARY` (see decisions) |
| `ca graph` (`src/ca/cli/graph.py`) | Forward/reverse/both traversal with depth and hard limits, path/cycle queries, table/JSON/DOT output; merges direct calls and candidate edges |
| `ca.analyze.callbacks` | Indirect-call candidate resolution: registration assignments, arrays, parameter passing, typedefs, return pointers; emits `CallTargetRecord(kind=indirect_candidate, confidence, reason)` filtered by function-pointer signature |
| `ca.store.graph` / `ca.store.workspace` / `ca.store.deep` | Persist `call_targets`, graph helpers, workspace/project/variant metadata and `deep_status`; `store_schema_version=3`; L0 queries work with zero deep rows |
| Offline/incremental modes | `ca index` builds a full offline snapshot; `ca update --changed-files ...` reparses only changed TUs; daemon design in [docs/incremental.md](../../docs/incremental.md) |
| `ca ui` (`src/ca/ui/server.py`) | local read-only web UI: vanilla page + JSON API + SVG graph; loopback only |
| `ca.build.openharmony` | OpenHarmony corpus logic: repo manifest/projects, GN/ninja command construction, `ninja -t compdb` normalization, metadata; operates on the working copy only |

## Data flow and query path

```text
OpenHarmony working copy
  -> GN generation (out/<product>/build.ninja)
  -> ninja -t compdb (real C/C++ flags, no full compile)
  -> offline batch index on the source server (L0 base + main-file deep)
  -> sharded SQLite snapshots (one DB per shard/workspace)
  -> ca mcp (stdio over SSH or HTTP on the LAN)
  -> AI agent tools
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
