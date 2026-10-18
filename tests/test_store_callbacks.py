"""Cross-TU callback registration facts: writes, indexed queries, links, upgrade."""

from __future__ import annotations

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

REPO = "/repo"
FILE = "foundation/net/net_conn.c"
TU = CompilationUnit(
    id=tu_id(FILE, REPO, ["-c", FILE]),
    file=FILE,
    directory=REPO,
    repo_root=REPO,
    arguments=("-c", FILE),
)

CALLBACK_KEYS = {
    "id",
    "object_path",
    "object_field",
    "target_function_id",
    "target_function_name",
    "target_usr",
    "file",
    "line",
    "col",
    "end_line",
    "end_col",
    "tu_id",
    "registration_kind",
    "evidence",
    "module",
    "directory",
    "registrar_function_id",
}


def _sloc(line: int, file: str = FILE) -> Sloc:
    return Sloc(file, line, 3, line, 24)


def _function(name: str, line: int, file: str = FILE) -> FunctionRecord:
    usr = f"c:@F@{name}"
    return FunctionRecord(
        id=function_id(name=name, usr=usr, sloc=_sloc(line, file), linkage="external"),
        tu_id=TU.id,
        name=name,
        qualified_name=name,
        usr=usr,
        kind="function",
        sloc=_sloc(line, file),
        is_definition=True,
        linkage="external",
    )


REGISTER = _function("net_register_callbacks", 10)
ON_DATA = _function("on_data_received", 40)
ON_CLOSE = _function("on_connection_closed", 50)

CALL = CallRecord(
    id=call_id(
        caller_id=REGISTER.id,
        callee_usr="",
        callee_name="ops.read",
        sloc=_sloc(20),
    ),
    tu_id=TU.id,
    caller_id=REGISTER.id,
    sloc=_sloc(20),
    callee_name="ops.read",
    kind="indirect",
)
TARGET = CallTargetRecord(
    id=call_target_id(CALL.id, ON_DATA.id, "indirect_candidate"),
    call_id=CALL.id,
    caller_id=REGISTER.id,
    callee_id=ON_DATA.id,
    callee_name=ON_DATA.name,
    kind="indirect_candidate",
    reason="registration fact g_conn.ops.read",
    sloc=_sloc(20),
)
PARSE_RESULT = ParseResult(
    tu=TU, functions=[REGISTER, ON_DATA, ON_CLOSE], calls=[CALL]
)
ANALYSIS = AnalysisResult(function_id=REGISTER.id, call_targets=[TARGET])


def _registrations() -> list[dict]:
    return [
        {
            "object_path": "g_conn.ops.read",
            "target_function_id": ON_DATA.id,
            "target_function_name": ON_DATA.name,
            "target_usr": ON_DATA.usr,
            "file": FILE,
            "line": 12,
            "col": 5,
            "end_line": 12,
            "end_col": 35,
            "tu_id": TU.id,
            "registration_kind": "struct_field",
            "confidence": 0.9,
            "evidence": ["g_conn.ops.read = on_data_received;"],
            "module": "foundation/net",
            "registrar_function_id": REGISTER.id,
        },
        {
            "object_path": "g_conn.ops.close",
            "target_function_id": ON_CLOSE.id,
            "target_function_name": ON_CLOSE.name,
            "file": FILE,
            "line": 13,
            "tu_id": TU.id,
            "registration_kind": "designated_initializer",
            "confidence": 0.75,
            "evidence": "g_conn.ops = { .close = on_connection_closed }",
            "module": "foundation/net",
        },
        {
            # Name-only target (function not in this TU yet) + other module.
            "object_path": "g_listeners[0].OnData",
            "target_function_name": "external_listener",
            "file": "foundation/listen/listener.c",
            "line": 7,
            "tu_id": "tu:other",
            "registration_kind": "array_element",
            "confidence": 0.6,
            "module": "foundation/listen",
        },
    ]


@pytest.fixture()
def store(tmp_path):
    instance = SqliteStore(tmp_path / "callbacks.db")
    instance.open()
    instance.begin()
    instance.add_parse_result(PARSE_RESULT)
    instance.add_analysis_result(ANALYSIS)
    instance.commit()
    instance.finalize()
    yield instance
    instance.close()


