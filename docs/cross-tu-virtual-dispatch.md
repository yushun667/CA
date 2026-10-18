# Cross-TU C++ virtual dispatch candidates

C++ virtual calls are resolved statically to the declared target, so callers of
an override look unreachable across a code base (`Base::run()` call sites do not
link to `Derived::run()`). This document describes the additive virtual-dispatch
metadata, the candidate-edge rules and the current limitations. The include
dependency contract used by incremental indexing lives in
[incremental.md](incremental.md).

## Data

The parser records metadata on the existing core records (schema v2):

| Record | Field | Meaning |
|---|---|---|
| `FunctionRecord` | `is_virtual`, `is_pure_virtual` | method dispatch flags |
| `FunctionRecord` | `overrides` | ids of directly overridden declarations (`clang_getOverriddenCursors`) |
| `TypeRecord` | `base_types` | direct base type ids (multi-inheritance ordered) |
| `CallRecord` | `kind="virtual"`, `is_virtual` | virtual call site, static target in `callee_id` |

Store schema v7 persists them as `functions.is_virtual`,
`functions.is_pure_virtual`, `functions.overrides_json`, `types.bases_json`,
plus direct-edge tables `virtual_overrides(function_id, base_function_id)` and
`type_bases(type_id, base_type_id)`. Upgrade is idempotent: opening an older
database adds the missing tables/columns in place.

## Candidate rules

- **Same TU**: `ca.analyze.virtual_dispatch` turns virtual call sites into
  `virtual_candidate` `CallTargetRecord`s for every override of the static
  target visible in that TU (direct override 0.6, depth 2 0.5, depth 3 0.45,
  threshold `AnalyzeOptions.call_target_min_confidence`).
- **Cross TU**: query time merges `virtual_candidate` edges on top of direct
  calls in `store.load_forward_edges`/`load_reverse_edges` using the persisted
  override relation from all indexed shards. No extra materialization step is
  needed; old databases without the tables degrade to direct edges.
- For a call through `Base::m`, candidates are the known overrides of `m`
  (transitive override chain, at most 3 levels). `callees` returns them with
  `kind="virtual_candidate"`, a lower `confidence` than the direct edge and a
  human-readable `reason`; `callers` of `Derived::m` include virtual call sites
  whose static target is an ancestor method.
- Direct call edges are never replaced or upgraded: candidates are additional
  rows (`source != "call"`), same ids via `ca.core.ids.call_target_id`.

## Limits

- The dynamic type is unknown, so candidates are possibilities, not proven
  targets. No override metadata means no candidate (no guessed edges).
- Multiple inheritance, templates and dependent types can broaden the candidate
  set conservatively; candidates are capped per function
  (`max_call_targets`, default 64) and deduplicated.
- Virtual calls are recognized from calls whose target kind is a C++ method;
  virtual destructors are not tracked as virtual calls (no candidate edge).
- Candidate computation adds a bounded recursive CTE per callers/callees query;
  it is skipped when the virtual tables are missing or the store is read-only
  older layout.

## Tests

`tests/test_parse_virtual.py`, `tests/test_analyze_virtual.py`,
`tests/test_store_virtual.py` (metadata, same-TU/cross-TU candidates, no fake
edge without overrides, idempotent database upgrade).
