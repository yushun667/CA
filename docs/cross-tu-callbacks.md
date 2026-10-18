# Cross-TU callbacks and function pointers

Intra-TU callback resolution (`ca.analyze.callbacks`) only sees stores and calls
inside one translation unit, so dispatch tables registered in one file and
invoked from another stay unresolved (xv6 `devsw`, OpenHarmony listener/ops
tables, `file_operations`-style tables). This document describes the cross-TU
registration fact model, the resolution pass, the logical dedupe rule and the
current limitations. Correctness expectations are in
[tests/test_analyze_crosstu.py](../tests/test_analyze_crosstu.py) and the
fixtures under `tests/fixtures/ast_crosstu_*.c`.

## Data flow

1. **Per-TU extraction** (`ca.analyze.registrations.extract_registrations`):
   every TU yields registration facts. A fact records `object_path`
   (`g_connManagerCb.OnDataReceived`, `devsw[CONSOLE].read`), target function
   id/name/USR, registration point `(tu_id, file, line, col)`, evidence text and
   `registration_kind`: `global_assign`, `struct_field`, `array_element`,
   `designated_initializer`, `registration_call`, `argument_pass`,
   `return_pointer`. Only **global** storage is recorded; assignments into local
   test structures are deliberately ignored, which removes the "same field
   name, different instance" false-positive family. The same pass emits neutral
   indirect call sites (callee path, function-pointer signature, caller
   identity).
2. **Persistence** (store schema v4): facts are written to
   `callback_registrations`; candidate edges are ordinary `call_targets` rows
   (`kind="indirect_candidate"`), and
   `callback_target_links(registration_id, call_target_id)` back-links each
   candidate to the registration evidence that produced it.
3. **Resolution** (`resolve_cross_tu_targets`): matches indirect call sites
   against facts from all TUs. Missing signatures suppress a candidate instead
   of emitting an unchecked edge.

## Matching rules

A match means membership in the call site's **possible target set**, not a
score: the resolver keeps every signature-compatible candidate and records the
evidence in `reason`. Candidates are bounded per call site
(`max_candidates_per_call`) and per function (`max_targets`); there is no
no score or ranking of any kind.

| Evidence | Recorded in `reason` |
|---|---|
| Exact global path match (no dynamic qualifier) | `g_ops_a.fn = impl_a at file:line` |
| Same base + field chain, same constant qualifier | registration path + qualifier kind |
| Dynamic call index vs constant registration index (table dispatch) | registration path + qualifier=`dynamic` |
| Callback passed as function argument across TUs | argument index + `file:line` |
| Function pointer returned by a getter | getter name + `file:line` |
| Same base + field chain with an unresolved wildcard qualifier | registration path + qualifier kind |

The base symbol and the complete field chain must match; field-name-suffix
matches across different bases are not produced. This is the qualifier-aware
rule that keeps `g_ops_a.fn` from matching a registration under `g_ops_b.fn`.

## Logical dedupe

Header-inline functions are instantiated in many TUs, so the same logical edge
can be reported once per TU. The resolver therefore dedupes by
`(caller_USR or caller_id, callee_USR or callee_id, call file, line)` by default
(`dedupe=True`; pass `dedupe=False` to inspect raw per-call-site edges). The
retained record is a deterministic representative chosen by natural-key order
(not by any score); duplicate reasons are merged
into its `reason` (`; also ...`) and every matching registration id links to the
retained `call_target_id`. Raw and deduped counts are both reported for
acceptance; coverage figures use the deduped ("logical") edge count.

## Validation

- Fixtures: 10 expected cross-TU edges, precision/recall = 1.0, covering all
  registration kinds, qualifier negatives and signature filtering.
- xv6-riscv full corpus: new edges `fileread -> consoleread` and
  `filewrite -> consolewrite` (`devsw[CONSOLE]` registered in `console.c`,
  called from `file.c`); total candidate edges 22 -> 24 with no regressions
  (`findings=2`, FreeRTOS `findings=0`).
- OpenHarmony S5/S7 shard validation (parse-only pass with the matched
  libclang15 toolchain): S7 adds 10 raw -> **8 logical** edges (2 header-inline
  duplicates), candidate callers 0.64% -> 0.82%; S5 adds 256 raw = 256 logical
  edges (no header-inline duplicates in this shard), candidate callers
  5.11% -> 5.48%. Combined: 264 new logical edges, candidate edges
  1,941 -> **2,205** (+13.6%); candidate callers 498 -> 529 out of 22,800
  indirect callers = 2.18% -> **2.32%**.
- Production-path sample precision: 10/10 verified against source (mbedtls
  global hook, alsa PCM ops, curl/libexif/libnl argument callbacks, HDF
  list/foreach comparators); 0 false positives in the sample.

## Limitations

- `struct`-array tables with multi-field chains (e.g. per-driver
  `ops[a].field[b]`) are matched by base + field + qualifier length; partial
  chains and nested function-pointer structs are not modeled yet.
- C++ virtual dispatch (`kind="virtual"`) is out of scope; this pass handles
  function-pointer registrations only.
- Registration through opaque setter APIs (a callback copied into an internal
  list by a library call) is recorded as a `registration_call` fact for audit,
  but cannot yet be joined to later dispatch sites.
- Facts are intra-TU global stores only; cross-function ownership and instance
  aliasing beyond global symbols are not tracked.
- Resolution needs target function signatures; when a target definition is
  missing from the batch (parse-degraded TUs, generated headers), candidates
  are dropped conservatively.
- Coverage on degraded corpora follows parse health: shards with many missing
  generated headers/non-stdinc TUs contribute fewer facts and candidates.
- `analyze_function` itself remains intra-TU; the cross-TU pass is a separate
  batch/post-processing stage.

## Entry points

- `extract_registrations(unit) -> (facts, call_sites)`
- `resolve_cross_tu_targets(call_sites, facts, functions, signatures=...,
  existing_edges=..., dedupe=True) -> (call_targets, links)`
- `extract_target_signatures(unit, ids)` for DB post-processing workers
- `analyze_cross_tu(units)` for single-process tests and small batches
- CLI `ca resolve-callbacks` and MCP/graph candidate rendering consume the same
  records (see [usage.md](usage.md) for command syntax).