def test_add_and_round_trip(store) -> None:
    added = store.add_callback_registrations(_registrations())
    assert added == 3
    assert store.count_callbacks() == 3

    rows = store.query_callbacks(object_path="g_conn.ops.read")
    assert len(rows) == 1
    record = rows[0]
    assert set(record) == CALLBACK_KEYS
    assert record["id"].startswith("cbreg:")
    assert record["object_path"] == "g_conn.ops.read"
    assert record["object_field"] == "read"  # derived from the path
    assert record["target_function_id"] == ON_DATA.id
    assert record["target_function_name"] == "on_data_received"
    assert record["target_usr"] == ON_DATA.usr
    assert record["file"] == FILE and record["line"] == 12
    assert record["col"] == 5 and record["end_line"] == 12 and record["end_col"] == 35
    assert record["tu_id"] == TU.id
    assert record["registration_kind"] == "struct_field"
    # Legacy producers may still send confidence; it is not persisted/returned.
    assert "confidence" not in record
    assert record["evidence"] == ["g_conn.ops.read = on_data_received;"]
    assert record["module"] == "foundation/net"
    assert record["directory"] == "foundation/net"  # derived from file
    assert record["registrar_function_id"] == REGISTER.id

    explicit = [
        {
            "id": "cbreg:custom",
            "object_path": "ops->read",
            "target_function_name": "name_only_handler",
            "registration_kind": "registration_call",
        }
    ]
    assert store.add_callback_registrations(iter(explicit)) == 1
    row = store.get_callback("cbreg:custom")
    assert row is not None
    assert row["target_function_id"] == ""
    assert row["object_field"] == "read"
    assert "confidence" not in row
    assert row["evidence"] == []
    assert store.get_callback("cbreg:missing") is None


def test_dedupe_by_id_and_natural_key(store) -> None:
    assert store.add_callback_registrations(_registrations()) == 3
    assert store.add_callback_registrations(_registrations()) == 0
    assert store.count_callbacks() == 3

    # Same natural fact with a different explicit id is still deduplicated.
    duplicate = dict(_registrations()[0])
    duplicate["id"] = "cbreg:explicit-duplicate"
    assert store.add_callback_registrations([duplicate]) == 0
    assert store.count_callbacks() == 3
    # Unknown fields are ignored rather than rejected.
    extra = dict(_registrations()[1])
    extra["object_path"] = "g_conn.ops.write"
    extra["future_field"] = "ignored"
    assert store.add_callback_registrations([extra]) == 1
    assert "future_field" not in store.query_callbacks(object_path="g_conn.ops.write")[0]


def test_filters_prefix_field_module(store) -> None:
    store.add_callback_registrations(_registrations())
    assert {row["object_path"] for row in store.query_callbacks_by_path("g_conn.")} == {
        "g_conn.ops.read",
        "g_conn.ops.close",
    }
    assert store.query_callbacks_by_path("g_listeners[")[0]["target_function_name"] == (
        "external_listener"
    )
    assert store.query_callbacks_by_path("nope") == []
    assert len(store.query_callbacks_by_field("read")) == 1
    assert len(store.query_callbacks_by_module("foundation/net")) == 2
    assert {
        row["target_function_name"]
        for row in store.query_callbacks(tu_id=TU.id)
    } == {"on_data_received", "on_connection_closed"}
    assert len(store.query_callbacks(target_function_id=ON_DATA.id)) == 1
    # `min_confidence` was removed with the confidence column itself.
    with pytest.raises(TypeError):
        store.query_callbacks(min_confidence=0.8)
    assert len(store.query_callbacks(registration_kind="array_element")) == 1
    assert store.query_callbacks(limit=1)[0]["object_path"] == "g_conn.ops.close"
    assert len(store.query_callbacks(limit=2, offset=2)) == 1
    assert store.query_callbacks(limit=0) == []
    with pytest.raises(ValueError):
        store.query_callbacks(object_path="a", path_prefix="a")
    # Deterministic ordering: object_path first, then file/line/target name.
    ordered = [row["object_path"] for row in store.query_callbacks()]
    assert ordered == sorted(ordered)


def test_validation_and_empty_batches(store) -> None:
    assert store.add_callback_registrations([]) == 0
    with pytest.raises(ValueError):
        store.add_callback_registrations([{"target_function_name": "fn"}])
    with pytest.raises(ValueError):
        store.add_callback_registrations([{"object_path": "ops.read"}])
    with pytest.raises(ValueError):
        store.link_callback_targets([("only-one",)])


