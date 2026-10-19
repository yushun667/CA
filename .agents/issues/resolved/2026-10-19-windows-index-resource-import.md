# Issue: Unix memory metric blocks native Windows indexing

Status: resolved
Opened: 2026-10-19

## Symptoms and impact

Native Windows Python cannot import `ca.pipeline`, preventing `ca index` from
running even though its parsing and storage dependencies are installed.

## Reproduction or trigger

On Windows, import `ca.pipeline` before this fix. The module-level `import
resource` raises `ModuleNotFoundError`; Python provides `resource` on Unix only.

## Investigation

`resource` is used only by `_peak_rss_mb`, a diagnostic helper whose existing
failure fallback is `0.0`. The other Unix-only import, `fcntl` in `ca.cli.watch`,
is loaded lazily by the separate watch command and does not block ordinary indexing.

## Root cause

An optional worker-memory metric was imported eagerly as a mandatory dependency.

## Resolution and validation

Import `resource` inside the helper's existing exception boundary. Platforms
without it retain the zero fallback; Unix unit conversion is unchanged. Make the
warning test supply a deterministic metric and add a fresh-process regression
that imports the pipeline with `resource` unavailable.

Native `.venv/Scripts/python.exe -m pytest -q tests/test_e2e_pipeline.py -k
"pipeline_import_without_resource or worker_rss_warning_logged or pool_recycle_setting
or new_pool_is_plain_process_pool or worker_recycling_keeps_all_units"` passed:
5 tests, including a two-worker indexing run. `git diff --check` also passed.
Native Windows worker RSS measurement remains unavailable (reported as zero).
This change does not implement Windows support for the separate watch command.

## Links

- Decision or architecture: none; this preserves the existing optional-metric fallback.
- Related commit: see Git history for this issue and `src/ca/pipeline.py`.
