#!/usr/bin/env python3
"""Sample OH call_targets candidate edges for precision review (server-side).

Produces the JSON layout agreed with the analyze owner:

* ``totals``: row count, kind distribution, reason categories, top caller dirs;
* ``samples``: indirect-candidate sample stratified by reason/evidence category
  plus one sample per other kind, with caller/callee context;
* ``direct_control``: indirect-call callers and their candidate-edge coverage;
* ``graph_integration``: ``ca graph`` JSON/DOT fragments showing candidate edges
  (plus one MCP ``graph`` check when ``--repo/--compdb`` are given);
* ``tu_health``: heuristic parse-failure coverage (TUs without functions/calls)
  and error classification parsed from the shard index log when provided.

Usage::

    python scripts/sample_candidates.py \
        --db /home/yu/CA_OH/index/oh_S1.db \
        --out /home/yu/CA_OH/parser-work/qa_call_targets_sample.json \
        --index-log /home/yu/CA_OH/index/logs/S1.log \
        --repo /home/yu/CA_OH/openharmony \
        --compdb /home/yu/CA_OH/corpus/openharmony/shards/shard_S1.json
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sqlite3
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _connect(db: str) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _reason_category(reason: str) -> str:
    text = (reason or "").lower()
    rules = (
        ("assigned_to", ("assigned", "assignment")),
        ("passed_as_argument", ("argument", "passed", "param")),
        ("global_initializer", ("global", "initializer", "initialized")),
        ("return_pointer", ("return",)),
        ("array_or_designated", ("array", "designated")),
        ("copied", ("copy", "copied")),
    )
    for category, needles in rules:
        if any(needle in text for needle in needles):
            return category
    return "other"


TEST_PATH_MARKERS = ("/test/", "/tests/", "/unittest", "unittest", "fuzztest", "_test.", "/testcase")


def _is_test_path(file_value: str) -> bool:
    text = str(file_value or "").lower()
    return any(marker in text for marker in TEST_PATH_MARKERS)


def _module_prefix(file_value: str) -> str:
    parts = [part for part in str(file_value or "").split("/") if part]
    return "/".join(parts[:2]) if parts else ""


def _sample_rows(
    connection: sqlite3.Connection,
    kinds: list[str],
    per_kind: int,
    seed: int,
) -> dict[str, list[sqlite3.Row]]:
    rng = random.Random(seed)
    selected: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for kind in kinds:
        rows = connection.execute(
            "SELECT * FROM call_targets WHERE kind=? ORDER BY call_id", (kind,)
        ).fetchall()
        selected[kind].extend(rng.sample(rows, min(per_kind, len(rows))))
    return selected


def _sample_entry(connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    caller = connection.execute(
        "SELECT * FROM functions WHERE id=?", (row["caller_id"],)
    ).fetchone()
    callee = connection.execute(
        "SELECT * FROM functions WHERE id=?", (row["callee_id"],)
    ).fetchone()
    caller_id = row["caller_id"]
    indirect_sites = connection.execute(
        "SELECT count(*) FROM calls WHERE caller_id=? AND kind='indirect'", (caller_id,)
    ).fetchone()[0]
    nondirect_sites = connection.execute(
        "SELECT count(*) FROM calls WHERE caller_id=? AND kind IN ('indirect','unknown')",
        (caller_id,),
    ).fetchone()[0]
    candidate_edges = connection.execute(
        "SELECT count(*) FROM call_targets WHERE caller_id=?", (caller_id,)
    ).fetchone()[0]
    candidate_calls = connection.execute(
        "SELECT count(DISTINCT t.call_id) FROM call_targets t"
        " JOIN calls c ON c.id=t.call_id WHERE t.caller_id=?",
        (caller_id,),
    ).fetchone()[0]
    reason = row["reason"] or ""
    return {
        "call_id": row["call_id"],
        "caller_id": caller_id,
        "caller_name": (caller["name"] if caller else ""),
        "caller_file": (caller["file"] if caller else row["file"] or ""),
        "caller_line": (caller["line"] if caller else row["line"] or 0),
        "caller_end_line": (caller["end_line"] if caller else 0),
        "caller_is_test": _is_test_path(caller["file"] if caller else row["file"] or ""),
        "tu_file": (caller["file"] if caller else ""),
        "callee_id": row["callee_id"],
        "callee_name": row["callee_name"] or (callee["name"] if callee else ""),
        "callee_file": (callee["file"] if callee else ""),
        "callee_line": (callee["line"] if callee else 0),
        "kind": row["kind"],
        "reason": reason,
        "reason_category": _reason_category(reason),
        "caller_indirect_sites": indirect_sites,
        "caller_nondirect_sites": nondirect_sites,
        "caller_candidate_edges": candidate_edges,
        "caller_candidate_call_sites": candidate_calls,
    }


def _totals(connection: sqlite3.Connection) -> dict[str, Any]:
    total = connection.execute("SELECT count(*) FROM call_targets").fetchone()[0]
    kinds = {
        row["kind"]: row["count"]
        for row in connection.execute(
            "SELECT kind, count(*) AS count FROM call_targets GROUP BY kind ORDER BY count DESC"
        )
    }
    reasons = Counter()
    for row in connection.execute("SELECT reason FROM call_targets"):
        reasons[_reason_category(row["reason"] or "")] += 1
    modules = Counter()
    for row in connection.execute(
        "SELECT f.file AS file, count(*) AS count FROM call_targets t"
        " LEFT JOIN functions f ON f.id=t.caller_id GROUP BY f.file"
    ):
        modules[_module_prefix(row["file"] or "")] += row["count"]
    test_edges = connection.execute(
        "SELECT count(*) FROM call_targets t LEFT JOIN functions f ON f.id=t.caller_id"
        " WHERE lower(coalesce(f.file,'')) LIKE '%/test%'"
        " OR lower(coalesce(f.file,'')) LIKE '%unittest%'"
        " OR lower(coalesce(f.file,'')) LIKE '%fuzztest%'"
        " OR lower(coalesce(f.file,'')) LIKE '%_test.%'"
    ).fetchone()[0]
    return {
        "path_split": {
            "test_path_edges": test_edges,
            "production_path_edges": max(0, total - test_edges),
        },
        "call_targets": total,
        "kinds": kinds,
        "reason_categories": dict(reasons.most_common()),
        "top_caller_modules": [
            {"module": module, "edges": count}
            for module, count in modules.most_common(15)
        ],
    }


def _direct_control(connection: sqlite3.Connection) -> dict[str, Any]:
    indirect_callers = connection.execute(
        "SELECT count(DISTINCT caller_id) FROM calls WHERE kind='indirect'"
    ).fetchone()[0]
    nondirect_callers = connection.execute(
        "SELECT count(DISTINCT caller_id) FROM calls"
        " WHERE kind IN ('indirect','unknown')"
    ).fetchone()[0]
    covered = connection.execute(
        "SELECT count(DISTINCT caller_id) FROM call_targets"
    ).fetchone()[0]
    matched_call_sites = connection.execute(
        "SELECT count(DISTINCT t.call_id) FROM call_targets t"
        " JOIN calls c ON c.id=t.call_id"
    ).fetchone()[0]
    nondirect_sites = connection.execute(
        "SELECT count(*) FROM calls WHERE kind IN ('indirect','unknown')"
    ).fetchone()[0]
    per_caller: list[int] = []
    for row in connection.execute(
        "SELECT caller_id, count(*) AS count FROM call_targets GROUP BY caller_id"
    ):
        per_caller.append(int(row["count"]))
    per_caller.sort()

    def percentile(values: list[int], pct: float) -> float:
        if not values:
            return 0
        index = max(0, min(len(values) - 1, int(round(pct / 100.0 * (len(values) - 1)))))
        return values[index]

    return {
        "callers_with_indirect_calls": indirect_callers,
        "callers_with_indirect_or_unknown_calls": nondirect_callers,
        "callers_with_candidate_edges": covered,
        "coverage_ratio_vs_indirect": round(covered / indirect_callers, 4) if indirect_callers else 0.0,
        "coverage_ratio_vs_nondirect": round(covered / nondirect_callers, 4) if nondirect_callers else 0.0,
        "candidate_call_ids_matched_to_calls": matched_call_sites,
        "nondirect_call_sites": nondirect_sites,
        "candidate_edges_per_caller": {
            "p50": percentile(per_caller, 50),
            "p95": percentile(per_caller, 95),
            "max": per_caller[-1] if per_caller else 0,
        },
    }


def _fp_suspects(connection: sqlite3.Connection, limit: int = 30) -> dict[str, Any]:
    """Field-suffix self-candidates are a known FP class (analyze fixes low-conf)."""

    query = (
        "SELECT t.*, f.name AS joined_caller_name FROM call_targets t"
        " JOIN functions f ON f.id=t.caller_id"
        " WHERE t.callee_name != '' AND t.callee_name = f.name"
        " AND (lower(t.reason) LIKE 'assigned to%'"
        " OR lower(t.reason) LIKE '%suffix%')"
    )
    total = connection.execute(f"SELECT count(*) FROM ({query})").fetchone()[0]
    rows = connection.execute(query + " ORDER BY t.call_id LIMIT ?", (limit,)).fetchall()
    return {
        "suspect_count": total,
        "sample": [
            {
                "call_id": row["call_id"],
                "caller_id": row["caller_id"],
                "caller_name": row["joined_caller_name"],
                "callee_name": row["callee_name"],
                "reason": row["reason"],
                "caller_is_test": _is_test_path(row["joined_caller_name"] or ""),
                "note": "same-name suffix candidate: check for FP",
            }
            for row in rows
        ],
    }


def _graph_integration(db: str, callers: list[str], repo: str | None, compdb: str | None) -> dict[str, Any]:
    integration: dict[str, Any] = {"checked": 0, "with_candidate_edges": 0, "samples": []}
    for caller in callers:
        command = [sys.executable, "-m", "ca", "graph", "--from", caller, "--depth", "1", "--db", db, "--format", "json"]
        result = subprocess.run(command, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            integration["samples"].append({"caller": caller, "ok": False, "error": result.stderr[-400:]})
            continue
        graph = json.loads(result.stdout)
        candidates = [edge for edge in graph.get("edges", []) if edge.get("candidate")]
        dot = subprocess.run(
            [sys.executable, "-m", "ca", "graph", "--from", caller, "--depth", "1", "--db", db, "--format", "dot"],
            capture_output=True,
            text=True,
            timeout=300,
        ).stdout
        integration["checked"] += 1
        if candidates:
            integration["with_candidate_edges"] += 1
        integration["samples"].append(
            {
                "caller": caller,
                "ok": True,
                "graph_edges": len(graph.get("edges", [])),
                "candidate_edges": len(candidates),
                "candidate_sample": candidates[:2],
                "dot_has_dashed_candidate": "style=dashed" in dot,
                "dot_fragment": [line for line in dot.splitlines() if "style=dashed" in line][:2],
            }
        )
        if len(integration["samples"]) >= 5:
            break
    if repo and compdb:
        caller = callers[0]
        payload = "\n".join(
            [
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "sample", "version": "0"}}}),
                json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "graph", "arguments": {"symbol": caller, "depth": 1}}}),
            ]
        ) + "\n"
        result = subprocess.run(
            [sys.executable, "-m", "ca", "mcp", "--db", db, "--repo", repo, "--compdb", compdb],
            input=payload,
            capture_output=True,
            text=True,
            timeout=600,
        )
        responses = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        envelope: dict[str, Any] = {}
        for response in responses:
            if response.get("id") == 2:
                envelope = json.loads(response["result"]["content"][0]["text"])
        data = envelope.get("data") or {}
        integration["mcp_graph"] = {
            "caller": caller,
            "ok": bool(envelope.get("ok")),
            "candidate_edges": len([edge for edge in data.get("edges", []) if edge.get("candidate")]),
            "edge_sample": [edge for edge in data.get("edges", []) if edge.get("candidate")][:1],
        }
    return integration


def _tu_health(connection: sqlite3.Connection, index_log: str | None) -> dict[str, Any]:
    empty_tus = connection.execute(
        "SELECT t.id, t.file FROM tus t"
        " WHERE NOT EXISTS (SELECT 1 FROM functions f WHERE f.tu_id=t.id)"
        " AND NOT EXISTS (SELECT 1 FROM calls c WHERE c.tu_id=t.id)"
    ).fetchall()
    errors: Counter[str] = Counter()
    error_lines: list[str] = []
    if index_log and Path(index_log).is_file():
        text = Path(index_log).read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            if "parse failed" not in line and "degraded" not in line and "error:" not in line:
                continue
            error_lines.append(line.strip()[:300])
            match = re.search(r"parse error: '([^']+)' file not found", line)
            if match:
                errors[f"missing header: {match.group(1)}"] += 1
            elif "parse error" in line:
                errors["other parse error"] += 1
            elif "degraded" in line:
                errors["degraded"] += 1
    return {
        "tus_total": connection.execute("SELECT count(*) FROM tus").fetchone()[0],
        "tus_without_any_records": len(empty_tus),
        "empty_tu_files": [row["file"] for row in empty_tus[:50]],
        "error_classification_from_log": dict(errors.most_common(20)),
        "error_lines_from_log": error_lines[:20],
        "note": (
            "parse_ok/diagnostics are not persisted in the database; empty TUs are "
            "a heuristic for failed/degraded TUs and the classification comes from "
            "the shard index log when provided"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--per-kind", type=int, default=10)
    parser.add_argument("--seed", type=int, default=18)
    parser.add_argument("--index-log", default=None)
    parser.add_argument("--repo", default=None)
    parser.add_argument("--compdb", default=None)
    args = parser.parse_args(argv)

    if not Path(args.db).is_file():
        print(f"error: database not found: {args.db}", file=sys.stderr)
        return 1
    connection = _connect(args.db)
    try:
        totals = _totals(connection)
        kinds = list(totals["kinds"])
        sampled = _sample_rows(connection, kinds, args.per_kind, args.seed)
        samples: dict[str, list[dict[str, Any]]] = {}
        for kind, rows in sampled.items():
            samples[kind] = [_sample_entry(connection, row) for row in rows]
        candidate_callers: list[str] = []
        for entry in samples.get("indirect_candidate", []):
            name = entry["caller_name"]
            if name and name not in candidate_callers:
                candidate_callers.append(name)
        report = {
            "db": args.db,
            "totals": totals,
            "samples": samples,
            "direct_control": _direct_control(connection),
            "fp_suspects": _fp_suspects(connection),
            "graph_integration": _graph_integration(
                args.db, candidate_callers, args.repo, args.compdb
            ),
            "tu_health": _tu_health(connection, args.index_log),
        }
    finally:
        connection.close()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("totals", "direct_control")}, indent=1, ensure_ascii=False)[:4000])
    print(f"sample rows: { {kind: len(rows) for kind, rows in report['samples'].items()} }")
    print(f"report: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
