"""End-to-end tests for workspace/project/variant registry (task-46).

Covers CLI CRUD, index-time registration, ``--variant`` query/graph routing,
the three new read-only MCP tools (18 -> 21) and the serve registry/active API.
"""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))

from test_e2e_pipeline import run_cli, write_fixture_repo  # noqa: E402
from test_server_serve import _get_json, _post_json, _server  # noqa: E402


def _index(repo: Path, db: Path, *extra: str) -> None:
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
            *extra,
        ]
    )
    assert code == 0


def _run_json(argv: list[str]) -> tuple[int, object, str]:
    from ca.cli.main import main

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    text = out.getvalue().strip()
    return code, (json.loads(text) if text else None), err.getvalue()


def _store(db: Path):
    from ca.store import SqliteStore

    return SqliteStore(db)


def _registry(db: Path) -> dict:
    store = _store(db)
    store.open()
    try:
        workspaces = store.list_workspaces()
        projects = []
        variants = []
        for workspace in workspaces:
            projects.extend(store.list_projects(workspace["id"]))
            variants.extend(store.list_variants(workspace["id"]))
        return {"workspaces": workspaces, "projects": projects, "variants": variants}
    finally:
        store.close()


def _variant_repo(root: Path) -> Path:
    repo = write_fixture_repo(root / "variant-repo")
    main_c = repo / "kernel" / "main.c"
    main_c.write_text(
        main_c.read_text(encoding="utf-8")
        + "\nint variant_only_probe(int value)\n{\n    return value + 7;\n}\n",
        encoding="utf-8",
    )
    return repo


def _variant_id(registry: dict, name: str) -> str:
    return next(
        variant["id"] for variant in registry["variants"] if variant["name"] == name
    )


def test_cli_registry_roundtrip_and_index_registration(tmp_path: Path) -> None:
    repo = write_fixture_repo(tmp_path / "repo")
    db = tmp_path / "index.db"
    _index(
        repo,
        db,
        "--workspace-path",
        str(repo),
        "--workspace-name",
        "WS",
        "--project-path",
        str(repo),
        "--project-name",
        "project",
        "--variant-name",
        "v1",
        "--variant-product",
        "p1",
        "--variant-board",
        "b1",
        "--variant-version",
        "1.0",
    )
    registry = _registry(db)
    assert len(registry["workspaces"]) == 1
    workspace = registry["workspaces"][0]
    assert workspace["name"] == "WS"
    assert len(registry["projects"]) == 1
    assert registry["projects"][0]["name"] == "project"
    assert len(registry["variants"]) == 1
    v1 = registry["variants"][0]
    assert v1["name"] == "v1" and v1["product"] == "p1"
    assert v1["db_path"] == str(db.resolve())

    # CLI list/show/add/remove round-trip on a second workspace.
    other = tmp_path / "other-workspace"
    other.mkdir()
    code, payload, err = _run_json(
        ["workspace", "add", "--db", str(db), "--path", str(other), "--name", "other", "--json"]
    )
    assert code == 0, err
    assert payload["workspace"]["id"].startswith("ws:")
    code, payload, err = _run_json(["workspace", "list", "--db", str(db), "--json"])
    assert code == 0 and payload["count"] == 2, err
    code, payload, err = _run_json(
        ["workspace", "show", str(other), "--db", str(db), "--json"]
    )
    assert code == 0 and payload["workspace"]["name"] == "other", err

    code, payload, err = _run_json(
        [
            "variant", "add", "--db", str(db), "--workspace", workspace["id"],
            "--name", "v2", "--product", "p2", "--board", "b2",
            "--db-path", str(tmp_path / "missing.db"), "--json",
        ]
    )
    assert code == 0, err
    v2_id = payload["variant"]["id"]
    code, payload, err = _run_json(
        ["variant", "list", "--db", str(db), "--workspace", workspace["id"], "--json"]
    )
    assert code == 0 and payload["count"] == 2, err
    by_name = {item["name"]: item for item in payload["items"]}
    assert by_name["v2"]["db_exists"] is False

    code, payload, err = _run_json(
        ["variant", "use", "v2", "--db", str(db), "--json"]
    )
    assert code == 0, err
    assert payload["variant"]["is_active"] is True
    registry = _registry(db)
    active = [v for v in registry["variants"] if v["is_active"]]
    assert len(active) == 1 and active[0]["id"] == v2_id

    code, payload, err = _run_json(
        ["workspace", "remove", str(other), "--db", str(db), "--json"]
    )
    assert code == 0 and payload["removed"]["workspaces"] == 1, err
    assert len(_registry(db)["workspaces"]) == 1

    # Error codes are stable: unknown selectors exit 1.
    assert run_cli(["workspace", "show", "ws:nope", "--db", str(db)]) == 1
    assert run_cli(["variant", "use", "var:nope", "--db", str(db)]) == 1
    assert run_cli(
        ["variant", "add", "--db", str(db), "--workspace", "ws:nope", "--name", "x"]
    ) == 1


