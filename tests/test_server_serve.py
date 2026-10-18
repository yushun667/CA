"""End-to-end tests for ``ca serve``: queries, generic jobs, SSE and stateless MCP.

The AI explain feature has been removed entirely: there is no explain API,
sampling session, explain MCP tool or agent worker.  Generic jobs
(``/api/jobs`` + ``/events`` SSE) remain as background-task infrastructure.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))

from test_e2e_pipeline import write_fixture_repo  # noqa: E402


def _index(repo: Path, db: Path) -> None:
    from ca.build import load_compilation_database
    from ca.pipeline import run_index

    units = list(
        load_compilation_database(str(repo / "compile_commands.json"), str(repo))
    )
    summary = run_index(
        str(repo), units, str(db), jobs=1, analyze_scope="main", record_scope="all"
    )
    assert summary.tus_parsed == len(units), summary.errors


@pytest.fixture(scope="module")
def db_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("ca-serve")
    repo = write_fixture_repo(root / "repo")
    db = root / "ca.db"
    _index(repo, db)
    return db


@contextlib.contextmanager
def _server(db: Path, state_db: Path, db_dir: Path | None = None):
    from ca.server import ServeConfig, make_server

    config = ServeConfig(
        db_values=[str(db)] if db_dir is None else [],
        db_dir=str(db_dir) if db_dir else None,
        host="127.0.0.1",
        port=0,
        state_db=str(state_db),
    )
    server = make_server(config)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        server.service.close()
        thread.join(timeout=5)


def _get_json(url: str, timeout: float = 10.0) -> tuple[int, dict]:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        return exc.code, (json.loads(body) if body.strip() else {})


def _post_json(url: str, payload: dict, timeout: float = 20.0) -> tuple[int, dict]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            return response.status, (json.loads(body) if body.strip() else {})
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        return exc.code, (json.loads(body) if body.strip() else {})


def _tool_envelope(response: dict) -> dict:
    return json.loads(response["result"]["content"][0]["text"])


def test_health_query_and_stateless_mcp(db_path: Path, tmp_path: Path) -> None:
    with _server(db_path, tmp_path / "jobs.db") as server:
        base = server.url
        status, health = _get_json(base + "/api/health")
        assert status == 200 and health["ok"] is True
        features = health["data"]["features"]
        assert features["jobs"] is True and features["mcp"] is True
        assert "sampling" not in health["data"]
        assert "sampling_explain" not in features

        status, search = _get_json(base + "/api/search?q=proc")
        assert status == 200 and search["data"]["items"]
        status, stats = _get_json(base + "/api/stats")
        assert status == 200 and stats["data"]["tables"]["functions"] > 0

        status, init = _post_json(
            base + "/mcp",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"sampling": {}},
                    "clientInfo": {"name": "t", "version": "0"},
                },
            },
        )
        assert status == 200 and init["result"]["protocolVersion"] == "2025-06-18"
        status, tools = _post_json(
            base + "/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        )
        names = [tool["name"] for tool in tools["result"]["tools"]]
        assert len(names) == 21
        assert not {"explain_function", "explain_jobs", "submit_explanation"} & set(names)
        status, call = _post_json(
            base + "/mcp",
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "detail", "arguments": {"symbol": "proc_alloc"}},
            },
        )
        envelope = _tool_envelope(call)
        assert envelope["ok"] is True
        assert envelope["data"]["function"]["name"] == "proc_alloc"

        # Removed explain endpoints are gone.
        status, payload = _get_json(base + "/api/explain?symbol=proc_alloc")
        assert status == 404
        status, payload = _post_json(
            base + "/api/explain/agent", {"symbol": "proc_alloc", "lang": "en"}
        )
        assert status == 404


def test_generic_job_loop_and_sse(db_path: Path, tmp_path: Path) -> None:
    with _server(db_path, tmp_path / "jobs.db") as server:
        base = server.url
        status, created = _post_json(
            base + "/api/jobs",
            {"type": "update", "payload": {"note": "generic background task"}},
        )
        assert status == 201, created
        job = created["data"]["job"]
        job_id = job["id"]
        assert job["kind"] == "update" and job["status"] == "queued"

        status, listing = _get_json(base + "/api/jobs?kind=update&status=queued")
        assert status == 200 and listing["data"]["count"] == 1

        status, claimed = _post_json(
            base + f"/api/jobs/{job_id}/claim", {"worker": "test-worker"}
        )
        assert status == 200 and claimed["data"]["job"]["status"] == "running"
        assert claimed["data"]["job"]["claimed_by"] == "test-worker"

        status, submitted = _post_json(
            base + f"/api/jobs/{job_id}/result",
            {"worker": "test-worker", "result": {"summary": "done"}},
        )
        assert status == 200 and submitted["data"]["job"]["status"] == "done"
        assert submitted["data"]["job"]["result"] == {"summary": "done"}

        status, final = _get_json(base + f"/api/jobs/{job_id}")
        assert status == 200 and final["data"]["job"]["status"] == "done"
        _status, stats = _get_json(base + "/api/jobs")
        assert stats["data"]["stats"]["by_status"]["done"] == 1

        # SSE replays the generic job lifecycle.
        stream = urllib.request.urlopen(base + "/events?since_id=0", timeout=10)
        try:
            events: list[dict] = []
            buffer = b""
            deadline = time.time() + 10
            while time.time() < deadline and len(events) < 3:
                line = stream.readline()
                if not line:
                    break
                buffer += line
                while b"\n\n" in buffer:
                    block, buffer = buffer.split(b"\n\n", 1)
                    for raw in block.splitlines():
                        if raw.startswith(b"data: "):
                            event = json.loads(raw[len(b"data: "):].decode("utf-8"))
                            if event.get("type") == "job.updated" and event["job"]["id"] == job_id:
                                events.append(event)
            kinds = [event["event"] for event in events]
            assert "created" in kinds and "claimed" in kinds and "done" in kinds
            assert events[-1]["job"]["status"] == "done"
        finally:
            stream.close()


def test_restart_requeues_running_generic_jobs(db_path: Path, tmp_path: Path) -> None:
    state_db = tmp_path / "jobs.db"
    with _server(db_path, state_db) as server:
        status, created = _post_json(server.url + "/api/jobs", {"type": "update"})
        assert status == 201, created
        job_id = created["data"]["job"]["id"]
        status, _claimed = _post_json(
            server.url + f"/api/jobs/{job_id}/claim", {"worker": "crash-worker"}
        )
        assert status == 200
    with _server(db_path, state_db) as server:
        status, fetched = _get_json(server.url + f"/api/jobs/{job_id}")
        assert status == 200
        job = fetched["data"]["job"]
        assert job["status"] == "queued"
        assert job["claimed_by"] == ""
        assert job["attempts"] == 1


def test_mcp_serve_url_proxy(db_path: Path, tmp_path: Path) -> None:
    with _server(db_path, tmp_path / "jobs.db") as server:
        payload = "\n".join(
            json.dumps(message)
            for message in [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "t", "version": "0"},
                    },
                },
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            ]
        ) + "\n"
        result = subprocess.run(
            [sys.executable, "-m", "ca", "mcp", "--serve-url", server.url],
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
        responses = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        by_id = {item["id"]: item for item in responses}
        assert len(by_id[2]["result"]["tools"]) == 21


def test_federated_serve_reports_shards(db_path: Path, tmp_path: Path) -> None:
    shards = tmp_path / "shards"
    shards.mkdir()
    for name in ("shard1.db", "shard2.db"):
        for suffix in ("", "-wal", "-shm"):
            source = Path(str(db_path) + suffix)
            if source.exists():
                shutil.copy2(source, shards / f"{name}{suffix}")
    from ca.server import ServeConfig, make_server

    server = make_server(
        ServeConfig(
            db_values=[],
            db_dir=str(shards),
            host="127.0.0.1",
            port=0,
            state_db=str(tmp_path / "fed-jobs.db"),
        )
    )
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    try:
        status, stats = _get_json(server.url + "/api/stats")
        assert status == 200 and stats["data"]["shard_count"] == 2
        status, search = _get_json(server.url + "/api/search?q=proc_alloc")
        assert search["data"]["items"][0]["shard"] == "shard1"
        status, health = _get_json(server.url + "/api/health")
        assert health["data"]["features"]["federated"] is True
    finally:
        server.shutdown()
        server.server_close()
        server.service.close()
        thread.join(timeout=5)


def test_removed_explain_cli_and_flags() -> None:
    env = {
        "PYTHONPATH": str(REPO_ROOT / "src"),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(Path.home()),
    }
    top = subprocess.run(
        [sys.executable, "-m", "ca", "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        cwd=str(REPO_ROOT),
    )
    assert "agent-worker" not in (top.stdout + top.stderr)
    assert "agent-session" not in (top.stdout + top.stderr)
    serve_help = subprocess.run(
        [sys.executable, "-m", "ca", "serve", "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        cwd=str(REPO_ROOT),
    )
    assert serve_help.returncode == 0
    for flag in ("--sampling-timeout", "--explain-grace-ms", "--agent-url"):
        assert flag not in (serve_help.stdout + serve_help.stderr)
