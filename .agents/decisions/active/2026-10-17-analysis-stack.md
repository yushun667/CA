# Decision: analysis stack and parser backend

Status: active
Date: 2026-10-17

## Context

The tool must deliver high-precision semantic facts (function/type identity,
call targets, source locations) for large OS C/C++ repositories at high
throughput. The development host is macOS arm64 with Apple clang 21 but without
LLVM development headers, and package installation is not available. Python
3.11 and pinned pip dependencies are available.

## Decision

Use Python 3.11 with bundled libclang 18.1.1 (cindex) as the semantic parser,
one process per worker, and build CFG/data-flow analyses over Clang cursors.
Keep records and module interfaces Clang-independent so a faster backend can
replace the parser later.

## Reasons

libclang exposes the semantic AST (USRs, types, linkage, referenced
declarations, source ranges) without a host LLVM SDK, and the shared library
installs as a pinned pip dependency. Process-level parallelism fits libclang's
per-TU model and avoids GIL contention. Implementation and debugging cost for
this phase is much lower than C++ LibTooling.

## Alternatives considered

- **C++ LibTooling/Clang tool**: fastest and exposes `clang::CFG`; rejected for
  this phase because LLVM development headers are absent, installing them needs
  package privileges, and build/integration cost is high. Revisit in phase 2 if
  throughput targets are missed.
- **tree-sitter only**: very fast syntax tree but no semantic resolution
  (typedefs, overloads, implicit declarations), so call/type precision would
  miss the goal.
- **`clang -ast-dump=json`**: full semantics without libraries, but
  serialization volume and JSON post-processing dominate large TUs.
- **clangd index**: good symbol index, but no stable AST/CFG/data-flow API for
  extraction.

## Consequences

- Throughput is bounded by libclang parse time plus per-cursor Python overhead;
  mitigated by process parallelism, single-pass extraction that shares the AST
  with analysis, and per-worker duplicate-function suppression.
- Macro expansion, function pointers, and system-header declarations keep known
  precision limits that must be measured and documented.
- The parser is isolated behind `ca.parse`, so a C++/Rust backend can be added
  without touching storage or CLI contracts.

## Validation and risks

End-to-end xv6 indexing, measured TU/s and LOC/s, and per-category
precision/recall on labeled fixtures. Main risk: Python overhead on
kernel-scale TUs; mitigate with phase-2 profiling and an optional native
backend.

## Relations and supersession

- Related architecture: [overview.md](../../architecture/overview.md)
- Supersedes: none
- Superseded by: none
