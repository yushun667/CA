# Decision: compile-command provenance policy

Status: active
Date: 2026-10-17

## Context

Extraction precision depends on the real compile flags (include paths, macros,
target ABI, language standard). Target repositories use CMake, Meson, plain
make, or the Linux kernel build system, and many do not ship a
`compile_commands.json`. Silently guessing flags would produce plausible but
wrong ASTs and make precision impossible to reason about.

## Decision

Acquire compile commands in this order and record provenance per TU:

1. load an existing `compile_commands.json` (file or build directory);
2. capture a real build through generated compiler wrappers (`CC`/`CXX`
   interception), which records the verbatim argv and working directory;
3. reconstruct Linux kernel commands from the build tree's `*.cmd` files;
4. synthesize flags only when no real source exists, always with
   `CompilationUnit.source == "synthesized"`.

The provenance is persisted in the database and surfaced by the CLI. Synthesized
commands are never presented as real build parameters.

## Reasons

Real flags are the single largest precision lever for Clang parsing. A wrapper
works for any build system that honors `CC`/`CXX`, while explicit exporters are
faster when available. The kernel format is common enough to support directly.
Declaring the fallback keeps the tool honest and lets users filter low-confidence
results.

## Alternatives considered

- **Require a compile database**: rejects many OS trees that never export one.
- **Always synthesize**: cheap to run but low precision and misleading.
- **Per-build-system parsers only**: brittle and endless (autotools, kbuild,
  custom scripts); the wrapper generalizes over them.
- **`bear` dependency**: not installed on macOS and still requires a successful
  build; our wrapper keeps the tool self-contained.

## Consequences

- Captured builds pay wrapper overhead and need a successful configure/build;
  failures must keep partial captures and report the exit code.
- Kernel `.cmd` normalization is kernel-specific and must drop `-o`/dependency
  noise while preserving defines, includes, and `-include` files.
- Synthetic TUs have lower precision and are marked as such everywhere.

## Validation and risks

xv6 is built with a captured cross-compiler invocation and produces a real
`compile_commands.json`; synthetic provenance is covered by tests; the database
records `source` per TU. Risk: build systems that bypass `CC`/`CXX`; detect and
document rather than silently degrade.

## Relations and supersession

- Related architecture: [overview.md](../../architecture/overview.md)
- Related decision: [analysis stack](2026-10-17-analysis-stack.md)
- Supersedes: none
- Superseded by: none
