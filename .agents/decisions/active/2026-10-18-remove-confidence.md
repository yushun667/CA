# Decision: remove confidence scores from indirect-call resolution

Status: active
Date: 2026-10-18

## Context

Callback/function-pointer and C++ virtual dispatch resolution emitted candidate
`CallTargetRecord`/`RegistrationFact` rows with a `confidence` float (heuristic
0.4-0.95) and filtered/ranked candidates by that value. The numbers were
hand-assigned, not calibrated against ground truth, yet downstream consumers
(CLI tables, MCP output, graph DOT, LLM agents) used them as if they were
probabilities, and the UI could only "hide" them after the fact. The user
requiresthat the analysis layer must not express confidence at all: indirect
resolution outputs a set of *possible* call targets plus auditable evidence,
not a scored prediction.

## Decision

Confidence is removed from the analysis contract end to end:

1. Core schema (v2): `CallTargetRecord.confidence` and
   `RegistrationFact.confidence` are deleted; the records carry the candidate
   target set and a human-auditable `reason` (registration site, override
   relationship, argument passing, getter return, ...). Docstrings state the
   candidate-set semantics.
2. Analyze: `callbacks.py`, `registrations.py`, `virtual_dispatch.py` no longer
   compute, filter, subtract or sort by confidence. All signature-compatible,
   qualifier-matching candidates are retained; bounds are purely counts
   (`max_candidates_per_call`, `max_targets`, override depth <= 3). Dedupe and
   representative selection use natural keys and deterministic order;
   duplicate evidence is merged into `reason`. `AnalyzeOptions` drops
   `call_target_min_confidence`.
3. Store writer: candidate rows are written without a confidence binding;
   `STORE_SCHEMA_VERSION` stays 7 in this change. The storage layer owns schema
   v8 (dropping `call_targets.confidence`/`callback_registrations.confidence`
   and removing query-side confidence) as a separate logical change.
4. Consumers: CLI (graph/query/`resolve-callbacks`), all MCP tools, UI/API,
   scripts and current docs emit no confidence; `--min-confidence` is removed;
   dashed candidate edges mean the possible-target set.

## Reasons

- The scores were fabricated heuristics; presenting them as probabilities
  over-claimed precision and biased consumers toward "top" candidates.
- Candidate-set + evidence is the honest contract: every emitted target is
  defensible from a registration site, override relation, argument pass or
  getter return, and ambiguity is resolved by the consumer, not by a score.
- Removing filtering by confidence also removes an implicit recall/precision
  tuning knob; explicit count caps bound volume while retaining all evidence.
- Direct calls are unchanged: candidates remain additional rows and never
  replace a statically resolved edge.

## Alternatives considered

- **Keep the field but hide it in the UI/CLI**: rejected by the user; hiding
  does not change the analysis contract or downstream agent behaviour.
- **Keep internal scores but stop persisting them**: still leaks into
  `AnalysisResult`/`CallTargetRecord` and cannot be audited; rejected.
- **Calibrate the heuristics with labelled data**: no labelled ground truth and
  outside the current phase; even calibrated scores would still be predictions
  the user does not want.
- **Drop ambiguous candidates entirely** (precision-first without scores):
  loses legitimate multi-target evidence (e.g. two implementations registered
  to the same table slot); rejected in favour of bounded candidate sets.

## Consequences

- Consumers must treat every `indirect_candidate`/`virtual_candidate` row as
  possible, not probable; ordering is deterministic but not a ranking.
- Candidate volume grows where thresholds previously cut weak matches; bounded
  caps per call site/function keep output manageable.
- Schema v8 drops the columns/keys; old databases upgrade on open.
  Consumer cleanup is complete; no confidence is emitted. Historical
  benchmark reports may mention past scores.
- `reason` strings carry the evidence (registration path/file/line, override
  depth, argument index, getter) and are merged on dedupe instead of
  confidence-based selection.

## Validation and risks

`tests/test_analyze_callbacks.py`, `tests/test_analyze_crosstu.py`,
`tests/test_analyze_virtual.py` assert candidate sets, evidence strings,
dedupe and caps (no numeric confidence). `tests/test_store_*` construct
`CallTargetRecord`s without confidence and assert exported/merged edges as
sets; the full suite passes (455 passed, 3 skipped). Report and docs
(`cross-tu-callbacks`, `cross-tu-virtual-dispatch`) describe candidate sets.
Risk: consumers not yet migrated may still print a `confidence` key from the
legacy DB column/default until storage v8 and the consumer cleanup land; they
must not interpret it as a score.

## 关联与取代

- 相关架构或问题：docs/cross-tu-callbacks.md、docs/cross-tu-virtual-dispatch.md
- 取代的决策：2026-10-18-virtual-dispatch.md 中“override depth 置信度”部分（候选规则仍有效）
- 被以下决策取代：无
