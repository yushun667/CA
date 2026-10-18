"""End-to-end tests for the CA pipeline and CLI.

The self-contained repository is written into a temporary directory (C sources,
headers and a hand-written ``compile_commands.json``); no network and no corpus
is required. The xv6 test only runs when the build teammate has produced a real
compile database under ``.tmp/corpus``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
XV6_REPO = REPO_ROOT / ".tmp" / "corpus" / "xv6-riscv"
XV6_COMPDB = XV6_REPO / "compile_commands.json"

_HEADER = """\
#ifndef OS_H
#define OS_H

struct proc {
    int pid;
    int state;
    struct proc *next;
};

struct spinlock {
    int locked;
};

void *kmalloc(unsigned long size);
void kfree(void *ptr);
void spin_lock(struct spinlock *lk);
void spin_unlock(struct spinlock *lk);

int proc_alloc(int pid);
void proc_free(struct proc *p);
int proc_init(void);

#endif
"""

_PROC_C = """\
#include "os.h"

struct proc proc_table[8];

int proc_alloc(int pid)
{
    struct proc *p = kmalloc(sizeof(struct proc));
    if (p == 0) {
        return -1;
    }
    p->pid = pid;
    p->state = 1;
    return 0;
}

void proc_free(struct proc *p)
{
    if (p->next) {
        p->next = 0;
    }
    kfree(p);
}

int proc_init(void)
{
    int i = 0;
    while (i < 8) {
        proc_table[i].pid = i;
        i = i + 1;
    }
    return 0;
}
"""

_MAIN_C = """\
#include "os.h"