def test_index_registration_requires_workspace_and_variant_name(tmp_path: Path) -> None:
    repo = write_fixture_repo(tmp_path / "repo")
    db = tmp_path / "index.db"
    assert (
        run_cli(
            [
                "index", "--repo", str(repo), "--db", str(db),
                "--source", "compdb", "--jobs", "1",
                "--variant-name", "v1",
            ]
        )
        == 1
    )
    assert (
        run_cli(
            [
                "index", "--repo", str(repo), "--db", str(db),
                "--source", "compdb", "--jobs", "1",
                "--workspace-path", str(repo), "--variant-product", "p",
            ]
        )
        == 1
    )


def test_query_and_graph_route_to_variant_db(tmp_path: Path) -> None:
    repo1 = write_fixture_repo(tmp_path / "repo1")
    db1 = tmp_path / "one.db"
    _index(
        repo1,
        db1,
        "--workspace-path", str(repo1), "--workspace-name", "WS",
        "--variant-name", "base",
    )
    workspace_id = _registry(db1)["workspaces"][0]["id"]
    repo2 = _variant_repo(tmp_path)
    db2 = tmp_path / "two.db"
    _index(repo2, db2)
    code, payload, err = _run_json(
        [
            "variant", "add", "--db", str(db1), "--workspace", workspace_id,
            "--name", "v2", "--db-path", str(db2), "--json",
        ]
    )
    assert code == 0, err
    variant_id = payload["variant"]["id"]

    # The marker exists only in db2.
    code, payload, err = _run_json(
        ["query", "functions", "variant_only_probe", "--db", str(db1), "--json"]
    )
    assert code == 2 and payload == [], err  # CLI convention: no rows -> 2
    code, payload, err = _run_json(
        [
            "query", "functions", "variant_only_probe", "--db", str(db1),
            "--variant", variant_id, "--json",
        ]
    )
    assert code == 0 and payload and payload[0]["name"] == "variant_only_probe", err

    code, payload, err = _run_json(
        [
            "graph", "--from", "variant_only_probe", "--depth", "1",
            "--db", str(db1), "--variant", "v2", "--format", "json",
        ]
    )
    assert code == 0, err
    assert payload["root"] == "variant_only_probe"
    assert any(node["name"] == "variant_only_probe" for node in payload["nodes"])

    assert (
        run_cli(
            ["query", "functions", "proc_alloc", "--db", str(db1), "--variant", "missing"]
        )
        == 1
    )
    assert (
        run_cli(["query", "functions", "proc", "--db-dir", str(tmp_path), "--variant", "v2"])
        == 1
    )


