"""JSONL helpers for debugging, fixtures, and optional exports.

The database is the system of record; JSONL is the interchange format used by
tests, fixtures, and ``ca export``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Iterator

from ca.core.schema import AnalysisResult, ParseResult, iter_record_dicts


def write_jsonl(path: str | Path, records: Iterable[dict[str, Any]]) -> int:
    """Write record dicts, one JSON object per line. Returns the count."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with target.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")
            count += 1
    return count


def read_jsonl(path: str | Path) -> Iterator[dict[str, Any]]:
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def write_result_jsonl(
    path: str | Path,
    parse_result: ParseResult,
    analyses: Iterable[AnalysisResult] = (),
) -> int:
    return write_jsonl(path, iter_record_dicts(parse_result, analyses))
