"""Human and machine readable output for the CA command line.

The CLI contract: data goes to stdout (``--json`` makes it machine readable),
progress and metrics go to stderr. Tables degrade gracefully when a store
returns additional keys, so this module stays usable while query shapes evolve.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Iterable, Sequence, TextIO

MAX_CELL_CHARS = 48


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple)):
        return ", ".join(_cell(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    text = str(value).replace("\n", " ").replace("\r", " ")
    if len(text) > MAX_CELL_CHARS:
        text = text[: MAX_CELL_CHARS - 3] + "..."
    return text


def _columns(rows: Iterable[dict[str, Any]], preferred: Sequence[str] | None) -> list[str]:
    available: set[str] = set()
    for row in rows:
        available.update(row)
    if preferred:
        chosen = [key for key in preferred if key in available]
        if chosen:
            return chosen
    order: list[str] = []
    for row in rows:
        for key in row:
            if key not in order:
                order.append(key)
    return order


def write_table(
    rows: Iterable[dict[str, Any]],
    stream: TextIO | None = None,
    *,
    columns: Sequence[str] | None = None,
) -> None:
    """Print dict rows as an aligned text table with a header and separator."""

    if stream is None:
        stream = sys.stdout
    materialized = [dict(row) for row in rows]
    if not materialized:
        return
    selected = _columns(materialized, columns)
    if not selected:
        return
    body = [[_cell(row.get(column, "")) for column in selected] for row in materialized]
    widths = [len(column) for column in selected]
    for row in body:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))
    print("  ".join(name.ljust(widths[i]) for i, name in enumerate(selected)), file=stream)
    print("  ".join("-" * width for width in widths), file=stream)
    for row in body:
        print("  ".join(value.ljust(widths[i]) for i, value in enumerate(row)), file=stream)


def write_json(data: Any, stream: TextIO | None = None) -> None:
    if stream is None:
        stream = sys.stdout
    json.dump(data, stream, indent=2, ensure_ascii=False, sort_keys=False, default=str)
    print(file=stream)


def write_key_values(
    data: dict[str, Any], stream: TextIO | None = None, *, indent: int = 0
) -> None:
    """Print a nested dict as ``key: value`` blocks; nested dicts start a section."""

    if stream is None:
        stream = sys.stdout
    pad = "  " * indent
    for key, value in data.items():
        if isinstance(value, dict) and value:
            print(f"{pad}{key}:", file=stream)
            write_key_values(value, stream, indent=indent + 1)
        else:
            print(f"{pad}{key}: {_cell(value)}", file=stream)


def format_seconds(seconds: float) -> str:
    if seconds < 1.0:
        return f"{seconds * 1000.0:.0f}ms"
    return f"{seconds:.2f}s"


def _format_bytes(size: int) -> str:
    value = float(max(0, size))
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024.0 or unit == "GiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024.0
    return f"{size} B"


def format_metrics(metrics: dict[str, Any]) -> list[str]:
    """One-line ``key=value`` rendering used by benchmark and index reports."""

    parts: list[str] = []
    for key, value in metrics.items():
        if isinstance(value, float):
            rendered = f"{value:.3f}"
        else:
            rendered = str(value)
        parts.append(f"{key}={rendered}")
    return parts


def summary_lines(summary: Any, *, db_size: int | None = None) -> list[str]:
    """Render an :class:`ca.core.schema.IndexSummary` for stderr."""

    lines = [
        f"indexed {summary.tus_parsed}/{summary.tus_total} translation units"
        f" ({summary.tus_failed} failed) into {summary.db_path}"
    ]
    lines.append(
        f"records: {summary.functions} functions, {summary.types} types,"
        f" {summary.calls} calls, {summary.files} files, {summary.lines_of_code} LOC"
    )
    lines.append(
        f"analysis: {summary.analyzed_functions} functions,"
        f" {summary.branches} branches, {summary.assignments} assignments,"
        f" {summary.returns} returns, {summary.resource_ops} resource ops,"
        f" {summary.findings} findings"
    )
    lines.append(
        "timing: parse "
        f"{format_seconds(summary.parse_seconds)} cpu, analyze "
        f"{format_seconds(summary.analyze_seconds)} cpu, write "
        f"{format_seconds(summary.write_seconds)}, total "
        f"{format_seconds(summary.total_seconds)}"
    )
    if db_size is not None:
        lines.append(f"database size: {_format_bytes(db_size)}")
    return lines


def print_summary(
    summary: Any,
    *,
    db_size: int | None = None,
    produced: dict[str, int] | None = None,
    stream: TextIO | None = None,
) -> None:
    if stream is None:
        stream = sys.stderr
    for line in summary_lines(summary, db_size=db_size):
        print(f"[index] {line}", file=stream)
    if produced:
        print(
            "[index] records produced before dedupe (raw per-TU):"
            f" functions={produced.get('functions', 0)},"
            f" types={produced.get('types', 0)},"
            f" fields={produced.get('fields', 0)},"
            f" calls={produced.get('calls', 0)}",
            file=stream,
        )
    for error in summary.errors[:10]:
        print(f"[index] error: {error}", file=stream)
    if len(summary.errors) > 10:
        print(f"[index] ... {len(summary.errors) - 10} more error(s)", file=stream)
