# Decision: Linux kernel corpus via Apple container

Status: active
Date: 2026-10-17

## Context

Phase 2 needs a real Linux kernel compile database to validate parsing and
analysis at kernel scale. The development host is macOS arm64 with no sudo;
kbuild cannot build Linux kernels natively on macOS (host tooling and ELF
linker assumptions). The Apple `container` CLI is installed and verified:
Ubuntu 24.04 guest, Linux 6.18 aarch64, virtiofs bind mounts, CPU/memory limits,
no host sudo required. CI for this repository is not configured yet.

## Decision

Build a pinned Linux kernel inside a pinned Ubuntu container image through Apple
container, natively for arm64, with `LLVM=1` (clang/lld). Generate the compile
database with the kernel's own
`scripts/clang-tools/gen_compile_commands.py` from the build tree, publish the
corpus under `.tmp/corpus/linux/` (ignored), and record the kernel tag/commit,
image digest, config and build command in metadata. Reproducible orchestration
lives in `ca.build.kernel_corpus`; when the container runtime is unavailable the
helper fails explicitly and no synthetic kernel database is presented as real.

## Reasons

This is the only route on this host to a genuine kernel compile database with
real generated headers, config and macros — the inputs that determine parsing
precision. The container is already installed and rootless from the project's
perspective, and virtiofs bind mounts let the host analyzer read the built tree
directly. Pinning the kernel and image keeps the corpus reproducible.

## Alternatives considered

- **Linux CI build**: the right long-term reproducibility path, but it requires
  repository/CI setup and slows iteration; kept as a phase-3 option.
- **Cross-build on macOS**: unsupported kbuild host assumptions and missing ELF
  toolchain; rejected.
- **Synthesized kernel flags**: generated headers/config make this unusable for
  kernel precision; rejected and explicitly forbidden as a substitute.
- **QEMU/Lima/UTM**: would require additional installation while Apple container
  is already available; rejected.

## Consequences

- The corpus is arm64-only; x86_64 kernel builds would need another image/config.
- Kernel build costs wall time and disk under `.tmp`; image/kernel pins are
  recorded so runs can be reproduced.
- Kernel compile databases expose real macro/config complexity to parser and
  analyzer, which is the point; failures are recorded per TU.

## Validation and risks

Acceptance: ≥300 TUs with real kernel flags, ≥95% parse success on a sampled
subset, and a measured kernel-subset index run. Risks: container/image
availability, network for apt/kernel source, and long build times; all are
recorded so the corpus can be rebuilt on another host.

## Relations and supersession

- Related architecture: [overview.md](../../architecture/overview.md)
- Related decisions: [analysis stack](2026-10-17-analysis-stack.md),
  [compile-command policy](2026-10-17-compile-command-policy.md)
- Supersedes: none
- Superseded by: none
