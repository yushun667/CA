# Agent Utility Evaluation (xv6 Pilot)

Status: xv6 pilot + 6-run and 45-run real-LLM evaluations complete (2026-10-18); OpenHarmony extension pending. Artifacts:
[benchmarks/agent_eval/](../../benchmarks/agent_eval/README.md).

## Purpose

Measure what an AI agent gains from CA facts versus reading source directly, for
three consumers: debug, coding (impact analysis) and test generation. Focus:
cross-file call chains (large context, expected high value) vs function-local
CFG/dataflow (expected low marginal value).

## Method

Three conditions on the same tasks: **A** source-only (grep/read); **B** CA
basic index (functions, callers, callees, detail/positions); **C** CA basic plus
deep facts (branches, returns, resource ops, leaks, recursive graph). 15 xv6
tasks (5 per class), 2 repetitions, randomized condition order, same extractor
rules; ground truth = 322 source-validated location facts; metrics = fact-level
P/R/F1 plus bytes read, tool calls and latency. This pilot is a deterministic
retrieval-policy run, not an LLM-agent run (see Limitations).

## Headline results (mean F1 / bytes / tool calls)

| Class | A source | B basic | C deep |
|---|---|---|---|
| debug | 0.966 / 12.1 KB / 2.2 | 0.667 / 3.7 KB / 1.4 | 1.000 / 10.3 KB / 5.2 |
| coding | 0.997 / 38.2 KB / 9.6 | 1.000 / 4.7 KB / 3.2 | 1.000 / 7.7 KB / 1.0 |
| test-gen | 0.991 / 14.4 KB / 2.0 | 0.579 / 3.7 KB / 1.0 | 1.000 / 11.7 KB / 4.0 |

## Conclusions

1. **Call graph and symbol/position facts are the highest-value capability.**
   Direct caller sets cost 4.7 KB and 1 query versus 38.2 KB and 9.6 shell calls
   for source reading (−88 % context, −90 % calls). A 2-hop reverse impact query
   costs 1 graph call versus 12 basic calls or 23 source calls.
2. **Function-local CFG/dataflow facts add little information** for xv6-sized
   functions: source reading of the function body already reaches F1 0.97–0.99;
   deep facts reach 1.0 at similar byte cost and more queries (+3.4 pt debug,
   +0.9 pt test-gen). Their value is structured, machine-consumable enumeration,
   not new facts.
3. **B alone is insufficient** for debug/test-gen (recall 0.51/0.41); it is the
   right default for coding impact analysis and as the entry point for graph
   traversal.
4. **Default:** function list/detail/signatures/positions, direct callers and
   callees, depth-limited/reverse graph, per-function summaries (branch count,
   error-return count, lock pairing). **On demand:** full branches/returns/
   resource lists, assignments, CFG blocks, findings. Findings must remain
   advisory (the only DB leak alert in the pilot is a false positive).

## Failure cases

- Syscall dispatch is a function-pointer table; `call_targets` is empty, so
  `sys_open`/`fork` entry edges are missing from the call graph.
- The `proc_mapstacks` kalloc leak finding is a false positive (kernel page
  table lives for kernel lifetime); agents may report it as a bug.
- Full deep-fact JSON is verbose (id/block/col fields), so byte savings can
  disappear in single-function scenarios; field projection and batching needed.

## Limitations and next step

Deterministic policy pilot, no LLM run (sub-agent delegation unavailable).
Ground truth is DB-enumerated then source-validated, so C's perfect score is
near-tautological; xv6 is small, which favors A. Next step: real LLM agent A/C
experiment (2 tasks per class, 2 repetitions), then an OpenHarmony larger-repo
re-run with the same protocol.

## Reproduction

```bash
PYTHONPATH=src .venv/bin/python benchmarks/agent_eval/run_eval.py --build-tasks
PYTHONPATH=src .venv/bin/python benchmarks/agent_eval/run_eval.py \
  --out benchmarks/agent_eval/results/pilot_xv6.json
```

A **45-run real-LLM matrix** (15 tasks x A/B/C, REPORT.md §7) refines this:
B ≈ C > A on structural fact coverage (combined 0.898 / 0.900 / 0.869; A→B and
A→C +2.9/+3.1 pt, sign-test p=0.065) with zero hallucinated file:line locations;
debug is the clearest CA win (C +4.4 pt F1 and −39 % context vs A), test-gen
favours B (+10.9 pt F1 vs A), coding is at ceiling for B/C. CA still costs more
context overall (median A 23.8 KB < C 31.2 KB < B 56.3 KB) because agents dump
verbose `detail` output — compact/projected and batched queries are the top
optimization backlog items (OPTIMIZATION_BACKLOG.md). Source reading keeps the
semantic edge in the human-scored subset.

Full report with per-task deltas: [benchmarks/agent_eval/REPORT.md](../../benchmarks/agent_eval/REPORT.md).
