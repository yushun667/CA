# Decision: OpenHarmony-first phase-3 ingestion and execution strategy

Status: active
Date: 2026-10-18

## Context

The tool's users include debug, coding and test-generation AI agents plus human
programmers (UI last). Source trees must eventually include OpenHarmony, Yocto,
Android and Ubuntu; the user chose OpenHarmony first, at its current version,
C/C++ only. The OpenHarmony tree lives on the Linux server
`yu@192.168.0.100` (`~/OH_6.1/src`, 500 repo projects, product opi5plus/board
rk3588); the original tree must stay read-only. The user asked for offline full
indexing plus realtime incremental updates (never realtime full parsing), CA
running on the source machine, and queries through MCP.

## Decision

- Work on a mirrored working copy at `/home/yu/CA_OH/openharmony` (rsync of the
  original source excluding build output and repo object store); never modify
  `~/OH_6.1/src`.
- Generate real compile arguments offline from the OpenHarmony GN build graph
  (`out/rk3588/build.ninja`) with `ninja -t compdb` / GN export; no full compile
  is required for corpus generation. Fall back to the existing Docker builder
  image if the host environment cannot run GN.
- Index C/C++ only in phase 3; other languages and other OS trees are later
  phases.
- Separate two modes: an offline batch that builds the full baseline snapshot
  (dev validation on a repo subset, acceptance on the full C/C++ corpus) and a
  realtime incremental path that reparses only changed translation units.
- Run CA on the source machine with a user-level Python 3.11 environment; keep
  source and index DB on the server and expose queries via MCP over the local
  network / SSH. The UI is a later phase.

## Reasons

The kernel-scale phase already showed that per-TU parse/analyze dominates cost,
so full-tree realtime parsing is not viable; an offline snapshot plus per-TU
delta updates gives interactive query latency without re-parsing everything.
The source machine has the build environment and avoids shipping a 100 GB tree
over the network. GN-to-compdb gives real flags without spending hours on a
full compile.

## Alternatives considered

- **Index on the macOS workstation**: requires copying/streaming the source tree
  and lacks the OpenHarmony build toolchain; rejected.
- **Realtime full parse**: explicitly rejected by the user and infeasible at
  scale.
- **Build the full OpenHarmony image just to get flags**: unnecessary; GN graph
  generation yields the same compile commands.
- **Start with other OS trees or languages**: deferred by the user's priority.

## Consequences

- Corpus generation depends on GN working in the working copy (host or
  Docker); failures are reported instead of synthesizing flags.
- Full acceptance indexing is a long offline batch job on the server; the dev
  subset keeps iteration fast.
- MCP is exposed only on the user's LAN/SSH path; authentication is future work.
- Callback/function-pointer edges are candidate edges with confidence, not
  sound points-to results.

## Validation and risks

Acceptance: real C/C++ compdb from the current OpenHarmony snapshot; ≥95% parse
success on a sampled set; dev-subset offline index queryable through MCP; full
C/C++ acceptance batch completes; call graph and candidate edges verified on
labeled fixtures. Risks: GN environment breakage, disk/time growth for the full
corpus, and MCP network exposure.

## Relations and supersession

- Related architecture: [overview.md](../../architecture/overview.md)
- Related decisions: [compile-command policy](2026-10-17-compile-command-policy.md),
  [incremental indexing](2026-10-17-incremental-indexing.md),
  [FTS5 symbol search](2026-10-17-fts5-symbol-search.md)
- Supersedes: none
- Superseded by: none