int main(void)
{
    int pid = 1;
    if (proc_alloc(pid) != 0) {
        return 1;
    }
    proc_init();
    return 0;
}
"""


def write_fixture_repo(root: Path) -> Path:
    """Write the self-contained mini operating-system repository."""

    include_dir = root / "include"
    kernel_dir = root / "kernel"
    include_dir.mkdir(parents=True, exist_ok=True)
    kernel_dir.mkdir(parents=True, exist_ok=True)
    (include_dir / "os.h").write_text(_HEADER, encoding="utf-8")
    proc_c = kernel_dir / "proc.c"
    main_c = kernel_dir / "main.c"
    proc_c.write_text(_PROC_C, encoding="utf-8")
    main_c.write_text(_MAIN_C, encoding="utf-8")
    include = f"-I{include_dir}"
    entries = [
        {
            "directory": str(kernel_dir),
            "file": str(path),
            "arguments": [
                "clang",
                include,
                "-c",
                str(path),
                "-o",
                str(root / ".tmp" / f"{path.stem}.o"),
            ],
        }
        for path in (proc_c, main_c)
    ]
    (root / "compile_commands.json").write_text(
        json.dumps(entries, indent=2), encoding="utf-8"
    )
    return root


def produced_counts(stderr: str) -> dict[str, int]:
    """Parse the raw per-TU record counts from the index summary."""

    match = re.search(
        r"records produced before dedupe \(raw per-TU\):"
        r" functions=(\d+), types=(\d+), fields=(\d+), calls=(\d+)",
        stderr,
    )
    if not match:
        return {}
    keys = ("functions", "types", "fields", "calls")
    return {key: int(value) for key, value in zip(keys, match.groups())}


def run_cli(argv: list[str]) -> int:
    """Invoke ``ca.cli.main`` in-process (stdout/stderr captured by pytest)."""

    from ca.cli.main import main

    return main(argv)


@pytest.fixture(scope="module")
def indexed_repo(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    root = tmp_path_factory.mktemp("ca-e2e")
    repo = write_fixture_repo(root / "repo")
    db_path = root / "ca.db"
    code = run_cli(
        [
            "index",
            "--repo",
            str(repo),
            "--db",
            str(db_path),
            "--source",
            "compdb",
            "--jobs",
            "1",
        ]
    )
    assert code == 0
    assert db_path.is_file()
    return {"repo": repo, "db": db_path}


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #


def test_pipeline_indexes_self_contained_repo(tmp_path: Path) -> None:
    from ca.build import load_compilation_database
    from ca.pipeline import run_index

    repo = write_fixture_repo(tmp_path / "repo")
    units = load_compilation_database(str(repo / "compile_commands.json"), str(repo))
    assert len(units) == 2
    summary = run_index(str(repo), units, str(tmp_path / "ca.db"), jobs=2)

    assert summary.tus_total == 2
    assert summary.tus_parsed == 2, summary.errors
    assert summary.functions >= 4, summary.errors
    assert summary.calls >= 2, summary.errors
    assert summary.analyzed_functions >= 3, summary.errors
    assert summary.parse_seconds > 0
    assert summary.total_seconds >= summary.parse_seconds
    assert summary.lines_of_code > 0
    assert summary.files >= 3


# --------------------------------------------------------------------------- #
# CLI: index + queries
# --------------------------------------------------------------------------- #


def test_index_cli_metrics_go_to_stderr(
    indexed_repo: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    repo, db = indexed_repo["repo"], indexed_repo["db"]
    code = run_cli(
        [
            "index",
            "--repo",
            str(repo),
            "--db",
            str(db),
            "--source",
            "compdb",
            "--jobs",
            "1",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert captured.out == ""  # data channel stays clean during indexing
    assert "[index]" in captured.err
    assert "translation units" in captured.err
    produced = produced_counts(captured.err)
    assert produced["functions"] >= 8, produced
    assert produced["calls"] >= 4, produced


def _query_json(
    capsys: pytest.CaptureFixture[str], *argv: str
) -> tuple[int, list[dict], str]:
    code = run_cli(["query", *argv, "--json"])
    captured = capsys.readouterr()
    rows = (
        json.loads(captured.out)
        if captured.out.strip() and captured.out.lstrip().startswith("[")
        else []
    )
    return code, rows, captured.err


def test_query_functions(
    indexed_repo: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    code, rows, err = _query_json(
        capsys, "functions", "proc", "--db", str(indexed_repo["db"])
    )
    assert code == 0, err
    names = {row.get("name") for row in rows}
    assert {"proc_alloc", "proc_free", "proc_init"} <= names


def test_query_callers(
    indexed_repo: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    code, rows, err = _query_json(
        capsys, "callers", "kmalloc", "--db", str(indexed_repo["db"])
    )
    assert code == 0, err
    assert rows
    assert any(row.get("caller_name") == "proc_alloc" for row in rows)


def test_query_branches_assignments_returns(
    indexed_repo: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    db = str(indexed_repo["db"])
    code, branches, err = _query_json(capsys, "branches", "proc_alloc", "--db", db)
    assert code == 0, err
    assert branches
    assert any(row.get("condition") for row in branches)

    code, assignments, err = _query_json(capsys, "assignments", "proc_alloc", "--db", db)
    assert code == 0, err
    assert assignments
    assert any("pid" in str(row.get("lhs", "")) for row in assignments)

    code, returns, err = _query_json(capsys, "returns", "proc_alloc", "--db", db)
    assert code == 0, err
    assert returns


def test_query_resources(
    indexed_repo: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    code, rows, err = _query_json(
        capsys, "resources", "proc_alloc", "--db", str(indexed_repo["db"])
    )
    assert code == 0, err
    assert rows
    assert any(
        row.get("api_name") == "kmalloc" and row.get("op") == "acquire" for row in rows
    ), rows


def test_query_detail_and_stats(
    indexed_repo: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    db = str(indexed_repo["db"])
    code = run_cli(["query", "detail", "proc_alloc", "--db", db, "--json"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    detail = json.loads(captured.out)
    assert detail["function"]["name"] == "proc_alloc"
    assert "branches" in detail

    code = run_cli(["stats", "--db", db, "--json"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    stats = json.loads(captured.out)
    assert stats["tables"]["functions"] >= 4
    assert stats["db_bytes"] > 0


def test_query_limit_and_no_results(
    indexed_repo: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    db = str(indexed_repo["db"])
    code, rows, err = _query_json(capsys, "functions", "proc", "--db", db, "--limit", "1")
    assert code == 0, err
    assert len(rows) == 1

    code, rows, err = _query_json(capsys, "callers", "no_such_symbol_anywhere", "--db", db)
    assert code == 2
    assert rows == []
    assert "no results" in err


def test_export_jsonl(
    indexed_repo: dict[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out_path = tmp_path / "export.jsonl"
    code = run_cli(
        ["export", "--db", str(indexed_repo["db"]), "--out", str(out_path)]
    )
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert captured.out == ""
    lines = [
        json.loads(line)
        for line in out_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert lines
    kinds = {line.get("kind") for line in lines}
    assert {"function", "call"} <= kinds
    assert all("schema" in line for line in lines)


def test_auto_source_uses_compile_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = write_fixture_repo(tmp_path / "repo")
    db_path = tmp_path / "auto.db"
    code = run_cli(["index", "--repo", str(repo), "--db", str(db_path), "--jobs", "1"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert "compile database" in captured.err
    assert "synthesized" not in captured.err.lower()


def test_synthesized_fallback_warns_and_indexes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Self-contained sources with no include paths: synthesized arguments cannot
    # know project-specific -I flags, so this only verifies the fallback path.
    repo = tmp_path / "synthesized-repo"
    repo.mkdir()
    (repo / "counter.c").write_text(
        "int counter_add(int value)\n"
        "{\n"
        "    int total = 0;\n"
        "    while (total < value) {\n"
        "        total = total + 1;\n"
        "    }\n"
        "    return total;\n"
        "}\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "synth.db"
    code = run_cli(
        [
            "index",
            "--repo",
            str(repo),
            "--db",
            str(db_path),
            "--source",
            "synthesized",
            "--jobs",
            "1",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert "SYNTHESIZED" in captured.err
    assert db_path.is_file()
    code, rows, err = _query_json(capsys, "functions", "counter_add", "--db", str(db_path))
    assert code == 0, err
    assert rows


def test_repo_compdb_mismatch_fails_loudly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = write_fixture_repo(tmp_path / "repo")
    other_repo = tmp_path / "other"
    other_repo.mkdir()
    code = run_cli(
        [
            "index",
            "--repo",
            str(other_repo),
            "--db",
            str(tmp_path / "mismatch.db"),
            "--compdb",
            str(repo / "compile_commands.json"),
            "--jobs",
            "1",
        ]
    )
    captured = capsys.readouterr()
    assert code == 1
    assert "none of the" in captured.err
    assert "would index nothing" in captured.err


def test_cli_error_exit_codes(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_cli(["version"]) == 0
    assert run_cli([]) == 1
    assert run_cli(["no-such-command"]) == 1
    assert run_cli(["index", "--repo", "/no/such/repo", "--db", "/tmp/x.db"]) == 1
    assert run_cli(["query", "callers", "main", "--db", "/no/such.db"]) == 1
    assert run_cli(["query", "callers", "--db", "/no/such.db"]) == 1  # missing symbol


# --------------------------------------------------------------------------- #
# Incremental indexing (cache)
# --------------------------------------------------------------------------- #


def _index_with_cache(
    tmp_path: Path, cache_dir: Path | None = None
) -> tuple[int, dict, str, object]:
    from ca.build import load_compilation_database
    from ca.pipeline import run_index

    repo = tmp_path / "repo"
    if not repo.exists():
        write_fixture_repo(repo)
    units = load_compilation_database(str(repo / "compile_commands.json"), str(repo))
    cache_stats: dict = {}
    summary = run_index(
        str(repo),
        units,
        str(tmp_path / "inc.db"),
        jobs=1,
        cache_dir=str(cache_dir) if cache_dir else None,
        cache_stats=cache_stats,
    )
    return summary, cache_stats, repo, units


def test_incremental_second_run_hits_cache(tmp_path: Path) -> None:
    cache_dir = tmp_path / ".ca-cache"
    first, first_stats, repo, _units = _index_with_cache(tmp_path, cache_dir)
    second, second_stats, _repo, _units = _index_with_cache(tmp_path, cache_dir)

    assert first_stats["hits"] == 0 and first_stats["misses"] == 2
    assert second_stats["hits"] == 2 and second_stats["misses"] == 0
    assert second_stats["hit_rate"] == 1.0
    # Identical facts regardless of cache origin.
    for field in (
        "tus_parsed",
        "functions",
        "types",
        "calls",
        "branches",
        "assignments",
        "returns",
        "resource_ops",
        "findings",
        "files",
        "lines_of_code",
    ):
        assert getattr(first, field) == getattr(second, field), field
    # Cache is on disk, beside the database when using the CLI default.
    assert list(cache_dir.rglob("*.pkl"))
    assert first.total_seconds >= 0 and second.total_seconds >= 0


def test_incremental_reparses_changed_tu(tmp_path: Path) -> None:
    cache_dir = tmp_path / ".ca-cache"
    _index_with_cache(tmp_path, cache_dir)
    proc_c = tmp_path / "repo" / "kernel" / "proc.c"
    proc_c.write_text(
        proc_c.read_text(encoding="utf-8")
        + "\nint proc_magic(void)\n{\n    return 42;\n}\n",
        encoding="utf-8",
    )
    summary, stats, _repo, _units = _index_with_cache(tmp_path, cache_dir)
    assert stats["hits"] == 1  # proc.c changed, main.c still cached
    assert stats["misses"] == 1
    from ca.store import SqliteStore

    store = SqliteStore(str(tmp_path / "inc.db"))
    store.open()
    try:
        rows = store.query_functions("proc_magic")
    finally:
        store.close()
    assert any(row["name"] == "proc_magic" for row in rows)
    assert summary.tus_parsed == 2


def test_incremental_header_change_is_not_tracked(tmp_path: Path) -> None:
    """Documented limitation: the cache key hashes only the main TU source.

    A header edit does not invalidate cached TUs, so stale results replay until
    the user rebuilds without ``--incremental`` (covered by docs/usage.md).
    """

    cache_dir = tmp_path / ".ca-cache"
    _index_with_cache(tmp_path, cache_dir)
    header = tmp_path / "repo" / "include" / "os.h"
    header.write_text(
        header.read_text(encoding="utf-8")
        + "\nint header_only_change(int value);\n",
        encoding="utf-8",
    )
    _summary, stats, _repo, _units = _index_with_cache(tmp_path, cache_dir)
    assert stats["hits"] == 2  # by design: headers are not part of the key
    assert stats["misses"] == 0
    # A full rebuild without the cache is the documented remedy.
    _summary, full_stats, _repo, _units = _index_with_cache(tmp_path, None)
    assert full_stats["enabled"] is False


def test_incremental_reparses_changed_compile_arguments(tmp_path: Path) -> None:
    cache_dir = tmp_path / ".ca-cache"
    _index_with_cache(tmp_path, cache_dir)
    compdb_path = tmp_path / "repo" / "compile_commands.json"
    entries = json.loads(compdb_path.read_text(encoding="utf-8"))
    entries[0]["arguments"].append("-DVERSION=2")
    compdb_path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    _summary, stats, _repo, units = _index_with_cache(tmp_path, cache_dir)
    assert stats["hits"] == 1  # the TU with new arguments is re-parsed
    assert stats["misses"] == 1
    assert any("-DVERSION=2" in unit.arguments for unit in units)


def test_incremental_corrupt_cache_falls_back(tmp_path: Path) -> None:
    cache_dir = tmp_path / ".ca-cache"
    _index_with_cache(tmp_path, cache_dir)
    entries = sorted(cache_dir.rglob("*.pkl"))
    assert len(entries) == 2
    entries[0].write_bytes(b"not a pickle at all")
    summary, stats, _repo, _units = _index_with_cache(tmp_path, cache_dir)
    assert stats["hits"] == 1 and stats["misses"] == 1
    assert summary.tus_parsed == 2
    assert summary.functions >= 8


def test_incremental_cli_reports_hit_rate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = write_fixture_repo(tmp_path / "repo")
    db = tmp_path / "ca.db"
    argv = [
        "index",
        "--repo",
        str(repo),
        "--db",
        str(db),
        "--source",
        "compdb",
        "--jobs",
        "1",
        "--incremental",
    ]
    assert run_cli(argv) == 0
    cold = capsys.readouterr()
    assert "cache: hits=0/2" in cold.err
    assert ".ca-cache" in cold.err

    assert run_cli(argv) == 0
    warm = capsys.readouterr()
    assert "cache: hits=2/2 (100.0%)" in warm.err
    assert "misses=0" in warm.err


# --------------------------------------------------------------------------- #
# Real corpus (optional): xv6 with a captured compile database
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(
    not XV6_COMPDB.is_file(),
    reason="xv6 compile_commands.json not produced yet (.tmp/corpus/xv6-riscv)",
)
def test_xv6_end_to_end(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Non-vacuous real-corpus test: all 46 TUs must parse and yield facts."""

    db = tmp_path / "xv6.db"
    code = run_cli(
        [
            "index",
            "--repo",
            str(XV6_REPO),
            "--db",
            str(db),
            "--compdb",
            str(XV6_COMPDB),
            "--jobs",
            "8",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0, captured.err
    # 46/46 real RISC-V TUs, zero failures, real (not synthesized) arguments.
    assert "indexed 46/46 translation units (0 failed)" in captured.err, captured.err
    assert "source=compdb" in captured.err
    assert "SYNTHESIZED" not in captured.err
    produced = produced_counts(captured.err)
    # Raw per-TU function records exceed 1000 because every kernel TU re-parses
    # the static inline helpers in riscv.h; unique DB rows are ~394.
    assert produced["functions"] > 1000, produced
    assert produced["calls"] > 2000, produced

    code = run_cli(["stats", "--db", str(db), "--json"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    stats = json.loads(captured.out)
    tables = stats["tables"]
    # Unique persisted rows: 369 definitions + riscv.h helpers collapse to <1000
    # unique ids even though 46 TUs produce thousands of duplicate records.
    assert tables["tus"] == 46
    assert tables["functions"] >= 350, tables
    assert tables["types"] >= 50, tables
    assert tables["calls"] >= 2000, tables
    assert tables["branches"] >= 1000, tables
    assert tables["assignments"] >= 1000, tables
    assert tables["resource_ops"] > 0, tables
    assert stats["db_bytes"] > 1_000_000

    code, rows, err = _query_json(capsys, "functions", "kalloc", "--db", str(db))
    assert code == 0, err
    assert any(row.get("name") == "kalloc" for row in rows)

    code, rows, err = _query_json(capsys, "callers", "kfree", "--db", str(db))
    assert code == 0, err
    assert len(rows) >= 10, rows[:3]

    code, rows, err = _query_json(capsys, "branches", "syscall", "--db", str(db))
    assert code == 0, err
    assert rows

    code, rows, err = _query_json(capsys, "resources", "pipealloc", "--db", str(db))
    assert code == 0, err
    assert any(
        row.get("api_name") == "kalloc" and row.get("op") == "acquire" for row in rows
    ), rows
    assert any(row.get("resource_kind") == "file" for row in rows), rows

    code, rows, err = _query_json(capsys, "leaks", "--db", str(db), "--limit", "10")
    assert code in (0, 2), err


# --------------------------------------------------------------------------- #
# analyze scope: main (default) / all (header dedupe) / none + transport
# --------------------------------------------------------------------------- #

_HEADER_DEF = """\
#ifndef COMMON_H
#define COMMON_H
static inline int hdr_helper(int x)
{
    if (x > 0) {
        return x;
    }
    return -x;
}
int proto_only(int value);
#endif
"""

_A_MAIN = """\
#include "common.h"
int a_main(void)
{
    return hdr_helper(1);
}
"""

_B_MAIN = """\
#include "common.h"
int b_main(void)
{
    return hdr_helper(2);
}
"""


def _write_header_definition_repo(root: Path) -> Path:
    include_dir = root / "include"
    src_dir = root / "kernel"
    include_dir.mkdir(parents=True, exist_ok=True)
    src_dir.mkdir(parents=True, exist_ok=True)
    (include_dir / "common.h").write_text(_HEADER_DEF, encoding="utf-8")
    entries = []
    for name, text in (("a.c", _A_MAIN), ("b.c", _B_MAIN)):
        path = src_dir / name
        path.write_text(text, encoding="utf-8")
        entries.append(
            {
                "directory": str(src_dir),
                "file": str(path),
                "arguments": [
                    "clang",
                    f"-I{include_dir}",
                    "-c",
                    str(path),
                    "-o",
                    str(root / ".tmp" / f"{path.stem}.o"),
                ],
            }
        )
    (root / "compile_commands.json").write_text(
        json.dumps(entries, indent=2), encoding="utf-8"
    )
    return root


def _scope_index(
    tmp_path: Path,
    *,
    analyze_scope: str,
    jobs: int = 1,
    transport_filter: str = "none",
    measure_transport: bool = False,
) -> tuple[object, dict, Path]:
    from ca.build import load_compilation_database
    from ca.pipeline import run_index

    repo = _write_header_definition_repo(tmp_path / "repo")
    units = load_compilation_database(str(repo / "compile_commands.json"), str(repo))
    metrics: dict = {}
    db = tmp_path / f"scope_{analyze_scope}.db"
    summary = run_index(
        str(repo),
        units,
        str(db),
        jobs=jobs,
        analyze_scope=analyze_scope,
        transport_filter=transport_filter,
        measure_transport=measure_transport,
        metrics=metrics,
    )
    return summary, metrics, db


def _detail(db: Path, symbol: str) -> dict:
    from ca.store import SqliteStore

    store = SqliteStore(str(db))
    store.open()
    try:
        return dict(store.query_function_detail(symbol))
    finally:
        store.close()


def test_analyze_scope_main_skips_header_definitions(tmp_path: Path) -> None:
    summary, metrics, db = _scope_index(tmp_path, analyze_scope="main")
    assert metrics["analyze_scope"] == "main"
    # Produced per TU: each TU defines hdr_helper (from the header) plus its own
    # main-file function, so the raw total is 4.
    assert metrics["definitions_total"] == 4
    assert metrics["definitions_analyzed"] == 2  # only the two main-file definitions
    assert metrics["definitions_suppressed"] == 2
    # Header symbols are still parsed/stored; only deep analysis is skipped.
    assert _detail(db, "hdr_helper")["function"]["name"] == "hdr_helper"
    assert _detail(db, "hdr_helper")["branches"] == []
    assert _detail(db, "a_main")["function"]["name"] == "a_main"
    assert summary.analyzed_functions == 2


def test_analyze_scope_all_analyzes_header_once_across_jobs(tmp_path: Path) -> None:
    _summary, metrics_single, db_single = _scope_index(
        tmp_path, analyze_scope="all", jobs=1
    )
    _summary2, metrics_parallel, db_parallel = _scope_index(
        tmp_path, analyze_scope="all", jobs=2
    )
    # Same set of definitions analyzed regardless of worker count: the header
    # helper is analyzed exactly once even though both TUs include the header.
    assert metrics_single["definitions_analyzed"] == metrics_parallel["definitions_analyzed"]
    assert metrics_parallel["definitions_analyzed"] == 3
    helper = _detail(db_parallel, "hdr_helper")
    assert helper["function"]["name"] == "hdr_helper"
    assert helper["branches"], helper  # deep analysis did run, exactly once
    assert len(helper["branches"]) == 1
    assert helper["returns"]


def test_analyze_scope_none_keeps_symbols_and_calls(tmp_path: Path) -> None:
    summary, metrics, db = _scope_index(tmp_path, analyze_scope="none")
    assert metrics["definitions_analyzed"] == 0
    assert summary.analyzed_functions == 0
    detail = _detail(db, "a_main")
    assert detail["function"]["name"] == "a_main"
    assert detail["branches"] == [] and detail["returns"] == []
    from ca.store import SqliteStore

    store = SqliteStore(str(db))
    store.open()
    try:
        assert store.query_callees("a_main", limit=10)
    finally:
        store.close()


def test_transport_measurement_and_declaration_filter(tmp_path: Path) -> None:
    _summary, plain, _db = _scope_index(
        tmp_path, analyze_scope="main", measure_transport=True
    )
    assert plain["pickle_bytes_avg"] > 0
    assert plain["transmit_bytes_avg"] == plain["pickle_bytes_avg"]
    (tmp_path / "filtered").mkdir()
    _summary2, filtered, _db2 = _scope_index(
        tmp_path / "filtered",
        analyze_scope="main",
        transport_filter="declarations",
        measure_transport=True,
    )
    assert filtered["transport_filter"] == "declarations"
    assert filtered["transmit_bytes_avg"] < filtered["pickle_bytes_avg"]


# --------------------------------------------------------------------------- #
# Robustness: chunk caps and BrokenProcessPool retry (OOM-killed workers)
# --------------------------------------------------------------------------- #


def _fake_units(count: int) -> list:
    from ca.core.schema import CompilationUnit

    return [
        CompilationUnit(
            id=f"tu:fake{i}",
            file=f"/tmp/ca-fake/f{i}.c",
            directory="/tmp/ca-fake",
            repo_root="/tmp/ca-fake",
            arguments=("clang", "-c", f"f{i}.c"),
            language="c",
            source="compile_commands",
        )
        for i in range(count)
    ]


def _fake_outcome(unit):
    from ca.core.schema import FunctionRecord, ParseResult, Sloc, TuWorkResult
    from ca.pipeline import _UnitOutcome

    function = FunctionRecord(
        id=f"fn:{unit.id}",
        tu_id=unit.id,
        name=f"fn_{unit.id.split(':')[-1]}",
        qualified_name=f"fn_{unit.id.split(':')[-1]}",
        usr="",
        kind="function",
        sloc=Sloc(file=f"f/{unit.id.split(':')[-1]}.c", line=1),
        is_definition=True,
    )
    parse = ParseResult(tu=unit, functions=[function], parse_ok=True)
    return _UnitOutcome(TuWorkResult(parse=parse, analyses=[]), definitions_total=1)


def test_chunk_size_is_capped_and_covers_all_units() -> None:
    from ca.pipeline import _MAX_CHUNK_UNITS, _chunk_units

    units = _fake_units(100)
    workers, chunks = _chunk_units(units, 4)
    assert workers == 4
    assert all(len(chunk) <= _MAX_CHUNK_UNITS for chunk in chunks)
    flattened = [unit.id for chunk in chunks for unit in chunk]
    assert len(flattened) == len(units)
    assert sorted(flattened) == sorted(unit.id for unit in units)


def test_broken_pool_retries_without_duplicates_or_loss(monkeypatch, tmp_path) -> None:
    import ca.pipeline as pipeline

    units = _fake_units(6)
    executor_instances: list[FakeExecutor] = []

    class FakeFuture:
        def __init__(self, broken: bool, chunk) -> None:
            self.broken = broken
            self.chunk = chunk

        def result(self):
            if self.broken:
                raise pipeline.BrokenProcessPool("worker killed (simulated OOM)")
            return [_fake_outcome(unit) for unit in self.chunk]

    class FakeExecutor:
        def __init__(self, max_workers: int) -> None:
            self.max_workers = max_workers
            self.broken = len(executor_instances) == 0  # first pool dies
            self.submitted: list[list] = []
            executor_instances.append(self)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def submit(self, fn, chunk, *args, **kwargs):
            self.submitted.append(list(chunk))
            return FakeFuture(self.broken, chunk)

    monkeypatch.setattr(pipeline, "ProcessPoolExecutor", FakeExecutor)
    monkeypatch.setattr(pipeline, "as_completed", lambda futures: iter(list(futures)))
    from ca.store import SqliteStore

    db_path = tmp_path / "broken-pool.db"
    metrics: dict = {}
    summary = pipeline.run_index(
        str(tmp_path / "repo"),
        units,
        str(db_path),
        jobs=3,
        analyze_scope="none",
        record_scope="all",
        metrics=metrics,
    )
    assert summary.tus_parsed == len(units), summary.errors
    assert summary.tus_failed == 0
    assert metrics["worker_pool_restarts"] >= 1
    assert len(executor_instances) >= 2  # retried with a fresh pool
    assert executor_instances[1].max_workers <= executor_instances[0].max_workers
    # Every unit was written exactly once (store dedupes by stable id anyway).
    store = SqliteStore(str(db_path))
    store.open()
    try:
        tables = store.stats()["tables"]
    finally:
        store.close()
    assert tables["tus"] == len(units)
    assert tables["functions"] == len(units)


def test_broken_pool_serial_fallback(monkeypatch, tmp_path) -> None:
    import ca.pipeline as pipeline

    units = _fake_units(3)

    class AlwaysBrokenFuture:
        def result(self):
            raise pipeline.BrokenProcessPool("worker killed (simulated OOM)")

    class AlwaysBrokenExecutor:
        def __init__(self, max_workers: int) -> None:
            self.max_workers = max_workers

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def submit(self, fn, chunk, *args, **kwargs):
            return AlwaysBrokenFuture()

    calls: list[str] = []
    real_analyze = pipeline._analyze_unit

    def fake_analyze(unit, *args, **kwargs):
        calls.append(unit.id)
        return _fake_outcome(unit)

    monkeypatch.setattr(pipeline, "ProcessPoolExecutor", AlwaysBrokenExecutor)
    monkeypatch.setattr(pipeline, "as_completed", lambda futures: iter(list(futures)))
    monkeypatch.setattr(pipeline, "_analyze_unit", fake_analyze)
    metrics: dict = {}
    db_path = tmp_path / "serial-fallback.db"
    summary = pipeline.run_index(
        str(tmp_path / "repo"),
        units,
        str(db_path),
        jobs=2,
        analyze_scope="none",
        record_scope="all",
        metrics=metrics,
    )
    assert summary.tus_parsed == len(units), summary.errors
    assert sorted(calls) == sorted(unit.id for unit in units)
    assert metrics["worker_pool_restarts"] >= 2


# --------------------------------------------------------------------------- #
# Worker memory control: max_tasks_per_child env, RSS logging, completeness
# --------------------------------------------------------------------------- #


def test_pool_recycle_setting(monkeypatch) -> None:
    import ca.pipeline as pipeline

    monkeypatch.delenv("CA_POOL_RECYCLE_CHUNKS", raising=False)
    monkeypatch.setenv("CA_MAX_TASKS_PER_CHILD", "16")
    assert pipeline._pool_recycle_chunks() == 4  # legacy value capped
    monkeypatch.setenv("CA_POOL_RECYCLE_CHUNKS", "3")
    assert pipeline._pool_recycle_chunks() == 3
    monkeypatch.setenv("CA_POOL_RECYCLE_CHUNKS", "0")
    assert pipeline._pool_recycle_chunks() == 1
    monkeypatch.delenv("CA_POOL_RECYCLE_CHUNKS")
    monkeypatch.delenv("CA_MAX_TASKS_PER_CHILD")
    assert pipeline._pool_recycle_chunks() == pipeline._DEFAULT_POOL_RECYCLE_CHUNKS


def test_new_pool_is_plain_process_pool(monkeypatch) -> None:
    import ca.pipeline as pipeline

    captured: list[dict] = []

    class CapturingExecutor:
        def __init__(self, **kwargs) -> None:
            captured.append(dict(kwargs))

    monkeypatch.setattr(pipeline, "ProcessPoolExecutor", CapturingExecutor)
    monkeypatch.setenv("CA_POOL_RECYCLE_CHUNKS", "2")
    pipeline._new_pool(3)
    assert captured[-1] == {"max_workers": 3}


def test_worker_rss_warning_logged(monkeypatch, capsys) -> None:
    import ca.pipeline as pipeline

    monkeypatch.setenv("CA_WORKER_RSS_WARN_MB", "1")
    monkeypatch.setenv("CA_WORKER_RSS_LOG", "0")
    pipeline._log_worker_rss("test-chunk", 4)
    assert "peak_rss=" in capsys.readouterr().err


def test_worker_recycling_keeps_all_units(monkeypatch, tmp_path) -> None:
    """With recycling enabled every TU must still be parsed/analyzed exactly once."""

    from ca.build import load_compilation_database
    from ca.pipeline import run_index as run_pipeline
    from ca.store import SqliteStore

    repo = write_fixture_repo(tmp_path / "repo")
    units = list(
        load_compilation_database(str(repo / "compile_commands.json"), str(repo))
    )
    db_path = tmp_path / "recycled.db"
    monkeypatch.setenv("CA_POOL_RECYCLE_CHUNKS", "1")
    metrics: dict = {}
    summary = run_pipeline(
        str(repo),
        units,
        str(db_path),
        jobs=2,
        analyze_scope="main",
        record_scope="all",
        metrics=metrics,
    )
    assert summary.tus_parsed == len(units)
    assert summary.tus_failed == 0
    assert metrics.get("worker_pool_restarts", 0) == 0
    store = SqliteStore(str(db_path))
    store.open()
    try:
        tables = store.stats()["tables"]
        detail = store.query_function_detail("proc_alloc")
    finally:
        store.close()
    assert tables["tus"] == len(units)
    assert detail["function"]["name"] == "proc_alloc"
    assert detail["function"]["id"].startswith("fn:")
