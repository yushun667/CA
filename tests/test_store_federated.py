"""Read-only federated queries across multiple shard databases.

Covers: discovery, attach/skip (missing/corrupt/non-CA), zero valid shards,
union + dedupe + ``shard`` provenance, exact-match priority, callers/callees,
facts, detail merging, call_targets, callbacks, cross-shard graph (depth/
dedupe/cycles/truncation), old-schema shards, stats aggregation and the
read-only guarantee.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from ca.core.ids import branch_id, call_id, call_target_id, function_id, tu_id
from ca.core.schema import (
    AnalysisResult,
    BranchRecord,
    CallRecord,
    CallTargetRecord,
    CompilationUnit,
    FunctionRecord,
    ParseResult,
    Sloc,
)
from ca.store import SqliteStore
from ca.store.federated import FederatedStore, discover_databases

REPO = "/repo"
FILE = "kernel/fed.c"


def _sloc(line: int, file: str = FILE) -> Sloc:
    return Sloc(file, line, 3, line, 24)


def _function(name: str, line: int, file: str = FILE) -> FunctionRecord:
    usr = f"c:@F@{name}"
    return FunctionRecord(
        id=function_id(name=name, usr=usr, sloc=_sloc(line, file), linkage="external"),
        tu_id="tu:placeholder",
        name=name,
        qualified_name=name,
        usr=usr,
        kind="function",
        sloc=_sloc(line, file),
        is_definition=True,
        linkage="external",
    )


def _call(caller: FunctionRecord, callee: FunctionRecord, line: int) -> CallRecord:
    sloc = _sloc(line)
    return CallRecord(
        id=call_id(caller_id=caller.id, callee_usr=callee.usr, callee_name=callee.name, sloc=sloc),
        tu_id="tu:placeholder",
        caller_id=caller.id,
        sloc=sloc,
        callee_id=callee.id,
        callee_usr=callee.usr,
        callee_name=callee.name,
        callee_qualified_name=callee.name,
        kind="direct",
        is_resolved=True,
    )


def _analysis(
    function: FunctionRecord,
    *,
    branches: list[BranchRecord] | None = None,
    call_targets: list[CallTargetRecord] | None = None,
) -> AnalysisResult:
    return AnalysisResult(
        function_id=function.id,
        branches=branches or [],
        call_targets=call_targets or [],
    )


def _write_shard(
    path: Path,
    functions: list[FunctionRecord],
    *,
    calls: list[CallRecord] = (),
    analyses: dict[str, AnalysisResult] | None = None,
    callbacks: list[dict] = (),
) -> Path:
    shard_file = f"kernel/shard_{path.stem}.c"
    unit = CompilationUnit(
        id=tu_id(shard_file, REPO, ["-c", shard_file]),
        file=shard_file,
        directory=REPO,
        repo_root=REPO,
        arguments=("-c", shard_file),
    )
    for function in functions:
        function.tu_id = unit.id
    for call in calls:
        call.tu_id = unit.id
    store = SqliteStore(str(path))
    store.open()
    store.begin()
    store.add_parse_result(ParseResult(tu=unit, functions=functions, calls=list(calls)))
    for analysis in (analyses or {}).values():
        store.add_analysis_result(analysis)
    if callbacks:
        store.add_callback_registrations(list(callbacks))
    store.commit()
    store.finalize()
    store.close()
    return path


def _branch(function: FunctionRecord, line: int, ordinal: int, condition: str) -> BranchRecord:
    return BranchRecord(
        id=branch_id(function.id, _sloc(line), ordinal),
        function_id=function.id,
        block_id="bb:x",
        sloc=_sloc(line),
        kind="if",
        condition=condition,
    )


def _hash(paths) -> dict[str, str]:
    return {str(path): hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in paths}


# --------------------------------------------------------------------------- #
# Discovery / attach
# --------------------------------------------------------------------------- #
def test_discover_databases_filters_sidecars(tmp_path) -> None:
    (tmp_path / "b.db").write_bytes(b"")
    (tmp_path / "a.db").write_bytes(b"")
    (tmp_path / "a.db-wal").write_bytes(b"")
    (tmp_path / "a.db-shm").write_bytes(b"")
    (tmp_path / ".hidden.db").write_bytes(b"")
    (tmp_path / "notes.txt").write_text("x")
    names = [path.name for path in discover_databases(tmp_path)]
    assert names == ["a.db", "b.db"]
    with pytest.raises(FileNotFoundError):
        discover_databases(tmp_path / "missing")


def test_missing_corrupt_and_non_ca_shards_are_skipped(tmp_path) -> None:
    alpha = _function("alpha", 10)
    valid = _write_shard(tmp_path / "oh_S1.db", [alpha])
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"this is not a sqlite database at all")
    non_ca = tmp_path / "other.db"
    import sqlite3

    connection = sqlite3.connect(non_ca)
    connection.execute("CREATE TABLE unrelated (x INTEGER)")
    connection.commit()
    connection.close()
    missing = tmp_path / "missing.db"

    with FederatedStore([valid, corrupt, non_ca, missing]) as fed:
        assert [shard["shard"] for shard in fed.shards] == ["oh_S1"]
        skipped_paths = {Path(item["path"]).name for item in fed.skipped}
        assert skipped_paths == {"corrupt.db", "other.db", "missing.db"}
        assert fed.query_functions("alpha")[0]["shard"] == "oh_S1"


def test_single_file_copy_without_sidecars_opens(tmp_path) -> None:
    """A copied shard often has only the .db file (no -wal/-shm sidecars).

    Read-only WAL databases without a wal-index normally fail to open with
    ``mode=ro``; the federated store falls back to ``immutable=1`` (and does the
    same for the ATTACH control connection) so such copies still work.
    """

    alpha = _function("alpha", 10)
    original = _write_shard(tmp_path / "original.db", [alpha])
    copied_dir = tmp_path / "copied"
    copied_dir.mkdir()
    copied = copied_dir / "copied.db"
    shutil.copyfile(original, copied)
    assert not (copied_dir / "copied.db-wal").exists()
    with FederatedStore([copied]) as fed:
        assert fed.skipped == []
        assert [shard["shard"] for shard in fed.shards] == ["copied"]
        rows = fed.query_functions("alpha")
        assert len(rows) == 1 and rows[0]["shard"] == "copied"


def test_zero_valid_shards_is_allowed(tmp_path) -> None:
    with FederatedStore([tmp_path / "nope.db"]) as fed:
        assert fed.shards == []
        assert fed.skipped and fed.skipped[0]["error"]
        assert fed.query_functions("x") == []
        assert fed.query_callers("x") == []
        assert fed.query_call_graph("x") == {
            "root": None,
            "deep_ready": False,
            "direction": "both",
            "depth": 1,
            "nodes": [],
            "edges": [],
            "cycles": [],
            "truncated": False,
        }
        stats = fed.stats()
        assert stats["shard_count"] == 0
        assert stats["db_bytes"] == 0
        assert stats["tables"]["functions"] == 0
        assert stats["skipped"]


def test_attach_more_shards_and_names(tmp_path) -> None:
    alpha = _function("alpha", 10)
    beta = _function("beta", 20)
    first = _write_shard(tmp_path / "s1.db", [alpha])
    second = _write_shard(tmp_path / "s2.db", [beta])
    fed = FederatedStore([first], shard_names=["one"])
    fed.open()
    assert [shard["shard"] for shard in fed.shards] == ["one"]
    added = fed.attach_databases([second, first])
    assert [item["shard"] for item in added] == ["s2"]
    assert {shard["shard"] for shard in fed.shards} == {"one", "s2"}
    assert {row["shard"] for row in fed.query_functions("a")} <= {"one", "s2"}
    fed.close()
    fed.close()  # idempotent
    assert fed.closed


# --------------------------------------------------------------------------- #
# Functions: union, dedupe, exact priority, pagination
# --------------------------------------------------------------------------- #
def test_functions_union_dedupe_and_exact_priority(tmp_path) -> None:
    header = _function("header_inline", 5, "kernel/shared.h")
    alpha = _function("alpha", 10)
    gamma = _function("gamma", 30)
    beta_helper = _function("beta_helper", 40)
    first = _write_shard(tmp_path / "s1.db", [alpha, header])
    second = _write_shard(tmp_path / "s2.db", [gamma, header, beta_helper])

    with FederatedStore([first, second]) as fed:
        # Same header function id lives in both shards: one row, first shard wins.
        header_rows = fed.query_functions("header")
        assert len(header_rows) == 1
        assert header_rows[0]["id"] == header.id
        assert header_rows[0]["shard"] == "s1"
        # Exact match globally wins over another shard's substring match.
        names = [row["name"] for row in fed.query_functions("alpha")]
        assert names == ["alpha"]
        assert [row["shard"] for row in fed.query_functions("alpha")] == ["s1"]
        assert fed.query_functions("missing_symbol") == []
        assert fed.query_functions("alpha", limit=0) == []


def test_functions_limit_offset_after_merge(tmp_path) -> None:
    functions = [_function(f"fn_{index:02d}", 100 + index) for index in range(10)]
    first = _write_shard(tmp_path / "s1.db", functions[:6])
    second = _write_shard(tmp_path / "s2.db", functions[4:])
    all_rows = [row["id"] for row in FederatedStore([first, second]).query_functions("fn_", limit=100)]
    # Sanity: build the expected federated order from the same merge rules.
    assert len(all_rows) == 10
    with FederatedStore([first, second]) as fed:
        page = fed.query_functions("fn_", limit=3, offset=2)
        assert [row["id"] for row in page] == all_rows[2:5]
        assert [row["shard"] for row in page]
        assert fed.query_functions("fn_", limit=3, offset=9) == [
            row for row in fed.query_functions("fn_", limit=3, offset=9)
        ]
        tail = fed.query_functions("fn_", limit=5, offset=8)
        assert len(tail) == 2
        assert [row["id"] for row in tail] == all_rows[8:]


# --------------------------------------------------------------------------- #
# Callers / callees / facts / detail
# --------------------------------------------------------------------------- #
def _call_graph_shards(tmp_path):
    alpha = _function("alpha", 10)
    beta = _function("beta", 20)
    gamma = _function("gamma", 30)
    alpha_beta = _call(alpha, beta, 12)
    beta_gamma = _call(beta, gamma, 22)
    first = _write_shard(
        tmp_path / "s1.db",
        [alpha, beta],
        calls=[alpha_beta],
        analyses={alpha.id: _analysis(alpha, branches=[_branch(alpha, 11, 0, "a > 0")])},
    )
    second = _write_shard(
        tmp_path / "s2.db",
        [beta, gamma],
        calls=[beta_gamma],
        analyses={beta.id: _analysis(beta, branches=[_branch(beta, 21, 0, "b > 0")])},
    )
    return first, second, alpha, beta, gamma


def test_callers_callees_cross_shard(tmp_path) -> None:
    first, second, alpha, beta, gamma = _call_graph_shards(tmp_path)
    with FederatedStore([first, second]) as fed:
        callers = fed.query_callers("beta")
        assert [(row["caller_name"], row["shard"]) for row in callers] == [("alpha", "s1")]
        callees = fed.query_callees("beta")
        assert [(row["callee_name"], row["shard"]) for row in callees] == [("gamma", "s2")]
        assert [row["shard"] for row in fed.query_callees("alpha")] == ["s1"]
        assert fed.query_callers("missing_symbol") == []


def test_fact_merge_and_dedupe(tmp_path) -> None:
    alpha = _function("alpha", 10)
    branch_a = _branch(alpha, 11, 0, "x")
    branch_b = _branch(alpha, 12, 0, "y")
    first = _write_shard(
        tmp_path / "s1.db",
        [alpha],
        analyses={alpha.id: _analysis(alpha, branches=[branch_a])},
    )
    second = _write_shard(
        tmp_path / "s2.db",
        [alpha],
        analyses={alpha.id: _analysis(alpha, branches=[branch_b, branch_a])},
    )
    with FederatedStore([first, second]) as fed:
        rows = fed.query_branches("alpha")
        assert [(row["condition"], row["shard"]) for row in rows] == [
            ("x", "s1"),
            ("y", "s2"),
        ]
        # Duplicate fact id across shards is collapsed to the first shard.
        assert len({row["id"] for row in rows}) == 2
        page = fed.query_branches("alpha", limit=1, offset=1)
        assert [(row["condition"], row["shard"]) for row in page] == [("y", "s2")]
        assert fed.query_assignments("alpha") == []
        assert fed.query_returns("alpha") == []
        assert fed.query_resource_ops("alpha") == []


def test_detail_merges_deep_ready_and_facts(tmp_path) -> None:
    beta = _function("beta", 20)
    first = _write_shard(
        tmp_path / "s1.db",
        [beta],
        analyses={beta.id: _analysis(beta, branches=[_branch(beta, 11, 0, "left")])},
    )
    second = _write_shard(
        tmp_path / "s2.db",
        [beta],
        analyses={beta.id: _analysis(beta, branches=[_branch(beta, 21, 0, "right")])},
    )
    # Mark deep-ready only in the second shard.
    writer = SqliteStore(str(second))
    writer.open()
    writer.mark_deep_ready([beta.id])
    writer.commit()
    writer.close()

    with FederatedStore([first, second]) as fed:
        detail = fed.query_function_detail("beta")
        assert detail["function"]["id"] == beta.id
        assert detail["function"]["shard"] == "s1"
        assert detail["deep_ready"] is True  # ready in any shard
        assert [(row["condition"], row["shard"]) for row in detail["branches"]] == [
            ("left", "s1"),
            ("right", "s2"),
        ]
        missing = fed.query_function_detail("missing")
        assert missing["function"] is None
        assert missing["shard"] is None
        assert missing["deep_ready"] is False
        assert missing["branches"] == []


def test_call_targets_and_findings(tmp_path) -> None:
    caller = _function("caller", 10)
    callee = _function("callee", 20)
    call = _call(caller, callee, 12)
    target = CallTargetRecord(
        id=call_target_id(call.id, callee.id, "indirect_candidate"),
        call_id=call.id,
        caller_id=caller.id,
        callee_id=callee.id,
        callee_name=callee.name,
        kind="indirect_candidate",
        confidence=0.8,
        reason="fed",
        sloc=_sloc(12),
    )
    first = _write_shard(
        tmp_path / "s1.db",
        [caller, callee],
        calls=[call],
        analyses={caller.id: _analysis(caller, call_targets=[target])},
    )
    with FederatedStore([first]) as fed:
        rows = fed.query_call_targets("caller", direction="callee")
        assert [(row["callee_name"], row["shard"]) for row in rows] == [("callee", "s1")]
        assert fed.query_call_targets("callee", direction="caller")[0]["call_id"] == call.id
        assert fed.query_findings() == []


def test_callbacks_federated(tmp_path) -> None:
    alpha = _function("alpha", 10)
    beta = _function("beta", 20)
    fact_a = {
        "id": "cbreg:one",
        "object_path": "g_ops.read",
        "target_function_id": alpha.id,
        "target_function_name": alpha.name,
        "file": FILE,
        "line": 12,
        "tu_id": "tu:one",
        "registration_kind": "struct_field",
        "confidence": 0.9,
        "evidence": ["g_ops.read = alpha"],
        "module": "kernel",
    }
    fact_b = {
        "id": "cbreg:two",
        "object_path": "g_ops.write",
        "target_function_id": beta.id,
        "target_function_name": beta.name,
        "file": FILE,
        "line": 22,
        "tu_id": "tu:two",
        "registration_kind": "registration_call",
        "confidence": 0.7,
        "evidence": ["register(&g_ops.write, beta)"],
        "module": "kernel",
    }
    first = _write_shard(tmp_path / "s1.db", [alpha], callbacks=[fact_a])
    second = _write_shard(tmp_path / "s2.db", [beta], callbacks=[fact_b, fact_a])
    with FederatedStore([first, second]) as fed:
        rows = fed.query_callbacks(path_prefix="g_ops.")
        assert [(row["object_path"], row["shard"]) for row in rows] == [
            ("g_ops.read", "s1"),
            ("g_ops.write", "s2"),
        ]
        # Duplicate callback id across shards is collapsed.
        assert len({row["id"] for row in rows}) == 2
        stats = fed.callback_stats()
        assert stats["registrations"] == 3  # shard-summed (header duplicate counted twice)
        assert stats["by_kind"] == {"registration_call": 1, "struct_field": 2}
        assert stats["shard_count"] == 2
        assert [item["shard"] for item in stats["shards"]] == ["s1", "s2"]


# --------------------------------------------------------------------------- #
# Cross-shard graph
# --------------------------------------------------------------------------- #
def test_graph_cross_shard_merge_and_depth(tmp_path) -> None:
    first, second, alpha, beta, gamma = _call_graph_shards(tmp_path)
    delta = _function("delta", 40)
    # Add header-inline duplicate edge alpha->beta in the second shard and a
    # second-level edge from gamma to delta there, so the graph must merge.
    alpha_copy = _function("alpha", 10)
    alpha_beta = _call(alpha_copy, beta, 12)
    gamma_delta = _call(gamma, delta, 32)
    third = _write_shard(
        tmp_path / "s3.db",
        [gamma, delta, alpha_copy],
        calls=[alpha_beta, gamma_delta],
    )
    with FederatedStore([first, second, third]) as fed:
        graph = fed.query_call_graph("alpha", direction="forward", depth=2, limit=100)
        assert graph["root"]["name"] == "alpha"
        assert graph["root"]["shard"] == "s1"
        assert set(graph) == {
            "root",
            "deep_ready",
            "direction",
            "depth",
            "nodes",
            "edges",
            "cycles",
            "truncated",
        }
        names = {(node["name"], node["depth"]) for node in graph["nodes"]}
        assert ("alpha", 0) in names and ("beta", 1) in names and ("gamma", 2) in names
        edge_keys = [
            (edge["caller_id"], edge["callee_id"], edge["depth"], edge["shard"])
            for edge in graph["edges"]
        ]
        # alpha->beta duplicate exists in s1 and s3; only the first shard wins.
        assert len(edge_keys) == len(set(edge_keys))
        alpha_beta = [edge for edge in graph["edges"] if edge["callee_name"] == "beta"]
        assert len(alpha_beta) == 1 and alpha_beta[0]["shard"] == "s1"
        # depth 2 continues into the third shard.
        assert any(edge["callee_name"] == "gamma" for edge in graph["edges"])
    with FederatedStore([first, second, third]) as fed:
        reverse = fed.query_call_graph("gamma", direction="reverse", depth=2, limit=100)
        reverse_names = {(node["name"], node["depth"]) for node in reverse["nodes"]}
        assert ("gamma", 0) in reverse_names and ("beta", 1) in reverse_names
        assert ("alpha", 2) in reverse_names  # alpha->beta lives in the first shard


def test_graph_cycles_and_truncation(tmp_path) -> None:
    alpha = _function("alpha", 10)
    beta = _function("beta", 20)
    alpha_beta = _call(alpha, beta, 12)
    beta_alpha = _call(beta, alpha, 22)
    first = _write_shard(tmp_path / "s1.db", [alpha, beta], calls=[alpha_beta])
    second = _write_shard(tmp_path / "s2.db", [beta, alpha], calls=[beta_alpha])
    with FederatedStore([first, second]) as fed:
        graph = fed.query_call_graph("alpha", direction="forward", depth=3, limit=100)
        assert graph["cycles"], "cross-shard cycle should be detected"
        cycle = graph["cycles"][0]
        assert set(cycle) == {alpha.id, beta.id}
        limited = fed.query_call_graph("alpha", direction="forward", depth=2, limit=1)
        assert limited["truncated"] is True
        assert len(limited["edges"]) == 1
        assert fed.query_call_graph("missing")["root"] is None
        with pytest.raises(ValueError):
            fed.query_call_graph("alpha", direction="sideways")


# --------------------------------------------------------------------------- #
# Old schema compatibility & stats
# --------------------------------------------------------------------------- #
def test_old_schema_shard_is_supported(tmp_path) -> None:
    alpha = _function("alpha", 10)
    beta = _function("beta", 20)
    call = _call(alpha, beta, 12)
    first = _write_shard(tmp_path / "s1.db", [alpha, beta], calls=[call])
    # Simulate a phase-2 shard: no call_targets/deep_status/callback tables.
    import sqlite3

    connection = sqlite3.connect(first)
    connection.executescript(
        "DROP TABLE IF EXISTS call_targets;"
        "DROP TABLE IF EXISTS deep_status;"
        "DROP TABLE IF EXISTS callback_registrations;"
        "DROP TABLE IF EXISTS callback_target_links;"
    )
    connection.commit()
    connection.close()
    with FederatedStore([first]) as fed:
        callers = fed.query_callers("beta")
        assert [(row["caller_name"], row["source"]) for row in callers] == [("alpha", "call")]
        assert fed.query_call_targets("alpha", direction="callee") == []
        assert fed.query_callbacks() == []
        assert fed.callback_stats()["registrations"] == 0
        detail = fed.query_function_detail("beta")
        assert detail["deep_ready"] is False


def test_stats_aggregation(tmp_path) -> None:
    alpha = _function("alpha", 10)
    beta = _function("beta", 20)
    first = _write_shard(tmp_path / "s1.db", [alpha], calls=[])
    second = _write_shard(tmp_path / "s2.db", [alpha, beta], calls=[])
    with FederatedStore([first, second]) as fed:
        stats = fed.stats()
        assert set(stats) == {
            "tables",
            "db_bytes",
            "schema_version",
            "sqlite_version",
            "shard_count",
            "shards",
            "skipped",
        }
        assert stats["shard_count"] == 2
        assert stats["tables"]["functions"] == 3  # shard-summed (alpha appears twice)
        assert stats["tables"]["calls"] == 0
        assert stats["db_bytes"] == sum(item["db_bytes"] for item in fed.shards)
        assert [item["shard"] for item in stats["shards"]] == ["s1", "s2"]
        for entry in stats["shards"]:
            assert entry["path"]
            assert entry["schema_version"] == "1"
            assert entry["store_schema_version"] == "5"
            assert entry["tables"]["functions"] >= 1


# --------------------------------------------------------------------------- #
# Read-only guarantee
# --------------------------------------------------------------------------- #
def test_federated_store_is_read_only(tmp_path) -> None:
    alpha = _function("alpha", 10)
    first = _write_shard(tmp_path / "s1.db", [alpha])
    before = _hash([first])
    with FederatedStore([first]) as fed:
        fed.query_functions("alpha")
        fed.query_function_detail("alpha")
        fed.query_call_graph("alpha")
        fed.stats()
        for write_api in (
            "add_parse_result",
            "add_analysis_result",
            "mark_deep_ready",
            "mark_deep_failed",
            "add_callback_registrations",
            "add_call_targets",
            "commit",
            "finalize",
        ):
            assert not hasattr(fed, write_api), write_api
    assert _hash([first]) == before
    with pytest.raises(ValueError):
        FederatedStore([first], read_only=False)
