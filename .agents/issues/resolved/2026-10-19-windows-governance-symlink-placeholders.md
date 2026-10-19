# Issue: Windows governance rejects Git symlink placeholders

Status: resolved
Opened: 2026-10-19
Resolved: 2026-10-19

## Symptom and impact

Native Windows precommit validation reported 14 unindexed management files,
blocking otherwise valid commits after a checkout with `core.symlinks=false`.

## Reproduction

Run `.venv/Scripts/python.exe .agents/validate.py --root . --mode precommit`
with a staged change in such a checkout. The affected files are `CLAUDE.md`
aliases whose Git symlink target is `AGENTS.md`.

## Investigation and root cause

The validator exempted real filesystem symlinks to `AGENTS.md`. Git instead
writes the link target as an ordinary file when symlinks are disabled, so
the validator incorrectly required duplicate directory-index entries.

## Resolution and validation

Recognize placeholders only when the stage-zero Git index records mode
`120000` and exactly the `AGENTS.md` target, and the working file still
contains exactly those target bytes. Read the index once and use Git's
object hash calculation, which respects the repository's object format.
Real symlinks retain their existing behavior; ordinary files, modified
placeholders and other link targets remain subject to index validation.

Five focused regression cases use temporary Git repositories and explicitly
disabled symlink checkout, requiring no filesystem symlink permission. They
cover accepted placeholders and rejected ordinary files, modified content,
trailing newlines and different indexed targets. Native Windows precommit
validation also passes after staging this fix.

## Related

- Validator: [.agents/validate.py](../../validate.py)
- Regression tests: [tests/test_governance_validation.py](../../../tests/test_governance_validation.py)
- Decision or architecture: none; this restores the existing alias exemption.
- Related commit: this fix.
