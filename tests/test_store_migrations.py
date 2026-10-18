"""Schema v8: candidate-confidence removal and legacy database migration.

Covers the fresh schema (no ``confidence`` columns anywhere in the query API),
the three in-place migration paths (DROP COLUMN, safe table rebuild, and the
graceful "kept legacy column" fallback), read-only federated shards that still
carry the legacy column, natural-key dedupe with audit ``reason`` merging, and
legacy producers that still send a ``confidence`` field.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from ca.core.ids import call_id, call_target_id, function_id, tu_id
from ca.core.schema import (
    AnalysisResult,
    CallRecord,
    CallTargetRecord,
    CompilationUnit,
    FunctionRecord,
    ParseResult,
    Sloc,
)
from ca.store import SqliteStore
from ca.store import migrations
from ca.store.federated import FederatedStore

REPO = "/repo"
FILE = "kernel/conf.c"
TU = CompilationUnit(
    id=tu_id(FILE, REPO, ["-c", FILE]),
    file=FILE,
    directory=REPO,
    repo_root=REPO,
    arguments=("-c", FILE),
)


def _sloc(line: int) -> Sloc:
    return Sloc(FILE, line, 3, line, 24)


def _function(name: str, line: int) -> FunctionRecord:
    usr = f"c:@F@{name}"
    return FunctionRecord(
        id=function_id(name=name, usr=usr, sloc=_sloc(line), linkage="external"),
        tu_id=TU.id,
        name=name,
        qualified_name=name,
        usr=usr,
        kind="function",
        sloc=_sloc(line),
        is_definition=True,
        linkage="external",
    )


MAIN = _function("main_caller", 10)
TARGET = _function("candidate_target", 40)
CALL = CallRecord(
    id=call_id(caller_id=MAIN.id, callee_usr=TARGET.usr, callee_name=TARGET.name, sloc=_sloc(12)),
    tu_id=TU.id,
    caller_id=MAIN.id,
    sloc=_sloc(12),
    callee_id=TARGET.id,
    callee_usr=TARGET.usr,
    callee_name=TARGET.name,
    callee_qualified_name=TARGET.name,
    kind="direct",
    is_resolved=True,
)


def _target(kind: str, reason: str) -> CallTargetRecord:
    return CallTargetRecord(
        id=call_target_id(CALL.id, TARGET.id, kind),
        call_id=CALL.id,
        caller_id=MAIN.id,
        callee_id=TARGET.id,
        callee_name=TARGET.name,
        kind=kind,
        reason=reason,
        sloc=_sloc(12),
    )


PARSE_RESULT = ParseResult(tu=TU, functions=[MAIN, TARGET], calls=[CALL])


def _write_candidate_store(path: Path, *, finalize: bool = True) -> SqliteStore:
    store = SqliteStore(path)
    store.open()
    store.begin()
    store.add_parse_result(PARSE_RESULT)
    store.add_analysis_result(
        AnalysisResult(
            function_id=MAIN.id,
            call_targets=[_target("indirect_candidate", "registration site")],
        )
    )
    store.add_callback_registrations(
        [
            {
                "object_path": "g_ops.read",
                "target_function_id": TARGET.id,
                "target_function_name": TARGET.name,
                "file": FILE,
                "line": 13,
                "tu_id": TU.id,
                "registration_kind": "struct_field",
                "evidence": ["g_ops.read = candidate_target"],
                "module": "kernel",
            }
        ]
    )
    store.commit()
    if finalize:
        store.finalize()
    return store


def _simulate_legacy(path: Path, *, legacy_index: bool = False) -> None:
    """Turn a v8 database into a v7-shaped one (confidence columns back)."""

    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "ALTER TABLE call_targets ADD COLUMN confidence REAL NOT NULL DEFAULT 0.0"
        )
        connection.execute(
            "ALTER TABLE callback_registrations "
            "ADD COLUMN confidence REAL NOT NULL DEFAULT 0.5"
        )
        connection.execute("UPDATE call_targets SET confidence = 0.9")
        connection.execute("UPDATE callback_registrations SET confidence = 0.7")
        if legacy_index:
            connection.execute(
                "CREATE INDEX idx_legacy_confidence ON call_targets(confidence)"
            )
        connection.commit()
    finally:
        connection.close()


def _columns(path: Path, table: str) -> list[str]:
    connection = sqlite3.connect(path)
    try:
        return [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
    finally:
        connection.close()


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_fresh_schema_has_no_confidence(tmp_path) -> None:
    db_path = tmp_path / "fresh.db"
    store = _write_candidate_store(db_path)
    try:
        meta = dict(store.connection.execute("SELECT key, value FROM meta"))
        assert meta["store_schema_version"] == "8"
        for table in ("call_targets", "callback_registrations"):
            assert "confidence" not in _columns(db_path, table)

        callees = store.query_callees("main_caller")
        assert callees and all("confidence" not in row for row in callees)
        raw_targets = store.query_call_targets("main_caller", direction="callee")
        assert raw_targets and all("confidence" not in row for row in raw_targets)
        graph = store.query_call_graph("main_caller", direction="forward", depth=1)
        assert graph["edges"] and all(
            "confidence" not in edge for edge in graph["edges"]
        )
        callbacks = store.query_callbacks()
        assert callbacks and all("confidence" not in row for row in callbacks)
        detail = store.query_function_detail("main_caller")
        assert "confidence" not in detail
        exported = list(store.iter_records(("call_target",)))
        assert exported and all("confidence" not in row for row in exported)
    finally:
        store.close()


def test_legacy_confidence_input_is_ignored(tmp_path) -> None:
    db_path = tmp_path / "legacy-input.db"
    store = SqliteStore(db_path)
    store.open()
    try:
        # Legacy analyze output may still carry the attribute/field on records.
        store.begin()
        store.add_parse_result(PARSE_RESULT)
        store.commit()
        store.add_call_targets(
            [
                {
                    "id": "ct:legacy-inline",
                    "call_id": CALL.id,
                    "caller_id": MAIN.id,
                    "callee_id": TARGET.id,
                    "callee_name": TARGET.name,
                    "kind": "indirect_candidate",
                    "reason": "legacy producer",
                    "confidence": 0.42,  # unknown field: ignored
                    "file": FILE,
                    "line": 12,
                }
            ]
        )
        assert store.add_callback_registrations(
            [
                {
                    "object_path": "g_ops.write",
                    "target_function_name": "legacy_handler",
                    "registration_kind": "registration_call",
                    "confidence": 0.31,  # unknown field: ignored
                    "evidence": ["register(&g_ops.write, legacy_handler)"],
                }
            ]
        ) == 1
        targets = store.query_call_targets("main_caller", direction="callee")
        assert targets and "confidence" not in targets[0]
        callbacks = store.query_callbacks(field="write")
        assert callbacks and "confidence" not in callbacks[0]
    finally:
        store.close()


def test_old_database_drop_column_migration(tmp_path) -> None:
    db_path = tmp_path / "v7.db"
    store = _write_candidate_store(db_path)
    store.close()
    _simulate_legacy(db_path)
    assert "confidence" in _columns(db_path, "call_targets")
    assert "confidence" in _columns(db_path, "callback_registrations")

    reopened = SqliteStore(db_path)
    with reopened:
        meta = dict(reopened.connection.execute("SELECT key, value FROM meta"))
        assert meta["store_schema_version"] == "8"
        assert "confidence" not in _columns(db_path, "call_targets")
        assert "confidence" not in _columns(db_path, "callback_registrations")
        # Existing rows/evidence survive the drop; queries stay confidence-free.
        assert reopened.stats()["tables"]["call_targets"] == 1
        assert reopened.stats()["tables"]["callback_registrations"] == 1
        target = reopened.query_call_targets("main_caller", direction="callee")[0]
        assert target["reason"] == "registration site"
        assert "confidence" not in target
        callback = reopened.query_callbacks()[0]
        assert callback["evidence"] == ["g_ops.read = candidate_target"]
        assert "confidence" not in callback
        reopened.finalize()  # still works after the migration
    # Reopening an already-migrated database is a no-op.
    again = SqliteStore(db_path)
    with again:
        assert "confidence" not in _columns(db_path, "call_targets")
        assert again.stats()["tables"]["call_targets"] == 1


def test_old_database_rebuild_fallback(tmp_path) -> None:
    db_path = tmp_path / "v7-rebuild.db"
    store = _write_candidate_store(db_path)  # finalize -> canonical indexes exist
    store.close()
    _simulate_legacy(db_path, legacy_index=True)
    # The legacy index on the dropped column makes DROP COLUMN fail.
    assert "idx_legacy_confidence" in _index_names(db_path)

    reopened = SqliteStore(db_path)
    with reopened:
        assert "confidence" not in _columns(db_path, "call_targets")
        assert "confidence" not in _columns(db_path, "callback_registrations")
        # Rebuild preserved the data and recreated the canonical indexes.
        assert reopened.stats()["tables"]["call_targets"] == 1
        assert "idx_legacy_confidence" not in _index_names(db_path)
        assert "idx_call_targets_caller_id" in _index_names(db_path)
        assert reopened.query_call_targets("main_caller", direction="callee")[0][
            "reason"
        ] == "registration site"
        reopened.finalize()
        assert reopened.stats()["tables"]["callback_registrations"] == 1


def _index_names(path: Path) -> set[str]:
    connection = sqlite3.connect(path)
    try:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%'"
            )
        }
    finally:
        connection.close()


def test_keep_legacy_column_when_migration_fails(monkeypatch) -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        "CREATE TABLE call_targets (id TEXT PRIMARY KEY, reason TEXT NOT NULL DEFAULT '', "
        "confidence REAL NOT NULL DEFAULT 0.0);"
        "INSERT INTO call_targets (id, reason, confidence) VALUES ('ct:1', 'why', 0.5);"
    )
    assert "confidence" in migrations.table_columns(connection, "call_targets")
    monkeypatch.setattr(
        migrations, "_drop_column", lambda *args, **kwargs: (_ for _ in ()).throw(
            sqlite3.OperationalError("forced drop failure")
        )
    )
    monkeypatch.setattr(
        migrations, "_rebuild_table", lambda *args, **kwargs: (_ for _ in ()).throw(
            sqlite3.OperationalError("forced rebuild failure")
        )
    )
    report = migrations.migrate_confidence_columns(connection)
    # No exception; the legacy column stays and the store remains usable.
    assert report["call_targets"] == "kept"
    assert "confidence" in migrations.table_columns(connection, "call_targets")
    assert connection.execute("SELECT reason FROM call_targets").fetchone()[0] == "why"
    connection.close()


def test_readonly_federated_shard_with_legacy_column(tmp_path) -> None:
    db_path = tmp_path / "legacy-shard.db"
    store = _write_candidate_store(db_path)
    store.close()
    _simulate_legacy(db_path)
    before = _hash(db_path)

    with FederatedStore([db_path]) as fed:
        assert fed.shards, fed.skipped
        callees = fed.query_callees("main_caller")
        assert callees and all("confidence" not in row for row in callees)
        targets = fed.query_call_targets("main_caller", direction="callee")
        assert targets and all("confidence" not in row for row in targets)
        graph = fed.query_call_graph("main_caller", direction="forward", depth=1)
        assert graph["edges"] and all(
            "confidence" not in edge for edge in graph["edges"]
        )
        callbacks = fed.query_callbacks()
        assert callbacks and all("confidence" not in row for row in callbacks)
    # Read-only federation never migrates/writes the shard.
    assert _hash(db_path) == before
    assert "confidence" in _columns(db_path, "call_targets")


def test_natural_key_dedupe_merges_reasons(tmp_path) -> None:
    db_path = tmp_path / "dedupe.db"
    store = _write_candidate_store(db_path)
    try:
        # A second candidate for the same natural edge (same caller/callee/site)
        # but a different kind: one edge whose reason joins both audits.
        store.add_call_targets(
            [
                {
                    "id": call_target_id(CALL.id, TARGET.id, "virtual_candidate"),
                    "call_id": CALL.id,
                    "caller_id": MAIN.id,
                    "callee_id": TARGET.id,
                    "callee_name": TARGET.name,
                    "kind": "virtual_candidate",
                    "reason": "virtual override depth=2",
                    "file": FILE,
                    "line": 12,
                }
            ]
        )
        callees = store.query_callees("main_caller")
        same_target = [row for row in callees if row["callee_id"] == TARGET.id]
        assert len(same_target) == 1
        edge = same_target[0]
        assert edge["source"] == "call"  # direct edge wins
        assert edge["candidate"] is False
        assert "registration site" in edge["reason"]
        assert "virtual override depth=2" in edge["reason"]

        raw = store.query_call_targets("main_caller", direction="callee")
        reasons = [row["reason"] for row in raw if row["callee_id"] == TARGET.id]
        assert len(reasons) == 1
        assert "registration site" in reasons[0]
        assert "virtual override depth=2" in reasons[0]

        graph = store.query_call_graph("main_caller", direction="forward", depth=1)
        graph_edges = [edge for edge in graph["edges"] if edge["callee_id"] == TARGET.id]
        assert len(graph_edges) == 1
        assert "virtual override depth=2" in graph_edges[0]["reason"]
    finally:
        store.close()


def test_default_schema_ddl_matches_canonical(tmp_path) -> None:
    """The v8 canonical DDL must not reintroduce the removed columns."""

    for table in ("call_targets", "callback_registrations"):
        ddl = migrations._canonical_ddl(table)
        assert "confidence" not in ddl
        assert "CREATE TABLE IF NOT EXISTS" in ddl
