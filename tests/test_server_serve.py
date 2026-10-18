"""End-to-end tests for ``ca serve``: unified backend, jobs, SSE and MCP.

The server is started in a thread on an ephemeral port (``port=0``); tests use
plain ``urllib`` (no external HTTP client dependency). Cross-client loop:
Web-UI style HTTP creates an ``explain`` job -> MCP agent claims it over
``/mcp`` -> submits an explanation -> the job becomes ``done`` and ``/events``
replays ``created``/``claimed``/``done`` events.
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
def _server(db: Path, state_db: Path, db_dir: Path | None = None, grace_ms: int = 0):
    from ca.server import ServeConfig, make_server

    config = ServeConfig(
        db_values=[str(db)] if db_dir is None else [],
        db_dir=str(db_dir) if db_dir else None,
        host="127.0.0.1",
        port=0,
        state_db=str(state_db),
        explain_grace_ms=grace_ms,
    )
    server = make_server(config)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
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
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _post_json(url: str, payload: dict, timeout: float = 20.0) -> tuple[int, dict]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _mcp(base: str, messages: list[dict]) -> list[dict]:
    request = urllib.request.Request(
        base + "/mcp",
        data=json.dumps(messages).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = response.read().decode("utf-8")
    return json.loads(payload) if payload else []


def _tool_envelope(response: dict) -> dict:
    return json.loads(response["result"]["content"][0]["text"])


def test_health_query_and_mcp_endpoint(db_path: Path, tmp_path: Path) -> None:
    with _server(db_path, tmp_path / "jobs.db") as server:
        base = server.url
        status, health = _get_json(base + "/api/health")
        assert status == 200 and health["ok"] is True
        assert health["data"]["features"]["jobs"] is True
        assert health["data"]["features"]["mcp"] is True

        status, search = _get_json(base + "/api/search?q=proc")
        assert status == 200 and search["data"]["items"]

        status, stats = _get_json(base + "/api/stats")
        assert status == 200 and stats["data"]["tables"]["functions"] > 0

        responses = _mcp(
            base,
            [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "detail", "arguments": {"symbol": "proc_alloc"}}},
            ],
        )
        by_id = {item["id"]: item for item in responses}
        tools = by_id[2]["result"]["tools"]
        assert len(tools) == 20
        assert {"explain_jobs", "submit_explanation"} <= {tool["name"] for tool in tools}
        detail = _tool_envelope(by_id[3])
        assert detail["ok"] is True
        assert detail["data"]["function"]["name"] == "proc_alloc"


def test_explain_job_cross_client_loop(db_path: Path, tmp_path: Path) -> None:
    with _server(db_path, tmp_path / "jobs.db") as server:
        base = server.url
        status, created = _post_json(
            base + "/api/jobs",
            {"type": "explain", "symbol": "proc_alloc", "payload": {"lang": "en"}},
        )
        assert status == 201, created
        job = created["data"]["job"]
        job_id = job["id"]
        assert job["status"] == "queued" and job["symbol"] == "proc_alloc"

        claim = _tool_envelope(
            _mcp(
                base,
                [
                    {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "explain_jobs", "arguments": {"action": "claim", "worker": "test-agent"}}}
                ],
            )[0]
        )
        assert claim["ok"] is True
        assert claim["data"]["job"]["status"] == "running"
        assert claim["data"]["job"]["claimed_by"] == "test-agent"

        submitted = _tool_envelope(
            _mcp(
                base,
                [
                    {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "submit_explanation", "arguments": {"job_id": job_id, "explanation": "allocates a page", "evidence": ["kernel/proc.c:5"], "worker": "test-agent"}}}
                ],
            )[0]
        )
        assert submitted["ok"] is True
        assert submitted["data"]["job"]["status"] == "done"
        assert submitted["data"]["job"]["result"]["explanation"] == "allocates a page"

        status, final = _get_json(base + f"/api/jobs/{job_id}")
        assert status == 200 and final["data"]["job"]["status"] == "done"

        # SSE replay (since_id=0) delivers created/claimed/done snapshots.
        stream = urllib.request.urlopen(base + "/events?since_id=0", timeout=10)
        try:
            events: list[dict] = []
            deadline = time.time() + 10
            buffer = b""
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
                            if event.get("type") == "job.updated" and event.get("job", {}).get("id") == job_id:
                                events.append(event)
            kinds = [event["event"] for event in events]
            assert "created" in kinds and "claimed" in kinds and "done" in kinds
            assert events[-1]["job"]["status"] == "done"
        finally:
            stream.close()


def _create_explain(base: str, symbol: str = "proc_alloc") -> str:
    status, created = _post_json(
        base + "/api/jobs", {"type": "explain", "symbol": symbol}
    )
    assert status == 201, created
    return created["data"]["job"]["id"]


def _wait_job(base: str, job_id: str, status: str, timeout: float = 8.0) -> dict:
    deadline = time.time() + timeout
    payload: dict = {}
    while time.time() < deadline:
        code, payload = _get_json(base + f"/api/jobs/{job_id}")
        assert code == 200, payload
        job = payload["data"]["job"]
        if job["status"] == status:
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not reach {status}: {payload}")


def test_explain_auto_complete_without_agent(db_path: Path, tmp_path: Path) -> None:
    with _server(db_path, tmp_path / "jobs.db", grace_ms=80) as server:
        job_id = _create_explain(server.url)
        job = _wait_job(server.url, job_id, "done")
        result = job["result"]
        assert result["provider"] in ("heuristic", "llm")
        assert result["fallback"] is True
        assert result["auto_completed"] is True
        assert result["explanation"].strip()
        assert job["claimed_by"] == "server:auto"
        stream = urllib.request.urlopen(server.url + "/events?since_id=0", timeout=10)
        try:
            done: list[dict] = []
            deadline = time.time() + 8
            buffer = b""
            while time.time() < deadline and not done:
                line = stream.readline()
                if not line:
                    break
                buffer += line
                while b"\n\n" in buffer:
                    block, buffer = buffer.split(b"\n\n", 1)
                    for raw in block.splitlines():
                        if raw.startswith(b"data: "):
                            event = json.loads(raw[len(b"data: "):].decode("utf-8"))
                            if event.get("event") == "done" and event["job"]["id"] == job_id:
                                done.append(event)
            assert done and done[0]["job"]["result"]["fallback"] is True
        finally:
            stream.close()


def test_agent_claim_beats_auto_complete(db_path: Path, tmp_path: Path) -> None:
    with _server(db_path, tmp_path / "jobs.db", grace_ms=600) as server:
        job_id = _create_explain(server.url)
        claim = _tool_envelope(
            _mcp(
                server.url,
                [{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "explain_jobs", "arguments": {"action": "claim", "worker": "fast-agent"}}}],
            )[0]
        )
        assert claim["data"]["job"]["id"] == job_id
        time.sleep(0.9)  # past the grace window: auto-completion must skip
        status, fetched = _get_json(server.url + f"/api/jobs/{job_id}")
        assert status == 200
        assert fetched["data"]["job"]["status"] == "running"
        submitted = _tool_envelope(
            _mcp(
                server.url,
                [{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "submit_explanation", "arguments": {"job_id": job_id, "explanation": "agent wins", "worker": "fast-agent"}}}],
            )[0]
        )
        assert submitted["data"]["job"]["status"] == "done"
        result = submitted["data"]["job"]["result"]
        assert result["provider"] == "agent"
        assert "fallback" not in result
        time.sleep(0.3)
        _code, again = _get_json(server.url + f"/api/jobs/{job_id}")
        assert again["data"]["job"]["result"]["explanation"] == "agent wins"


def test_explain_auto_complete_disabled(db_path: Path, tmp_path: Path) -> None:
    with _server(db_path, tmp_path / "jobs.db", grace_ms=0) as server:
        job_id = _create_explain(server.url)
        time.sleep(0.35)
        code, fetched = _get_json(server.url + f"/api/jobs/{job_id}")
        assert code == 200
        assert fetched["data"]["job"]["status"] == "queued"


def test_agent_worker_cli_consumes_job(db_path: Path, tmp_path: Path) -> None:
    with _server(db_path, tmp_path / "jobs.db", grace_ms=0) as server:
        job_id = _create_explain(server.url, symbol="proc_free")
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "ca",
                "agent-worker",
                "--serve-url",
                server.url,
                "--once",
                "--worker",
                "test-worker",
            ],
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
        assert "done" in result.stderr
        job = _wait_job(server.url, job_id, "done")
        assert job["claimed_by"] == "test-worker"
        assert job["result"]["provider"] == "agent"
        assert job["result"]["model"] == "mcp-agent"
        assert job["result"]["explanation"].strip()
        assert job["result"]["evidence"]


def test_restart_reschedules_queued_explain_jobs(
    db_path: Path, tmp_path: Path
) -> None:
    state_db = tmp_path / "jobs.db"
    with _server(db_path, state_db, grace_ms=0) as server:
        job_id = _create_explain(server.url)
        _wait_job(server.url, job_id, "queued", timeout=1.0) if False else None
    with _server(db_path, state_db, grace_ms=80) as server:
        job = _wait_job(server.url, job_id, "done", timeout=8.0)
        assert job["result"]["fallback"] is True


def test_restart_requeues_running_jobs(db_path: Path, tmp_path: Path) -> None:
    from ca.server.jobs import JobService

    state_db = tmp_path / "jobs.db"
    with _server(db_path, state_db) as server:
        status, created = _post_json(server.url + "/api/jobs", {"type": "explain", "symbol": "proc_free"})
        assert status == 201, created
        job_id = created["data"]["job"]["id"]
        claim = _tool_envelope(
            _mcp(
                server.url,
                [
                    {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "explain_jobs", "arguments": {"action": "claim", "worker": "crash-agent"}}}
                ],
            )[0]
        )
        assert claim["data"]["job"]["status"] == "running"

    # A fresh service on the same state DB recovers the interrupted job.
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
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
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
        assert len(by_id[2]["result"]["tools"]) == 20


def test_federated_serve_reports_shards(db_path: Path, tmp_path: Path) -> None:
    shards = tmp_path / "shards"
    shards.mkdir()
    for name in ("shard1.db", "shard2.db"):
        for suffix in ("", "-wal", "-shm"):
            source = Path(str(db_path) + suffix)
            if source.exists():
                shutil.copy2(source, shards / f"{name}{suffix}")
    from ca.server import ServeConfig, make_server

    service = make_server(
        ServeConfig(db_values=[], db_dir=str(shards), host="127.0.0.1", port=0, state_db=str(tmp_path / "fed-jobs.db"))
    )
    thread = threading.Thread(target=service.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        status, stats = _get_json(service.url + "/api/stats")
        assert status == 200
        assert stats["data"]["shard_count"] == 2
        status, search = _get_json(service.url + "/api/search?q=proc_alloc")
        assert search["data"]["items"][0]["shard"] == "shard1"
        status, health = _get_json(service.url + "/api/health")
        assert health["data"]["features"]["federated"] is True
        assert Path(stats["data"]["shards"][0]["path"]).name.startswith("shard")
    finally:
        service.shutdown()
        service.server_close()
        service.service.close()
        thread.join(timeout=5)
