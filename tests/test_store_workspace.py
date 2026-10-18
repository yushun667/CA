"""Multi-repo workspace / project / variant metadata and federation seam."""

from __future__ import annotations

import sqlite3

import pytest

from ca.store import SqliteStore

OH_PATH = "/home/yu/CA_OH/openharmony"
OH_PRODUCT = "opi5plus"
OH_BOARD = "rk3588"
OH_VERSION = "OpenHarmony-5.0.0-Release"


@pytest.fixture()
def store(tmp_path):
    instance = SqliteStore(tmp_path / "workspace.db")
    with instance:
        yield instance


def _projects(count: int = 500) -> list[dict]:
    return [
        {
            "path": f"foundation/ability/project_{index:03d}",
            "name": f"project_{index:03d}",
            "repo": f"repo_{index:03d}",
            "kind": "repo",
        }
        for index in range(count)
    ]


def test_openharmony_workspace_registration(store) -> None:
    workspace = store.upsert_workspace(
        OH_PATH,
        "OpenHarmony",
        "local",
        {"manifest": "default.xml", "repo_count": 500},
    )
    assert workspace.startswith("ws:")
    assert store.register_projects(workspace, _projects(500)) == 500
    variant = store.register_variant(
        workspace,
        OH_PRODUCT,
        product=OH_PRODUCT,
        board=OH_BOARD,
        version=OH_VERSION,
        db_path="/home/yu/CA_OH/.ca/index-opi5plus.db",
        metadata={"kernel": "linux-6.6"},
        is_active=True,
    )
    assert variant.startswith("var:")
    assert variant.endswith("")  # deterministic prefix only; id is opaque

    # Lookup by id and by path are equivalent.
    by_id = store.get_workspace(workspace)
    by_path = store.get_workspace(OH_PATH)
    assert by_id is not None and by_path is not None
    assert by_id["id"] == by_path["id"] == workspace
    assert by_id["path"] == OH_PATH
    assert by_id["name"] == "OpenHarmony"
    assert by_id["metadata"] == {"manifest": "default.xml", "repo_count": 500}
    assert by_id["created_at"]

    summary = store.workspace_summary(OH_PATH)
    assert summary["workspace"]["id"] == workspace
    assert summary["projects"] == 500
    assert summary["variants"] == 1
    assert summary["products"] == [OH_PRODUCT]

    all_projects = store.list_projects(workspace)
    assert len(all_projects) == 500
    assert all_projects[0]["workspace_id"] == workspace
    assert all_projects[0]["metadata"] == {}
    assert len(store.list_projects(workspace, limit=7)) == 7

    variants = store.list_variants(workspace)
    assert len(variants) == 1
    record = variants[0]
    assert record["name"] == OH_PRODUCT
    assert record["product"] == OH_PRODUCT
    assert record["board"] == OH_BOARD
    assert record["version"] == OH_VERSION
    assert record["db_path"].endswith("index-opi5plus.db")
    assert record["is_active"] is True
    assert record["metadata"] == {"kernel": "linux-6.6"}
    assert store.get_variant(variant)["id"] == variant
    assert store.get_variant("var:nope") is None


def test_upsert_is_idempotent_and_updates(store) -> None:
    first = store.upsert_workspace(OH_PATH, "OH")
    same = store.upsert_workspace(OH_PATH + "/", "OpenHarmony", metadata={"updated": True})
    assert first == same
    assert len(store.list_workspaces()) == 1
    record = store.get_workspace(first)
    assert record["name"] == "OpenHarmony"
    assert record["metadata"] == {"updated": True}

    store.register_projects(first, _projects(3))
    store.register_projects(first, _projects(3))
    assert len(store.list_projects(first)) == 3

    variant_a = store.register_variant(first, "opi5plus", product="opi5plus", board="rk3588")
    variant_b = store.register_variant(first, "opi5plus", product="opi5plus", board="rk3588")
    assert variant_a == variant_b
    assert len(store.list_variants(first)) == 1


def test_register_projects_replace_refreshes_manifest(store) -> None:
    workspace = store.upsert_workspace(OH_PATH)
    store.register_projects(workspace, _projects(10))
    assert len(store.list_projects(workspace)) == 10
    store.register_projects(workspace, [{"path": "only/one", "name": "one"}], replace=True)
    remaining = store.list_projects(workspace)
    assert len(remaining) == 1
    assert remaining[0]["path"] == "only/one"


def test_metadata_validation_errors(store) -> None:
    with pytest.raises(ValueError):
        store.upsert_workspace("   ")
    with pytest.raises(ValueError):
        store.register_projects("ws:missing", [{"path": "a"}])
    with pytest.raises(ValueError):
        store.register_variant("ws:missing", "v")
    workspace = store.upsert_workspace(OH_PATH)
    with pytest.raises(ValueError):
        store.register_projects(workspace, [{"name": "no-path"}])
    with pytest.raises(ValueError):
        store.register_variant(workspace, "")
    with pytest.raises(ValueError):
        store.attach_database(":memory:", "bad-alias; DROP TABLE projects")
    with pytest.raises(ValueError):
        store.detach_database("bad alias")


def test_unknown_workspace_summary(store) -> None:
    assert store.workspace_summary("ws:nope") == {
        "workspace": None,
        "projects": 0,
        "variants": 0,
        "products": [],
    }
    assert store.list_projects("ws:nope") == []
    assert store.list_variants("ws:nope") == []


def test_federation_attach_detach_seam(store, tmp_path) -> None:
    other = sqlite3.connect(tmp_path / "other-index.db")
    other.execute("CREATE TABLE marker (id TEXT PRIMARY KEY, value TEXT)")
    other.execute("INSERT INTO marker VALUES ('m1', 'other-variant')")
    other.commit()
    other.close()

    store.attach_database(str(tmp_path / "other-index.db"), "other_variant")
    rows = store.connection.execute(
        "SELECT id, value FROM other_variant.marker"
    ).fetchall()
    assert [tuple(row) for row in rows] == [("m1", "other-variant")]
    names = {entry["name"] for entry in store.attached_databases()}
    assert {"main", "other_variant"} <= names
    store.detach_database("other_variant")
    names = {entry["name"] for entry in store.attached_databases()}
    assert "other_variant" not in names


def test_store_schema_version_and_stats(store) -> None:
    meta = dict(store.connection.execute("SELECT key, value FROM meta"))
    assert meta["store_schema_version"] == "4"
    assert meta["schema_version"] == "1"  # core contract version stays 1
    tables = store.stats()["tables"]
    assert {"call_targets", "deep_status", "workspaces", "projects", "variants"} <= set(tables)

    workspace = store.upsert_workspace(OH_PATH)
    store.register_projects(workspace, _projects(2))
    store.register_variant(workspace, "v1")
    tables = store.stats()["tables"]
    assert tables["workspaces"] == 1
    assert tables["projects"] == 2
    assert tables["variants"] == 1
