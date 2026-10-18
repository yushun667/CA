#!/usr/bin/env bash
# Verify a CA deployment on a source server: venv, editable install, tests, MCP smoke.
#
# Usage (on the server):
#   bash scripts/server_bootstrap.sh [/path/to/index.db]
# Without a database argument the script creates a temporary SQLite index from a
# tiny generated C file, so it works on a fresh server without corpora.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${VENV:-/home/yu/CA_OH/venv}"
PYTHON="$VENV/bin/python"
CA="$VENV/bin/ca"
DB="${1:-}"

echo "== CA server bootstrap =="
echo "repo: $ROOT"
echo "venv: $VENV"

test -x "$PYTHON" || { echo "ERROR: venv python missing: $PYTHON" >&2; exit 1; }
"$PYTHON" -V
"$CA" version

echo "== editable install check =="
"$PYTHON" -m pip install --quiet --disable-pip-version-check -e "$ROOT"
"$CA" --help >/dev/null

echo "== full test suite =="
PYTEST_ARGS=(-q)
# Corpus compile databases captured on another machine keep absolute source
# paths; the xv6 e2e test cannot run against a relocated corpus. Detect that
# and skip only that test (it still runs on the machine that captured it).
if [[ -f "$ROOT/.tmp/corpus/xv6-riscv/compile_commands.json" ]]; then
  if ! "$PYTHON" - "$ROOT" <<'PY'
import json, sys
from pathlib import Path
root = Path(sys.argv[1])
entries = json.loads((root / ".tmp/corpus/xv6-riscv/compile_commands.json").read_text())
raise SystemExit(0 if all(Path(e["file"]).is_file() for e in entries) else 1)
PY
  then
    echo "note: xv6 corpus compile database points at another machine; skipping test_xv6_end_to_end"
    PYTEST_ARGS+=(-k "not test_xv6_end_to_end")
  fi
fi
(cd "$ROOT" && "$PYTHON" -m pytest "${PYTEST_ARGS[@]}")

if [[ -z "$DB" ]]; then
  echo "== creating a temporary smoke database =="
  TMPDIR_CA="$(mktemp -d)"
  trap 'rm -rf "$TMPDIR_CA"' EXIT
  cat > "$TMPDIR_CA/smoke.c" <<'C'
int smoke_add(int a, int b) { return a + b; }
int smoke_main(void) { return smoke_add(1, 2); }
C
  (cd "$ROOT" && "$CA" index --repo "$TMPDIR_CA" --db "$TMPDIR_CA/smoke.db" --source synthesized --jobs 2 >/dev/null)
  DB="$TMPDIR_CA/smoke.db"
fi

echo "== MCP stdio smoke: $DB =="
SMOKE_OUT="$(mktemp)"
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"bootstrap","version":"0"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
  '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"stats","arguments":{}}}' \
  | "$CA" mcp --db "$DB" > "$SMOKE_OUT"
"$PYTHON" - "$SMOKE_OUT" <<'PY'
import json, sys
lines = [json.loads(line) for line in open(sys.argv[1]) if line.strip()]
by_id = {item.get("id"): item for item in lines}
assert by_id[1]["result"]["serverInfo"]["name"] == "ca-analyzer", "initialize failed"
tools = [tool["name"] for tool in by_id[2]["result"]["tools"]]
assert "graph" in tools and "callers" in tools, f"tools/list incomplete: {tools}"
envelope = json.loads(by_id[3]["result"]["content"][0]["text"])
assert envelope["ok"] is True, f"stats tool failed: {envelope}"
print(f"MCP_OK tools={len(tools)} tables={sorted(envelope['data']['tables'])[:4]}")
PY
rm -f "$SMOKE_OUT"
echo "== SERVER_OK =="
