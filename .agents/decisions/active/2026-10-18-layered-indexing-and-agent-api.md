# Decision: layered indexing and agent-facing API policy

Status: active
Date: 2026-10-18

## Context

Deep analyzing every translation unit of OpenHarmony is infeasible: each TU
contains thousands of header inline definitions, header analysis accounted for
90.6% of analyze CPU, and a full deep pass was estimated at 5-10 days. The
real-agent evaluation (45 xv6 runs, A=source, B=CA facts, C=CA+deep) showed
that call/symbol facts are useful but not automatically cheaper: median context
was 23.8 KB for A, 56.3 KB for B and 31.2 KB for C because `detail` output is
verbose. Coding was near ceiling under all conditions; debug benefited most
from deep facts (C vs A +4.4pt F1); test-gen did best with B (B vs A +10.9pt).
No answer invented a file:line location. CA gaps (header declarations,
collapsed return blocks, missing lock dominance/constants/refcounts) were
explicitly reported by agents.

## Decision

- Build a fast base layer (L0) over each unique source file once: symbols,
  types, fields, direct calls and locations; queries work immediately and
  `deep_ready=0`.
- Compute deep facts (CFG, branches, assignments, returns, resource ops,
  call_targets, findings) lazily per requested function or module (L1), cache
  them in `deep_status`, and prewarm frequently queried modules in a bounded
  background queue (L2). Offline acceptance still precomputes deep facts for
  main-file definitions (`--analyze-scope main`) but not for every header
  inline function.
- Keep MCP/CLI facts layered: base facts and `deep_ready` are always returned;
  deep fact lists are optional/on-demand; `ensure_deep` materializes them.
- Do not expose raw CFG blocks as the default agent API. Return semantic facts
  (conditions, return/error paths, resource lifecycle) and let `detail` remain
  the summary entry; prioritize compact/projected output, batch queries and a
  `brief` entry based on the evaluation backlog.
- Acceptance scope: unique source files (about 65k), excluding generated
  `out/`/`prebuilts/` trees; the variant matrix remains metadata, not separate
  acceptance rows.

## Alternatives

- Full deep analysis of all TUs: 5-10 days and hundreds of GB, rejected.
- Base index only: insufficient for debug/test-gen recall, rejected as the
  deep interface, kept as the default fast path.
- Expose raw CFG/JSON dumps: low value and high context cost per evaluation,
  rejected.
- Analyze header inline functions per including TU: redundant duplicates,
  rejected; global dedupe is used in `all` mode instead.

## Consequences

- Query semantics include `deep_ready`/pending states; agents must handle
  "not analyzed yet" honestly instead of inferring absence of facts.
- Deep coverage is partial by design; acceptance reports deep coverage and
  ensures base coverage is complete.
- The compact-output and header-declaration gaps become the top optimization
  work after phase 3.

## Validation

xv6 lazy deep: first analysis 10.4 ms/symbol, cached 5.3 ms/symbol; OH
unique-100 base index 87.5 s / 359 MB, `ensure_deep` cache hit about 1 ms.
45-run agent matrix and the optimization backlog are recorded in
`benchmarks/agent_eval/`.