def test_callback_stats(store) -> None:
    store.add_callback_registrations(_registrations())
    stats = store.callback_stats()
    assert stats["registrations"] == 3
    assert stats["object_paths"] == 3
    assert stats["object_fields"] == 3
    assert stats["targets"] == 3  # 2 by id + 1 by name-only
    assert stats["modules"] == 2
    assert stats["directories"] == 2
    assert stats["tu_ids"] == 2
    assert stats["by_kind"] == {
        "array_element": 1,
        "designated_initializer": 1,
        "struct_field": 1,
    }
    assert stats["linked_call_targets"] == 0
    assert store.count_callbacks() == 3
    assert store.stats()["tables"]["callback_registrations"] == 3
    assert store.stats()["tables"]["callback_target_links"] == 0


def test_link_call_targets(store) -> None:
    records = store.add_callback_registrations(_registrations())
    assert records == 3
    registration = store.query_callbacks(object_path="g_conn.ops.read")[0]["id"]
    assert store.link_callback_targets(
        [{"registration_id": registration, "call_target_id": TARGET.id}]
    ) == 1
    assert store.link_callback_targets([(registration, TARGET.id)]) == 0  # idempotent
    targets = store.query_callback_targets(registration)
    assert len(targets) == 1
    target = targets[0]
    assert target["call_target_id"] == TARGET.id
    assert target["callee_name"] == ON_DATA.name
    assert target["kind"] == "indirect_candidate"
    assert target["reason"] == "registration fact g_conn.ops.read"
    reverse = store.query_registrations_for_target(TARGET.id)
    assert [row["id"] for row in reverse] == [registration]
    assert reverse[0]["evidence"] == ["g_conn.ops.read = on_data_received;"]
    assert store.callback_stats()["linked_call_targets"] == 1
    assert store.query_callback_targets("cbreg:missing") == []
    assert store.query_registrations_for_target("ct:missing") == []


def test_indexes_and_explain(store) -> None:
    store.add_callback_registrations(_registrations())
    indexes = {
        row[0]
        for row in store.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL"
        )
    }
    assert {
        "idx_callback_order",
        "idx_callback_natural",
        "idx_callback_field",
        "idx_callback_module",
        "idx_callback_tu_id",
        "idx_callback_target_id",
        "idx_callback_kind",
        "idx_callback_link_target",
    } <= indexes
    path_plan = store.connection.execute(
        "EXPLAIN QUERY PLAN SELECT id FROM callback_registrations "
        "WHERE object_path >= ? AND object_path < ?",
        ("g_conn.", "g_conn/"),
    ).fetchall()
    assert any("idx_callback_order" in row["detail"] for row in path_plan)
    order_plan = store.connection.execute(
        "EXPLAIN QUERY PLAN SELECT id FROM callback_registrations "
        "ORDER BY object_path, file, line, target_function_name, id LIMIT 10"
    ).fetchall()
    assert not any("TEMP B-TREE" in row["detail"] for row in order_plan)
    field_plan = store.connection.execute(
        "EXPLAIN QUERY PLAN SELECT id FROM callback_registrations WHERE object_field = ?",
        ("read",),
    ).fetchall()
    assert any("idx_callback_field" in row["detail"] for row in field_plan)


def test_phase4_database_upgrade_in_place(tmp_path) -> None:
    db_path = tmp_path / "upgrade.db"
    store = SqliteStore(db_path)
    store.open()
    store.begin()
    store.add_parse_result(PARSE_RESULT)
    store.add_analysis_result(ANALYSIS)
    store.commit()
    store.add_callback_registrations(_registrations()[:1])
    store.finalize()
    # Simulate a pre-callback (schema v3) database.
    store.connection.executescript(
        "DROP TABLE IF EXISTS callback_target_links;"
        "DROP TABLE IF EXISTS callback_registrations;"
        "DELETE FROM meta WHERE key = 'store_schema_version';"
    )
    store.close()

    reopened = SqliteStore(db_path)
    with reopened:
        assert reopened.stats()["tables"]["callback_registrations"] == 0
        assert reopened.query_function_detail("on_data_received")["function"]
        reopened.finalize()
        meta = dict(reopened.connection.execute("SELECT key, value FROM meta"))
        assert meta["store_schema_version"] == "8"
        indexes = {
            row[0]
            for row in reopened.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL"
            )
        }
        assert "idx_callback_order" in indexes
        assert reopened.add_callback_registrations(_registrations()) == 3
        assert reopened.count_callbacks() == 3
        # Existing graph features keep working after the in-place upgrade.
        assert reopened.query_call_targets("on_data_received", direction="caller")[0][
            "callee_name"
        ] == "on_data_received"
        reopened.finalize()  # idempotent
        assert reopened.count_callbacks() == 3
