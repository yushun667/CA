# Issue: Windows header updates can miss dependent translation units

Status: open
Opened: 2026-10-19

## Symptoms and impact

On Windows, `ca update` may leave stale analysis after a header change, despite
the index containing that header's `tu_includes` dependencies. Full indexing,
queries, MCP and web startup are separate from this failure.

## Reproduction or trigger

Create an index containing a TU dependency on `include/os.h`, edit the header,
and update it on Windows. If the update plan labels the changed header as
`include\os.h`, dependent TU selection can return no matches and skip the
required reparse.

Reproduction test: `python -m pytest tests/test_e2e_graph_mcp.py::test_update_header_change_clears_cache`.
The existing index has two dependent TUs, but update exits 2 with
`changed header(s) are not included by any indexed TU`.

## Investigation

The Windows extended test run reproduced the header-update failure both with
native-package changes and in exported pre-packaging baseline `da9d1ba`. It is
an existing analyzer defect, not caused by freezing or installer layout. Three
other baseline failures concern Node encoding and subprocess test environments
that omit Windows `SystemRoot`; they are test-environment findings rather than
additional product limitations.

## Root cause

`tu_includes` stores POSIX-style include paths, while
`ca.cli.watcher.plan_changes` can produce Windows backslash labels.
`ca.cli.commands` passes these labels to dependency queries that compare paths
literally; the inconsistent separators prevent a match.

## Resolution and verification

Unresolved. Normalize include keys and changed-file keys to the same form at
their shared boundary, then verify dependent headers reparse their TUs while
unrelated headers remain no-ops on Windows and POSIX. This fix is outside the
native-package delivery scope. Until fixed, run a full `ca index` without
`--incremental` after header changes; do not rely on `ca update` for them.
Windows `ca watch` is independently unsupported because of Unix file locking.

## Related

- User-facing workaround: [binary package guide](../../../docs/binaries.md).
- Architecture: [overview](../../architecture/overview.md).
- Related commit: no fix committed.
