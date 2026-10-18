#!/usr/bin/env python3
"""Accuracy baseline: score pipeline extraction against hand-written golden facts.

The fixture (``tests/fixtures/golden_os_mini.c``) is a small OS-style C file with
an exhaustive expectation file. We index it through the real pipeline with one
synthesized translation unit, read every exported record, derive facts and score
precision/recall/F1 per category.

Usage::

    .venv/bin/python benchmarks/run_accuracy.py
    .venv/bin/python benchmarks/run_accuracy.py --out benchmarks/results/accuracy.json
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterable

import _common

_common.ensure_import_path()

DEFAULT_FIXTURE = _common.REPO_ROOT / "tests" / "fixtures" / "golden_os_mini.c"
DEFAULT_EXPECTED = _common.REPO_ROOT / "tests" / "fixtures" / "golden_os_mini.expected.json"
TARGET_F1 = 0.9

CategoryMatcher = Callable[[Any, Any], bool]


# --------------------------------------------------------------------------- #
# Fact extraction from exported records
# --------------------------------------------------------------------------- #


def _norm(text: Any) -> str:
    return "".join(str(text or "").split())


def _same_file(value: Any, fixture_name: str) -> bool:
    text = str(value or "")
    return bool(text) and Path(text).name == fixture_name


def function_definitions(records: Iterable[dict[str, Any]], fixture_name: str) -> dict[str, str]:
    """Map exported function ids to names for definitions inside the fixture."""

    definitions: dict[str, str] = {}
    for record in records:
        if (
            record.get("kind") == "function"
            and record.get("is_definition")
            and _same_file(record.get("file"), fixture_name)
        ):
            definitions[str(record.get("id", ""))] = str(record.get("name", ""))
    return definitions


def extract_facts(records: Iterable[dict[str, Any]], fixture_name: str) -> dict[str, set]:
    """Turn exported store records into per-category sets of scored facts."""

    records = list(records)
    definitions = function_definitions(records, fixture_name)

    facts: dict[str, set] = {
        "functions": set(definitions.values()),
        "types": set(),
        "calls": set(),
        "branches": set(),
        "assignments": set(),
        "returns": set(),
        "resources": set(),
    }
    for record in records:
        kind = record.get("kind")
        if kind == "type":
            if _same_file(record.get("file"), fixture_name) and record.get("is_definition"):
                facts["types"].add(str(record.get("name", "")))
        elif kind == "call":
            if _same_file(record.get("file"), fixture_name):
                caller = definitions.get(str(record.get("caller_id", "")), "?")
                callee = str(
                    record.get("callee_name")
                    or record.get("callee_qualified_name")
                    or "?"
                )
                facts["calls"].add((caller, callee))
        elif kind == "branch":
            caller = definitions.get(str(record.get("function_id", "")))
            if caller is None:
                continue
            condition = _norm(record.get("condition") or record.get("case_value"))
            facts["branches"].add((caller, condition))
        elif kind == "assignment":
            caller = definitions.get(str(record.get("function_id", "")))
            if caller is None:
                continue
            facts["assignments"].add((caller, _norm(record.get("lhs"))))
        elif kind == "return":
            caller = definitions.get(str(record.get("function_id", "")))
            if caller is None or record.get("is_implicit"):
                continue
            facts["returns"].add((caller, _norm(record.get("expr"))))
        elif kind == "resource_op":
            caller = definitions.get(str(record.get("function_id", "")))
            if caller is None:
                continue
            facts["resources"].add(
                (caller, str(record.get("api_name", "")), str(record.get("op", "")))
            )
    return facts


def expected_facts(expected: dict[str, Any]) -> dict[str, Any]:
    return {
        "functions": set(expected.get("functions", [])),
        "types": set(expected.get("types", [])),
        "calls": {(item["caller"], item["callee"]) for item in expected.get("calls", [])},
        "branches": [
            (item["function"], _norm(item["condition"]))
            for item in expected.get("branches", [])
        ],
        "assignments": [
            (item["function"], _norm(item["lhs"])) for item in expected.get("assignments", [])
        ],
        "returns": [
            (item["function"], _norm(item["expr"])) for item in expected.get("returns", [])
        ],
        "resources": [
            (item["function"], item["api"], item["op"]) for item in expected.get("resources", [])
        ],
    }


def _match_exact(expected: Any, actual: Any) -> bool:
    return expected == actual


def _match_contains(function_index: int, key_index: int) -> CategoryMatcher:
    def matcher(expected: tuple, actual: tuple) -> bool:
        return (
            expected[function_index] == actual[function_index]
            and expected[key_index] != ""
            and expected[key_index] in actual[key_index]
        )

    return matcher


_MATCHERS: dict[str, CategoryMatcher] = {
    "functions": _match_exact,
    "types": _match_exact,
    "calls": _match_exact,
    "branches": _match_contains(0, 1),
    "assignments": _match_contains(0, 1),
    "returns": _match_contains(0, 1),
    "resources": _match_exact,
}


def score_category(
    expected: Iterable[Any], actual: Iterable[Any], matcher: CategoryMatcher
) -> dict[str, Any]:
    expected_list = list(expected)
    actual_list = sorted(actual, key=repr)
    matched_expected: set[int] = set()
    matched_actual: set[int] = set()
    for actual_index, actual_item in enumerate(actual_list):
        for expected_index, expected_item in enumerate(expected_list):
            if expected_index in matched_expected:
                continue
            if matcher(expected_item, actual_item):
                matched_expected.add(expected_index)
                matched_actual.add(actual_index)
                break
    true_positive = len(matched_actual)
    false_positive = len(actual_list) - true_positive
    false_negative = len(expected_list) - len(matched_expected)
    precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 1.0
    recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 1.0
    return {
        "tp": true_positive,
        "fp": false_positive,
        "fn": false_negative,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "false_positives": [
            repr(item)
            for index, item in enumerate(actual_list)
            if index not in matched_actual
        ][:10],
        "false_negatives": [
            repr(item)
            for index, item in enumerate(expected_list)
            if index not in matched_expected
        ][:10],
    }


# --------------------------------------------------------------------------- #
# Pipeline run
# --------------------------------------------------------------------------- #


def index_fixture(fixture: Path, workdir: Path) -> tuple[Path, Any, list[dict[str, Any]]]:
    from ca.build import synthesize_for_files
    from ca.pipeline import run_index
    from ca.store import SqliteStore

    target = workdir / fixture.name
    shutil.copyfile(fixture, target)
    units = synthesize_for_files([fixture.name], str(workdir))
    db_path = workdir / "accuracy.db"
    summary = run_index(str(workdir), units, str(db_path), jobs=1)
    store = SqliteStore(str(db_path))
    store.open()
    try:
        records = list(store.iter_records())
    finally:
        store.close()
    return db_path, summary, records


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


_CATEGORIES = (
    "functions",
    "types",
    "calls",
    "branches",
    "assignments",
    "returns",
    "resources",
)


def print_report(categories: dict[str, dict[str, Any]], overall: dict[str, Any]) -> None:
    header = f"{'category':<12} {'tp':>4} {'fp':>4} {'fn':>4} {'precision':>10} {'recall':>7} {'f1':>7}"
    print(header)
    print("-" * len(header))
    for name in _CATEGORIES:
        result = categories[name]
        print(
            f"{name:<12} {result['tp']:>4} {result['fp']:>4} {result['fn']:>4}"
            f" {result['precision']:>10.4f} {result['recall']:>7.4f} {result['f1']:>7.4f}"
        )
    print("-" * len(header))
    print(
        f"macro F1: {overall['macro_f1']:.4f}  target: {TARGET_F1:.2f}"
        f"  {'PASS' if overall['passed'] else 'BELOW TARGET'}"
    )
    failures = overall.get("failure_samples", {})
    for name in _CATEGORIES:
        samples = failures.get(name, {})
        for label, items in (("FP", samples.get("false_positives", [])),
                             ("FN", samples.get("false_negatives", []))):
            if items:
                print(f"  {name} {label}: {items}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", default=str(DEFAULT_FIXTURE))
    parser.add_argument("--expected", default=str(DEFAULT_EXPECTED))
    parser.add_argument("--out", default=None, help="result JSON (default: benchmarks/results/accuracy.json)")
    parser.add_argument("--fail-under", type=float, default=None,
                        help="exit 1 when macro F1 is below this value")
    args = parser.parse_args(argv)

    fixture = Path(args.fixture)
    expected_path = Path(args.expected)
    if not fixture.is_file():
        print(f"error: fixture not found: {fixture}", file=sys.stderr)
        return 1
    if not expected_path.is_file():
        print(f"error: expectations not found: {expected_path}", file=sys.stderr)
        return 1
    expected = json.loads(expected_path.read_text(encoding="utf-8"))

    with tempfile.TemporaryDirectory(prefix="ca-accuracy-") as tmp:
        workdir = Path(tmp)
        try:
            db_path, summary, records = index_fixture(fixture, workdir)
        except Exception as exc:
            print(f"error: pipeline failed: {exc}", file=sys.stderr)
            return 1
        actual = extract_facts(records, fixture.name)
        expected_categories = expected_facts(expected)
        categories: dict[str, dict[str, Any]] = {}
        for name in _CATEGORIES:
            categories[name] = score_category(
                expected_categories[name], actual[name], _MATCHERS[name]
            )

        f1_values = [categories[name]["f1"] for name in _CATEGORIES]
        macro_f1 = round(sum(f1_values) / len(f1_values), 4)
        overall = {
            "macro_f1": macro_f1,
            "target": TARGET_F1,
            "passed": all(f1 >= TARGET_F1 for f1 in f1_values),
            "failure_samples": {
                name: {
                    "false_positives": categories[name]["false_positives"],
                    "false_negatives": categories[name]["false_negatives"],
                }
                for name in _CATEGORIES
            },
        }

        # Resource-kind agreement is informational: keys already matched above.
        definitions = function_definitions(records, fixture.name)
        expected_kinds = {
            (item["function"], item["api"], item["op"]): item.get("resource_kind", "")
            for item in expected.get("resources", [])
        }
        actual_kinds = {
            (
                definitions.get(str(record.get("function_id", "")), "?"),
                record.get("api_name"),
                record.get("op"),
            ): record.get("resource_kind", "")
            for record in records
            if record.get("kind") == "resource_op"
        }
        kind_mismatches = [
            {"key": list(key), "expected": expected_kinds[key], "actual": actual_kinds[key]}
            for key in expected_kinds
            if key in actual_kinds and expected_kinds[key] != actual_kinds[key]
        ]

        result = {
            "generated_at": _common.utc_timestamp(),
            "command": _common.command_line(),
            "machine": _common.machine_info(),
            "fixture": {
                "path": _common.display_path(fixture),
                "sha256": _common.sha256_file(fixture),
                "expected_path": _common.display_path(expected_path),
                "expected_sha256": _common.sha256_file(expected_path),
            },
            "pipeline": {
                "db_path": _common.display_path(db_path),
                "summary": _common.dataclass_dict(summary),
                "translation_units": summary.tus_total,
                "records": len(records),
            },
            "categories": categories,
            "overall": overall,
            "resource_kind_mismatches": kind_mismatches,
        }
        out_path = _common.resolve_result_path(args.out, "accuracy.json")
        written = _common.write_result(out_path, result)
        print_report(categories, overall)
        print(f"\nresult: {_common.display_path(written)}")
        print(
            f"indexed {summary.tus_parsed}/{summary.tus_total} TUs,"
            f" {summary.functions} functions, {len(records)} records"
        )

    if args.fail_under is not None and macro_f1 < args.fail_under:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
