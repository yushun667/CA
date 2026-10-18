# Decision: matched libclang backend per corpus

Status: active
Date: 2026-10-18

## Context

OpenHarmony is built with clang 15 and a libc++ 15 header family, but the
shipped Python bindings default to libclang 18. Parsing real OpenHarmony TUs
with libclang 18 produced semantic mismatch diagnostics (for example
`nullptr_t`/`__promote`) and only 3/20 zero-error TUs; flags were not the cause.
Using the matching OH `libclang.so.15` raised zero-error parsing to 97/100 and
cut parse time to roughly 0.8-1.5 s/TU. Other corpora (xv6, FreeRTOS, local
tests) are validated with the bundled libclang 18 and must not regress.

## Decision

- Select the parser backend per corpus. The default remains the bundled
  libclang 18; OpenHarmony acceptance runs use the OH clang 15 backend through
  `CA_LIBCLANG_LIBRARY` (and matching bindings via `PYTHONPATH`) without changing
  the package dependency.
- Normalize argv for both backends: strip the source operand (libclang 15
  rejects it), output/dependency flags and vendor `-Xclang` passthrough; inject
  the OH resource directory, `libcxx-ohos` include path and `-stdlib=libc++`
  when the captured command lacks them, recording an audit note.
- Keep version-specific tests conditional: run only when the matched backend
  is configured, otherwise skip.
- Document the acceptance environment and command in build metadata and usage
  docs.

## Alternatives

- Keep libclang 18 only: low parse quality on the primary corpus, rejected.
- Upgrade the project to libclang 15 globally: likely breaks existing xv6 and
  FreeRTOS fixtures and pins the tool to an old API, rejected.
- Use `clang -ast-dump=json` with the OH driver: removes binding mismatch but
  adds large JSON I/O and slower extraction, rejected for the deep path.

## Consequences

- Acceptance has an explicit matched-backend environment; results are not
  portable across backend versions without re-running.
- The parser carries conditional compatibility code (source operand handling,
  bitfield fallback) and a small amount of version-specific testing.
- OpenHarmony parse quality/time numbers in reports must state which backend
  produced them.

## Validation

OH libclang 15 lean parsing: sample_20 19/20 (A scope 20/20), sample_100
97/100, 100-TU validation 100/100; libclang 18 remains green on xv6/FreeRTOS
and the local suite.
