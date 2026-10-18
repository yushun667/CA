#!/usr/bin/env bash
# Create the project virtualenv and install pinned dependencies.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
export PIP_CACHE_DIR="${PIP_CACHE_DIR:-$ROOT/.tmp/pip-cache}"
mkdir -p "$PIP_CACHE_DIR"

if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
  "$PYTHON" -m venv "$ROOT/.venv"
fi

"$ROOT/.venv/bin/python" -m pip install --quiet --disable-pip-version-check -r "$ROOT/requirements.txt"
# Editable install provides the `ca` console script (.venv/bin/ca).
"$ROOT/.venv/bin/python" -m pip install --quiet --disable-pip-version-check -e "$ROOT"
"$ROOT/.venv/bin/python" - <<'PY' "$ROOT"
import sys
from pathlib import Path
root = Path(sys.argv[1])
lib = root / ".venv/lib/python3.11/site-packages/clang/native/libclang.dylib"
if not lib.is_file():
    raise SystemExit(f"ERROR libclang dynamic library missing: {lib}")
from clang import cindex
print("OK environment ready:", root / ".venv")
print("OK libclang:", cindex.__file__)
console = root / ".venv" / "bin" / "ca"
if not console.is_file():
    raise SystemExit(f"ERROR console script missing: {console}")
print("OK console script:", console)
PY
