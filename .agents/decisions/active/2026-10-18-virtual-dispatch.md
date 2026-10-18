# Decision: C++ virtual dispatch candidates

Status: active
Date: 2026-10-18

## Context

OpenHarmony dsoftbus/camera are C++ code bases whose call graphs hinge on virtual
dispatch (interface classes in headers, implementations in other translation
units). A virtual call `base->run()` resolves statically to the declared
`Base::run`, so override implementations (`Derived::run`, `Leaf::run`) are
invisible to `callers`/`callees`/graph queries, and same-TU-only analysis cannot
see implementations compiled in other TUs. The project needs explainable,
cross-TU candidate targets without changing the meaning of direct calls and
without requiring a pointer analysis.

## Decision

1. **Parser metadata (core schema v2)** - additive fields only:
   `FunctionRecord.is_virtual` / `is_pure_virtual` /
   `overrides` (ids of directly overridden declarations from
   `clang_getOverriddenCursors`) and `TypeRecord.base_types` (direct base type
   ids from `CXX_BASE_SPECIFIER`). Out-of-line definitions replace header
   declarations in the TU as before, but virtual/override metadata is merged, so
   the surviving record keeps dispatch facts.
2. **Store schema v7** (idempotent upgrade): `functions.is_virtual`,
   `functions.is_pure_virtual`, `functions.overrides_json`, `types.bases_json`,
   plus indexed edge tables `virtual_overrides(function_id, base_function_id)`
   and `type_bases(type_id, base_type_id)`.
3. **Same-TU candidates** (`ca.analyze.virtual_dispatch`): a virtual call site
   emits `CallTargetRecord(kind="virtual_candidate")` for every override of its
   static target visible in the TU. Confidence decreases with override depth
   (1 = 0.6, 2 = 0.5, 3 = 0.45), respects the configured minimum, is
   deterministic, deduplicated and capped (`max_call_targets`).
4. **Cross-TU candidates at query time**: `store.load_forward_edges` /
   `load_reverse_edges` merge virtual candidates from the persisted override
   relation of every indexed shard (recursive CTE, depth <= 3). Candidates are
   additional rows (`source != "call"`) with `kind`, `confidence` and a readable
   `reason`; direct edges always win on duplicates. `callers` of an override
   likewise include virtual call sites whose static target is an ancestor
   method.
5. **No materialization**: cross-TU candidates are computed during
   callers/callees/graph queries instead of being written to `call_targets` by
   an indexing pass.

## Reasons

- Direct-call semantics stay untouched; virtual targets are explicit candidates
  with type/confidence/evidence, so agents can distinguish fact from heuristic.
- Query-time merging uses the whole database regardless of ingestion order,
  shard boundaries or later incremental re-indexing; a materialized pass would
  have to be rerun whenever a shard changes and would duplicate the direct-call
  relation.
- Persisted override/base edges are small (one row per direct relation) and let
  same-TU analysis and cross-TU queries share the same ids (`ca.core.ids`).
- Old databases without the v7 tables degrade to direct/indirect edges instead
  of failing, keeping federated/legacy queries usable.

## Alternatives considered

- **Materialize candidates during `finalize()`**: candidates depend on the
  current override set; re-indexing a shard would rewrite millions of rows and
  candidate volume is query/limit dependent.
- **Overload `CallRecord.kind` or rewrite the static callee**: loses the direct
  fact and breaks existing direct-call contracts; rejected.
- **RTTI/points-to analysis**: out of scope for the L0/L1 index budget; candidate
  metadata is the deliberately limited substitute.
- **Same-TU only**: misses the main OpenHarmony case (interface header in one TU,
  implementation in another); rejected.

## Consequences

- Callers/callees queries on virtual-heavy databases pay one bounded recursive
  CTE; depth/limit caps keep it predictable.
- Candidate sets are conservative for templates, multiple inheritance and
  unknown dynamic types; virtual destructors are not tracked as virtual calls.
- v7-less or header-declaration-less indexes yield fewer/no candidates; no
  direct edge is ever invented.
- Candidates are not persisted, so `call_targets`-only consumers see same-TU
  analyze candidates; callers/callees/graph/MCP see both.
- Governance: `directory_agents.max_chars` 1500→1800 because the active index
  must list all 42 filenames of 14 triples; 13 triples had saturated it.

## Validation and risks

The virtual/include test files pass with the full suite (454 passed,
3 skipped). A partial OH dsoftbus_camera index (640 TUs, v7/v2) recorded 2,863
virtual calls, 4,175 virtual methods, 3,957 override relations and 1,310
candidate edges for 387 calls (13.5%). Risk: candidates are possibilities;
task-40 observes real coverage and noise.

## 关联与取代

- 相关架构或问题：docs/cross-tu-virtual-dispatch.md
- 取代的决策：无
- 被以下决策取代：无