def test_mcp_registry_tools(tmp_path: Path) -> None:
    repo = write_fixture_repo(tmp_path / "repo")
    db = tmp_path / "index.db"
    _index(
        repo,
        db,
        "--workspace-path", str(repo), "--workspace-name", "WS",
        "--project-path", str(repo), "--project-name", "project",
        "--variant-name", "v1",
    )
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "workspaces", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "projects", "arguments": {"workspace": str(repo)}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "variants", "arguments": {"variant": "v1"}}},
    ]
    payload = "\n".join(json.dumps(message) for message in messages) + "\n"
    result = subprocess.run(
        [sys.executable, "-m", "ca", "mcp", "--db", str(db)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=120,
        env={
            "PYTHONPATH": str(REPO_ROOT / "src"),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(Path.home()),
        },
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, result.stderr[-2000:]
    responses = {
        item["id"]: item
        for item in (json.loads(line) for line in result.stdout.splitlines() if line.strip())
    }
    tools = [tool["name"] for tool in responses[2]["result"]["tools"]]
    assert len(tools) == 21
    assert {"workspaces", "projects", "variants"} <= set(tools)

    def envelope(response_id: int) -> dict:
        return json.loads(responses[response_id]["result"]["content"][0]["text"])

    workspaces = envelope(3)
    assert workspaces["ok"] is True
    assert workspaces["data"]["count"] == 1
    assert workspaces["data"]["items"][0]["name"] == "WS"
    projects = envelope(4)
    assert projects["ok"] is True and projects["data"]["items"][0]["name"] == "project"
    variants = envelope(5)
    assert variants["ok"] is True
    assert variants["data"]["variant"]["name"] == "v1"


def test_serve_registry_and_active_switch(tmp_path: Path) -> None:
    repo1 = write_fixture_repo(tmp_path / "repo1")
    db1 = tmp_path / "one.db"
    _index(
        repo1,
        db1,
        "--workspace-path", str(repo1), "--workspace-name", "WS",
        "--variant-name", "base",
    )
    workspace_id = _registry(db1)["workspaces"][0]["id"]
    repo2 = _variant_repo(tmp_path)
    db2 = tmp_path / "two.db"
    _index(repo2, db2)
    code, payload, err = _run_json(
        [
            "variant", "add", "--db", str(db1), "--workspace", workspace_id,
            "--name", "v2", "--db-path", str(db2), "--json",
        ]
    )
    assert code == 0, err
    variant_id = payload["variant"]["id"]
    # A registered variant with no usable database (UI disables it).
    code, payload, err = _run_json(
        [
            "variant", "add", "--db", str(db1), "--workspace", workspace_id,
            "--name", "unbuilt", "--db-path", str(tmp_path / "missing.db"), "--json",
        ]
    )
    assert code == 0, err
    unbuilt_id = payload["variant"]["id"]

    with _server(db1, tmp_path / "state.db") as server:
        status, registry = _get_json(server.url + "/api/registry")
        assert status == 200 and registry["ok"] is True
        data = registry["data"]
        assert data["active_variant"]["name"] == "base"
        assert data["registry_source"] == str(db1.resolve())
        assert len(data["workspaces"]) == 1 and len(data["variants"]) == 3
        by_name = {variant["name"]: variant for variant in data["variants"]}
        assert by_name["v2"]["db_exists"] is True
        assert by_name["unbuilt"]["db_exists"] is False
        assert [variant["name"] for variant in data["variants"]] == sorted(by_name)

        status, search = _get_json(server.url + "/api/search?q=variant_only_probe")
        assert status == 200 and search["data"]["count"] == 0

        status, switched = _post_json(
            server.url + "/api/registry/active", {"variant_id": variant_id}
        )
        assert status == 200 and switched["ok"] is True, switched
        active = switched["data"]["active_variant"]
        assert active["id"] == variant_id
        assert switched["data"]["active_variant_id"] == variant_id
        assert switched["data"]["health"]["database"]["paths"] == [str(db2.resolve())]
        status, search = _get_json(server.url + "/api/search?q=variant_only_probe")
        assert status == 200 and search["data"]["items"][0]["name"] == "variant_only_probe"

        # Registry source stays fixed after the switch.
        status, registry_after = _get_json(server.url + "/api/registry")
        assert registry_after["data"]["registry_source"] == str(db1.resolve())
        assert registry_after["data"]["active_variant"]["id"] == variant_id

        status, missing = _post_json(
            server.url + "/api/registry/active", {"variant_id": "var:nope"}
        )
        assert status == 404 and missing["error"]["code"] == "NOT_FOUND"
        status, bad = _post_json(server.url + "/api/registry/active", {})
        assert status == 400 and bad["error"]["code"] == "INVALID_ARGUMENT"
        status, unavailable = _post_json(
            server.url + "/api/registry/active", {"variant_id": unbuilt_id}
        )
        assert status == 503 and unavailable["error"]["code"] == "DB_UNAVAILABLE"
        # Failure leaves the previously active variant in place.
        status, search = _get_json(server.url + "/api/search?q=variant_only_probe")
        assert status == 200 and search["data"]["count"] == 1
