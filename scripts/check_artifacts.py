#!/usr/bin/env python3
"""Artifact integrity checks used by CI and before hand-off.

Validates artifacts that tests cannot see:

1. every bilingual document trio (``*.i18n.yaml`` + its two files): the
   recorded SHA-256 must match the file contents;
2. agent-evaluation artifacts (``benchmarks/agent_eval/``): tasks, manual
   traces and result JSON files parse, and REPORT/README exist;
3. benchmark result JSONs under ``benchmarks/results/`` (when present) parse.

Exit code 0 when everything is consistent, 1 otherwise. No third-party
dependencies: the small i18n YAML shape is parsed line by line.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SKIP_DIRS = {".git", ".venv", ".tmp", "__pycache__", ".pytest_cache", "node_modules"}
_FILE_RE = re.compile(r"^\s+file:\s*(.+?)\s*$")
_SHA_RE = re.compile(r"^\s+sha256:\s*([0-9a-f]{64})\s*$")


def _iter_files(root: Path, pattern: str):
    for path in root.rglob(pattern):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        yield path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_i18n(errors: list[str]) -> int:
    checked = 0
    for yaml_path in sorted(_iter_files(ROOT, "*.i18n.yaml")):
        files: list[str] = []
        hashes: list[str] = []
        for line in yaml_path.read_text(encoding="utf-8").splitlines():
            match = _FILE_RE.match(line)
            if match:
                files.append(match.group(1))
            match = _SHA_RE.match(line)
            if match:
                hashes.append(match.group(1))
        if len(files) < 2 or len(files) != len(hashes):
            errors.append(f"{yaml_path.relative_to(ROOT)}: malformed i18n manifest")
            continue
        for name, expected in zip(files, hashes):
            target = (yaml_path.parent / name).resolve()
            if not target.is_file():
                errors.append(f"{yaml_path.relative_to(ROOT)}: missing {name}")
                continue
            actual = _sha256(target)
            if actual != expected:
                errors.append(
                    f"{yaml_path.relative_to(ROOT)}: stale hash for {name}"
                    f" (expected {expected[:12]}…, got {actual[:12]}…)"
                )
            checked += 1
    return checked


def check_agent_eval(errors: list[str]) -> int:
    base = ROOT / "benchmarks" / "agent_eval"
    if not base.is_dir():
        return 0
    checked = 0
    for name in ("tasks.json", "manual_traces.json"):
        path = base / name
        if not path.is_file():
            errors.append(f"benchmarks/agent_eval/{name}: missing")
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            checked += 1
        except ValueError as exc:
            errors.append(f"benchmarks/agent_eval/{name}: invalid JSON: {exc}")
            continue
        if name == "tasks.json":
            tasks = payload.get("tasks") if isinstance(payload, dict) else payload
            if not isinstance(tasks, list) or not tasks:
                errors.append("benchmarks/agent_eval/tasks.json: empty task set")
            else:
                missing = [index for index, task in enumerate(tasks) if not isinstance(task, dict) or not task.get("id")]
                if missing:
                    errors.append(f"benchmarks/agent_eval/tasks.json: malformed tasks {missing[:5]}")
        if name == "manual_traces.json" and not isinstance(payload, (list, dict)):
            errors.append("benchmarks/agent_eval/manual_traces.json: expected list/object")
    for name in ("REPORT.md", "README.md"):
        if not (base / name).is_file():
            errors.append(f"benchmarks/agent_eval/{name}: missing")
    results_dir = base / "results"
    if results_dir.is_dir():
        for path in sorted(results_dir.glob("*.json")):
            try:
                json.loads(path.read_text(encoding="utf-8"))
                checked += 1
            except ValueError as exc:
                errors.append(f"{path.relative_to(ROOT)}: invalid JSON: {exc}")
    return checked


def check_results(errors: list[str]) -> int:
    results = ROOT / "benchmarks" / "results"
    if not results.is_dir():
        return 0
    checked = 0
    for path in sorted(results.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            errors.append(f"{path.relative_to(ROOT)}: invalid JSON: {exc}")
            continue
        if not isinstance(payload, dict):
            errors.append(f"{path.relative_to(ROOT)}: expected a JSON object")
            continue
        checked += 1
    return checked


def main(argv: list[str] | None = None) -> int:
    errors: list[str] = []
    i18n = check_i18n(errors)
    agent_eval = check_agent_eval(errors)
    results = check_results(errors)
    for error in errors:
        print(f"ERROR {error}", file=sys.stderr)
    print(
        f"artifact check: i18n pairs={i18n}, agent_eval files={agent_eval},"
        f" benchmark results={results}, errors={len(errors)}"
    )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
