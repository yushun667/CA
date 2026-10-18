# Issue: xv6 real compile database parsed as zero functions

Status: resolved
Opened: 2026-10-17
Resolved: 2026-10-17

## Symptom and impact

Indexing xv6 with the captured real RISC-V `compile_commands.json` reported
"46/46 TUs parsed" but produced 0 functions, 0 types and 0 calls. A direct
libclang replay raised `TranslationUnitLoadError`. Without a fix the flagship
real-corpus path was unusable, and the zero-record run was silent enough to
look successful.

## Reproduction

```sh
PYTHONPATH=src .venv/bin/python -m ca index \
    --repo .tmp/corpus/xv6-riscv \
    --compdb .tmp/corpus/xv6-riscv/compile_commands.json \
    --db /tmp/xv6.db --jobs 8
# observed: indexed 46/46, records: 0 functions, 0 calls
```

Parsing one TU directly with the compdb arguments failed, while a hand-written
RISC-V flag list parsed `kernel/bio.c` with 7 function definitions.

## Investigation

- The bundled libclang 18.1.1 does support RISC-V targets; the "missing
  backend" hypothesis was wrong.
- GCC-style compile databases append the input source as an operand
  (`... -c -o kernel/bio.o kernel/bio.c`). libclang receives the source path as
  a separate argument and fails when the same file appears again; the relative
  operand was not matched against the absolute TU path by the old filter.
- Without an explicit target, libclang used the host triple and rejected
  `-mcmodel=medany`; cross target/ABI inference was missing.
- The xPack cross GCC defaults to the rv32 ABI, so the capture wrapper injects
  `-mabi=lp64d`; the earlier compdb omitted it, making replay non-self-contained.
- `console.c`/`printk.c`/`printf` need the cross GCC builtin include directory
  for `stdarg.h`.
- `-no-pie` is a link-only flag that libclang reports as unused under a RISC-V
  target; with `-Werror` that became an error diagnostic.
- A repo/compdb root mismatch (`--repo` outside the compile database paths) made
  the extractor filter drop every entity while still reporting success.

## Root cause

Compile-argument normalisation was incomplete for GCC/kbuild-style databases:
source operand and link-only flags were not removed, cross target/ABI and
builtin include directories were not inferred, and a full extraction filter
could silently yield zero records.

## Resolution and verification

`ca.parse` now: filters source operands by realpath against `tu.file`, drops
`-o/-MD/-MF` values and link-only flags, infers `--target=riscv64/32-unknown-elf`
and `-mabi=lp64d` from the driver/`-march`, adds
`-isystem <driver -print-file-name=include>`, and does not treat
`unused-command-line-argument` as failure. `ca.cli` now exits 1 when no TU file
lies under `--repo` instead of writing an empty database.

Verified: xv6 46/46 TUs parse with zero diagnostics; 354 definitions in TU main
files; the end-to-end run yields 394 unique functions, 2,829 calls, 1,423
branches and 575 resource operations in 0.79 s. Regression tests cover relative
source operands, `-no-pie` + `-Werror`, RISC-V target inference and
unused-argument tolerance.

## Relations

- Architecture: [overview.md](../../architecture/overview.md)
- Decisions: [analysis stack](../../decisions/active/2026-10-17-analysis-stack.md),
  [compile-command policy](../../decisions/active/2026-10-17-compile-command-policy.md)
- Progress: [progress.md](../../planning/progress.md)
