"""Tests for the read-only web UI: JSON API, static page and safety guarantees.

Every test builds a temporary CA database directly through ``ca.store`` (no
Clang/compile-database dependency), starts the UI server on a random loopback
port in a background thread, and talks plain HTTP. No network access, no
long-running server and no mutation of the database are allowed.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import socket
import sqlite3
import time
import subprocess
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

import pytest

from ca.core.ids import (
    assignment_id,
    branch_id,
    call_id,
    cfg_block_id,
    cfg_edge_id,
    call_target_id,
    function_id,
    resource_op_id,
    return_id,
    tu_id,
)
from ca.core.schema import (
    AnalysisResult,
    AssignmentRecord,
    BranchRecord,
    CallRecord,
    CallTargetRecord,
    CfgBlockRecord,
    CfgEdgeRecord,
    CompilationUnit,
    FunctionRecord,
    ParamRecord,
    ParseResult,
    ResourceOpRecord,
    ReturnRecord,
    Sloc,
)
from ca.store import SqliteStore

REPO = "/repo"
FILE = "kernel/proc.c"


# --------------------------------------------------------------------------- #
# Database fixtures
# --------------------------------------------------------------------------- #


def _sloc(
    line: int, file: str = FILE, col: int = 5, end_line: int | None = None
) -> Sloc:
    return Sloc(file, line, col, end_line if end_line is not None else line, col + 24)


def _tu(file: str) -> CompilationUnit:
    return CompilationUnit(
        id=tu_id(file, REPO, ["-c", file]),
        file=file,
        directory=REPO,
        repo_root=REPO,
        arguments=("-c", file),
    )


def _function(
    name: str,
    line: int,
    tu: CompilationUnit,
    *,
    file: str,
    params: tuple[ParamRecord, ...] = (),
    is_definition: bool = True,
    end_line: int | None = None,
) -> FunctionRecord:
    usr = f"c:@F@{name}"
    sloc = _sloc(line, file, end_line=end_line)
    return FunctionRecord(
        id=function_id(name=name, usr=usr, sloc=sloc, linkage="external"),
        tu_id=tu.id,
        name=name,
        qualified_name=name,
        usr=usr,
        kind="function",
        sloc=sloc,
        is_definition=is_definition,
        return_type="int",
        signature=f"int {name}(void)",
        display_name=name,
        linkage="external",
        params=params,
    )


def _call(
    caller: FunctionRecord, callee: FunctionRecord, line: int, file: str = FILE
) -> CallRecord:
    sloc = _sloc(line, file)
    return CallRecord(
        id=call_id(
            caller_id=caller.id,
            callee_usr=callee.usr,
            callee_name=callee.name,
            sloc=sloc,
        ),
        tu_id=caller.tu_id,
        caller_id=caller.id,
        sloc=sloc,
        callee_id=callee.id,
        callee_usr=callee.usr,
        callee_name=callee.name,
        callee_qualified_name=callee.qualified_name,
        kind="direct",
        is_resolved=True,
    )


def _build_main_db(path: Path) -> dict[str, str]:
    """One small index with functions, calls, deep facts and callback facts."""

    tu = _tu(FILE)
    main = _function("main", 10, tu, file=FILE, params=(ParamRecord("argc", "int", 0, _sloc(10)),))
    proc_init = _function("proc_init", 20, tu, file=FILE)
    proc_alloc = _function("proc_alloc", 30, tu, file=FILE)
    proc_free = _function("proc_free", 35, tu, file=FILE)
    kmalloc = _function("kmalloc", 40, tu, file="kernel/mm.c")
    read_impl = _function("read_impl", 50, tu, file=FILE)
    calls = [
        _call(main, proc_init, 12),
        _call(main, proc_alloc, 14),
        _call(proc_init, kmalloc, 22),
        _call(proc_init, proc_free, 24),
    ]
    entry_block = CfgBlockRecord(
        id=cfg_block_id(proc_init.id, 0),
        function_id=proc_init.id,
        index=0,
        kind="entry",
        terminator="conditional",
        sloc=_sloc(20),
        statement_count=2,
    )
    body_block = CfgBlockRecord(
        id=cfg_block_id(proc_init.id, 1),
        function_id=proc_init.id,
        index=1,
        kind="normal",
        terminator="unconditional",
        sloc=_sloc(21),
        statement_count=3,
    )
    exit_block = CfgBlockRecord(
        id=cfg_block_id(proc_init.id, 2),
        function_id=proc_init.id,
        index=2,
        kind="exit",
        terminator="return",
        sloc=_sloc(26),
        statement_count=1,
    )
    cfg_edges = [
        CfgEdgeRecord(
            id=cfg_edge_id(proc_init.id, entry_block.id, body_block.id, "true"),
            function_id=proc_init.id,
            src=entry_block.id,
            dst=body_block.id,
            kind="true",
        ),
        CfgEdgeRecord(
            id=cfg_edge_id(proc_init.id, entry_block.id, exit_block.id, "false"),
            function_id=proc_init.id,
            src=entry_block.id,
            dst=exit_block.id,
            kind="false",
        ),
        CfgEdgeRecord(
            id=cfg_edge_id(
                proc_init.id, body_block.id, exit_block.id, "unconditional"
            ),
            function_id=proc_init.id,
            src=body_block.id,
            dst=exit_block.id,
            kind="unconditional",
        ),
    ]
    branch = BranchRecord(
        id=branch_id(proc_init.id, _sloc(21), 0),
        function_id=proc_init.id,
        block_id=entry_block.id,
        sloc=_sloc(21),
        kind="if",
        condition="flags & PROC_READY",
        var_names=("flags",),
        true_block=body_block.id,
        false_block=exit_block.id,
    )
    assignment = AssignmentRecord(
        id=assignment_id(proc_init.id, _sloc(23), 0),
        function_id=proc_init.id,
        block_id="block:body",
        sloc=_sloc(23),
        lhs="p",
        rhs="kmalloc(32, 0)",
        var_names=("p",),
        is_decl_init=True,
        operator="=",
        type_spelling="void *",
    )
    returned = ReturnRecord(
        id=return_id(proc_init.id, _sloc(26), 0),
        function_id=proc_init.id,
        block_id="block:exit",
        sloc=_sloc(26),
        expr="0",
        var_names=(),
    )
    resource = ResourceOpRecord(
        id=resource_op_id(proc_init.id, _sloc(23), "kmalloc", 0),
        function_id=proc_init.id,
        block_id="block:body",
        sloc=_sloc(23),
        op="acquire",
        resource_kind="memory",
        api_name="kmalloc",
        target="p",
        target_vars=("p",),
        matcher="kfree",
    )
    analysis = AnalysisResult(
        function_id=proc_init.id,
        cfg_blocks=[entry_block, body_block, exit_block],
        cfg_edges=cfg_edges,
        branches=[branch],
        assignments=[assignment],
        returns=[returned],
        resource_ops=[resource],
    )
    store = SqliteStore(path)
    with store:
        store.begin()
        store.add_parse_result(
            ParseResult(
                tu=tu,
                functions=[main, proc_init, proc_alloc, proc_free, kmalloc, read_impl],
                calls=calls,
            )
        )
        store.add_analysis_result(analysis)
        store.commit()
        store.finalize()
        # Candidate callback edge + registration facts (as the cross-TU pass writes them).
        store.add_call_targets(
            [
                CallTargetRecord(
                    id=call_target_id(
                        calls[2].id, read_impl.id, "indirect_candidate"
                    ),
                    call_id=calls[2].id,
                    caller_id=proc_init.id,
                    callee_id=read_impl.id,
                    callee_name=read_impl.name,
                    kind="indirect_candidate",
                    confidence=0.62,
                    reason="callback registration g_ops.read",
                    sloc=_sloc(22),
                )
            ]
        )
        store.add_callback_registrations(
            [
                {
                    "object_path": "g_ops.read",
                    "target_function_name": "read_impl",
                    "target_function_id": read_impl.id,
                    "file": FILE,
                    "line": 60,
                    "registration_kind": "struct_assign",
                    "confidence": 0.8,
                    "module": "kernel",
                    "evidence": ["g_ops.read = read_impl;"],
                }
            ]
        )
        store.mark_deep_ready([proc_init.id])
    return {
        "main": main.id,
        "proc_init": proc_init.id,
        "proc_alloc": proc_alloc.id,
        "read_impl": read_impl.id,
    }


def _build_shard_db(path: Path, file: str, function: str, line: int) -> None:
    tu = _tu(file)
    fn = _function(function, line, tu, file=file)
    store = SqliteStore(path)
    with store:
        store.begin()
        store.add_parse_result(ParseResult(tu=tu, functions=[fn]))
        store.commit()
        store.finalize()


def _write_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _source_tu(file: str, repo_root: str) -> CompilationUnit:
    return CompilationUnit(
        id=tu_id(file, repo_root, ["-c", file]),
        file=file,
        directory=repo_root,
        repo_root=repo_root,
        arguments=("-c", file),
        language="c",
    )


def _build_source_db(
    db_path: Path,
    repo_root: str,
    functions: list[FunctionRecord],
    *,
    file: str = "kernel/proc.c",
    analysis: AnalysisResult | None = None,
) -> None:
    tu = _source_tu(file, repo_root)
    store = SqliteStore(db_path)
    with store:
        store.begin()
        store.add_parse_result(ParseResult(tu=tu, functions=functions))
        if analysis is not None:
            store.add_analysis_result(analysis)
        store.commit()
        store.finalize()


def _build_source_repo(root: Path) -> dict[str, object]:
    """A temporary repository whose files back the ``/api/source`` tests."""

    repo = root / "repo"
    lines = [f"kernel/proc.c line {number}" for number in range(1, 521)]
    lines[19] = "int proc_init(void)"
    lines[20] = "{"
    lines[21] = "    return 0;"
    lines[25] = "}"
    _write_lines(repo / "kernel" / "proc.c", lines)

    secret = root / "secret.c"
    secret.write_text("int leaked(void) { return 0; }\n", encoding="utf-8")
    outside = root / "outside.c"
    outside.write_text("int outside(void) { return 1; }\n", encoding="utf-8")
    try:
        (repo / "link.c").symlink_to(secret)
    except OSError:  # pragma: no cover - exotic filesystems only
        pass

    tu = _source_tu("kernel/proc.c", str(repo))
    functions = [
        _function("proc_init", 20, tu, file="kernel/proc.c", end_line=26),
        _function("long_fn", 40, tu, file="kernel/proc.c", end_line=480),
        # Recorded paths that must never escape the repository root.
        _function("escape_fn", 1, tu, file="../secret.c", end_line=2),
        _function("absolute_fn", 1, tu, file=str(outside), end_line=2),
        _function("symlink_fn", 1, tu, file="link.c", end_line=2),
        # Recorded source file that does not exist on disk.
        _function("missing_fn", 5, tu, file="kernel/gone.c", end_line=6),
    ]
    db = root / "source.db"
    _build_source_db(db, str(repo), functions)
    return {"db": db, "repo": repo, "root": root, "outside": outside}


def _build_source_shard(
    db_path: Path, repo: Path, function: str, marker: str
) -> None:
    lines = [f"{marker} line {number}" for number in range(1, 41)]
    lines[9] = f"int {function}(void)"
    lines[10] = "{"
    lines[11] = "    return 0;"
    lines[12] = "}"
    _write_lines(repo / "src" / "a.c", lines)
    tu = _source_tu("src/a.c", str(repo))
    fn = _function(function, 10, tu, file="src/a.c", end_line=12)
    block = CfgBlockRecord(
        id=cfg_block_id(fn.id, 0),
        function_id=fn.id,
        index=0,
        kind="entry",
        terminator="unconditional",
        sloc=_sloc(10, "src/a.c"),
        statement_count=2,
    )
    _build_source_db(
        db_path,
        str(repo),
        [fn],
        file="src/a.c",
        analysis=AnalysisResult(function_id=fn.id, cfg_blocks=[block]),
    )


@pytest.fixture(scope="module")
def source_dataset(tmp_path_factory: pytest.TempPathFactory) -> dict[str, object]:
    root = tmp_path_factory.mktemp("ca-ui-source")
    return _build_source_repo(root)


@pytest.fixture(scope="module")
def source_shards(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("ca-ui-source-shards")
    for shard, function in (("shard_a", "alpha_main"), ("shard_b", "beta_main")):
        repo = directory / f"{shard}_repo"
        _build_source_shard(directory / f"{shard}.db", repo, function, shard)
    return directory


@pytest.fixture()
def source_client(source_dataset: dict[str, object]):
    server, thread, http = _start_server([str(source_dataset["db"])])
    try:
        yield http
    finally:
        _stop_server(server, thread)


@pytest.fixture()
def source_federated_client(source_shards: Path):
    server, thread, http = _start_server(None, str(source_shards))
    try:
        yield http
    finally:
        _stop_server(server, thread)


@pytest.fixture(scope="module")
def dataset(tmp_path_factory: pytest.TempPathFactory) -> dict[str, object]:
    root = tmp_path_factory.mktemp("ca-ui")
    db = root / "ca.db"
    ids = _build_main_db(db)
    return {"db": db, "ids": ids, "root": root}


@pytest.fixture(scope="module")
def shard_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("ca-ui-shards")
    _build_shard_db(directory / "shard_a.db", "a.c", "alpha_main", 3)
    _build_shard_db(directory / "shard_b.db", "b.c", "beta_main", 3)
    return directory


# --------------------------------------------------------------------------- #
# HTTP client / server fixtures
# --------------------------------------------------------------------------- #


class UiClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url

    def request(self, path: str, method: str = "GET"):
        request = urllib.request.Request(f"{self.base_url}{path}", method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read()

    def get(self, path: str):
        status, headers, body = self.request(path)
        return status, headers, body

    def json(self, path: str) -> tuple[int, dict]:
        status, _headers, body = self.request(path)
        return status, json.loads(body.decode("utf-8"))


def _start_server(db_values, db_dir=None) -> tuple[object, threading.Thread, UiClient]:
    from ca.ui.server import make_server

    server = make_server(db_values, db_dir, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, UiClient(server.url.rstrip("/"))


def _stop_server(server, thread: threading.Thread) -> None:
    server.shutdown()
    thread.join(timeout=10)
    server.server_close()


# --------------------------------------------------------------------------- #
# Thin-client proxy (ca ui --serve-url) against a fake ``ca serve`` backend
# --------------------------------------------------------------------------- #


class FakeServeHandler(BaseHTTPRequestHandler):
    """Minimal stand-in for the unified backend's HTTP/SSE surface."""

    server_version = "ca-serve-fake/0.1"
    sys_version = ""

    def log_message(self, *_args) -> None:
        pass

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        path = self.path.split("?", 1)[0]
        if path == "/api/health":
            self._send_json(
                200,
                {
                    "ok": True,
                    "data": {
                        "status": "ok",
                        "server": "ca-serve",
                        "features": {
                            "jobs": True,
                            "sse": True,
                            "mcp": True,
                        },
                    },
                },
            )
        elif path == "/api/search":
            self._send_json(
                200,
                {
                    "ok": True,
                    "data": {"query": "proc", "items": [], "upstream": True},
                },
            )
        elif path == "/api/slow":
            time.sleep(0.5)
            self._send_json(200, {"ok": True, "data": {"slow": True}})
        elif path == "/api/nope":
            self._send_json(
                404,
                {"ok": False, "error": {"code": "NOT_FOUND", "message": "nope"}},
            )
        elif path == "/events":
            self.send_response(200)
            self.send_header(
                "Content-Type", "text/event-stream; charset=utf-8"
            )
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(
                b'id: 1\ndata: {"type":"job.updated","event":"created",'
                b'"job":{"id":"job-1","status":"queued"}}\n\n'
            )
            self.wfile.flush()
            self.wfile.write(b": ping\n\n")
            self.wfile.flush()
            self.wfile.write(
                b'id: 2\ndata: {"type":"job.updated","event":"done",'
                b'"job":{"id":"job-1","status":"done"}}\n\n'
            )
            self.wfile.flush()
        else:
            self._send_json(
                404,
                {"ok": False, "error": {"code": "NOT_FOUND", "message": path}},
            )

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        try:
            parsed = json.loads(body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            parsed = {}
        self.server.received.append((self.path, parsed))  # type: ignore[attr-defined]
        self._send_json(
            200,
            {
                "ok": True,
                "data": {
                    "job": {
                        "id": "job-new",
                        "kind": "ensure_deep",
                        "status": "queued",
                    }
                },
            },
        )


@pytest.fixture()
def fake_serve():
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeServeHandler)
    upstream.received = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{upstream.server_address[1]}", upstream
    finally:
        upstream.shutdown()
        thread.join(timeout=10)
        upstream.server_close()


def _start_proxy(serve_url: str, timeout: float = 2.0):
    from ca.ui.server import make_proxy_server

    server = make_proxy_server(
        serve_url, host="127.0.0.1", port=0, timeout=timeout
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, UiClient(server.url.rstrip("/"))


@pytest.fixture()
def proxy_client(fake_serve):
    server, thread, http = _start_proxy(fake_serve[0])
    try:
        yield http
    finally:
        _stop_server(server, thread)


def _post_json(client: UiClient, path: str, payload: dict):
    request = urllib.request.Request(
        f"{client.base_url}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _read_sse(url: str) -> tuple[str, bytes]:
    with urllib.request.urlopen(url, timeout=10) as response:
        content_type = response.headers.get("Content-Type", "")
        chunks = []
        while True:
            chunk = response.read(4096)
            if not chunk:
                break
            chunks.append(chunk)
        return content_type, b"".join(chunks)


def _closed_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.fixture()
def client(dataset: dict[str, object]):
    server, thread, http = _start_server([str(dataset["db"])])
    try:
        yield http
    finally:
        _stop_server(server, thread)


@pytest.fixture()
def federated_client(shard_dir: Path):
    server, thread, http = _start_server(None, str(shard_dir))
    try:
        yield http
    finally:
        _stop_server(server, thread)


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #


def test_health_reports_database(client: UiClient) -> None:
    status, payload = client.json("/api/health")
    assert status == 200
    assert payload["ok"] is True
    data = payload["data"]
    assert data["status"] == "ok"
    assert data["db_ok"] is True
    assert data["server"] == "ca-ui"
    assert data["database"]["federated"] is False
    assert data["database"]["counts"]["functions"] >= 4
    assert data["database"]["db_bytes"] > 0
    assert data["database"]["schema_version"]
    assert data["uptime_seconds"] >= 0

    # /health is an alias for probes.
    status, alias = client.json("/health")
    assert status == 200
    assert alias["data"]["db_ok"] is True


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #


def test_search_exact_and_substring(client: UiClient) -> None:
    status, payload = client.json("/api/search?q=proc_init")
    assert status == 200 and payload["ok"] is True
    items = payload["data"]["items"]
    assert [row["name"] for row in items] == ["proc_init"]
    assert payload["data"]["count"] == 1
    # Single --db keeps the exact old contract: no shard column.
    assert "shard" not in items[0]
    assert payload["data"]["federated"] is False

    status, payload = client.json("/api/search?q=proc")
    names = {row["name"] for row in payload["data"]["items"]}
    assert {"proc_init", "proc_alloc", "proc_free"} <= names

    # Pagination happens after the store result set.
    _status, payload = client.json("/api/search?q=proc&limit=1&offset=1")
    assert payload["data"]["count"] == 1

    # Short prefixes still work through the LIKE fallback.
    status, payload = client.json("/api/search?q=proc_")
    assert status == 200
    assert len(payload["data"]["items"]) == 3


def test_search_requires_query(client: UiClient) -> None:
    status, payload = client.json("/api/search")
    assert status == 400
    assert payload["ok"] is False
    assert payload["error"]["code"] == "INVALID_ARGUMENT"
    assert "q" in payload["error"]["message"]


# --------------------------------------------------------------------------- #
# Function detail
# --------------------------------------------------------------------------- #


def test_function_detail_by_name_and_id(client: UiClient, dataset: dict[str, object]) -> None:
    status, payload = client.json("/api/function/proc_init")
    assert status == 200 and payload["ok"] is True
    data = payload["data"]
    function = data["function"]
    assert function["name"] == "proc_init"
    assert function["file"] == FILE
    assert function["line"] == 20
    assert function["signature"] == "int proc_init(void)"
    assert function["is_definition"] is True
    assert data["deep_ready"] is True
    assert len(data["branches"]) == 1
    assert data["branches"][0]["condition"] == "flags & PROC_READY"
    assert len(data["assignments"]) == 1
    assert data["assignments"][0]["is_decl_init"] is True
    assert len(data["returns"]) == 1
    assert data["resource_ops"][0]["api_name"] == "kmalloc"
    assert data["findings"] == []

    # Stable function ids are accepted too (URL-encoded).
    function_id_value = str(dataset["ids"]["proc_init"])
    status, payload = client.json(f"/api/function/{quote(function_id_value, safe='')}")
    assert status == 200
    assert payload["data"]["function"]["id"] == function_id_value

    # Query-string form used by the detail API.
    status, payload = client.json("/api/function?symbol=proc_alloc")
    assert status == 200
    assert payload["data"]["function"]["name"] == "proc_alloc"


def test_function_detail_not_found(client: UiClient) -> None:
    status, payload = client.json("/api/function/no_such_symbol_xyz")
    assert status == 404
    assert payload["ok"] is False
    assert payload["error"]["code"] == "NOT_FOUND"


# --------------------------------------------------------------------------- #
# Callers / callees / graph
# --------------------------------------------------------------------------- #


def test_callers_and_callees_merge_candidates(client: UiClient) -> None:
    status, payload = client.json("/api/callers?symbol=proc_init")
    assert status == 200
    callers = payload["data"]["items"]
    assert [row["caller_name"] for row in callers] == ["main"]
    assert callers[0]["source"] == "call"
    assert callers[0]["is_resolved"] is True

    status, payload = client.json("/api/callees?symbol=proc_init")
    assert status == 200
    callees = {row["callee_name"]: row for row in payload["data"]["items"]}
    assert {"kmalloc", "proc_free", "read_impl"} <= set(callees)
    assert callees["kmalloc"]["source"] == "call"
    candidate = callees["read_impl"]
    assert candidate["candidate"] is True
    assert candidate["source"] == "call_target"
    assert candidate["confidence"] == pytest.approx(0.62)

    status, payload = client.json("/api/callees?symbol=main")
    assert {"proc_init", "proc_alloc"} <= {
        row["callee_name"] for row in payload["data"]["items"]
    }


def test_graph_endpoint_has_direct_and_candidate_edges(client: UiClient) -> None:
    status, payload = client.json(
        "/api/graph?symbol=proc_init&direction=forward&depth=1"
    )
    assert status == 200 and payload["ok"] is True
    data = payload["data"]
    assert data["found"] is True
    assert data["root"] == "proc_init"
    names = {node["name"] for node in data["nodes"]}
    assert {"proc_init", "kmalloc", "read_impl"} <= names
    edges = {(edge["caller_name"], edge["callee_name"]): edge for edge in data["edges"]}
    assert ("proc_init", "kmalloc") in edges
    assert edges[("proc_init", "kmalloc")]["candidate"] is False
    candidate = edges[("proc_init", "read_impl")]
    assert candidate["candidate"] is True
    assert candidate["confidence"] == pytest.approx(0.62)
    assert data["stats"]["nodes"] == len(data["nodes"])

    status, payload = client.json("/api/graph?symbol=main&depth=2&direction=forward")
    assert status == 200
    names = {node["name"] for node in payload["data"]["nodes"]}
    assert "kmalloc" in names

    # Reverse traversal exposes callers (main is a second hop away).
    status, payload = client.json(
        "/api/graph?symbol=kmalloc&direction=reverse&depth=2"
    )
    assert {"main", "proc_init"} <= {
        node["name"] for node in payload["data"]["nodes"]
    }


def test_graph_errors(client: UiClient) -> None:
    status, payload = client.json("/api/graph?symbol=main&direction=sideways")
    assert status == 400
    assert payload["ok"] is False
    assert payload["error"]["code"] == "INVALID_ARGUMENT"

    status, payload = client.json("/api/graph")
    assert status == 400
    assert payload["error"]["code"] == "INVALID_ARGUMENT"

    status, payload = client.json("/api/graph?symbol=no_such_symbol_xyz")
    assert status == 200
    assert payload["data"]["found"] is False
    assert payload["data"]["nodes"] == []


# --------------------------------------------------------------------------- #
# Stats / callbacks / facts
# --------------------------------------------------------------------------- #


def test_stats_endpoint(client: UiClient) -> None:
    status, payload = client.json("/api/stats")
    assert status == 200 and payload["ok"] is True
    data = payload["data"]
    tables = data["tables"]
    assert tables["functions"] >= 4
    assert tables["calls"] >= 4
    assert tables["branches"] >= 1
    assert tables["call_targets"] == 1
    assert tables["callback_registrations"] == 1
    assert data["db_bytes"] > 0
    assert data["schema_version"]
    assert data["callbacks"]["registrations"] == 1
    assert data["federated"] is False
    assert data["database"]["federated"] is False


def test_callbacks_endpoint(client: UiClient) -> None:
    status, payload = client.json("/api/callbacks")
    assert status == 200 and payload["ok"] is True
    rows = payload["data"]["items"]
    assert len(rows) == 1
    row = rows[0]
    assert row["object_path"] == "g_ops.read"
    assert row["object_field"] == "read"
    assert row["target_function_name"] == "read_impl"
    assert row["registration_kind"] == "struct_assign"
    assert row["module"] == "kernel"
    assert row["evidence"] == ["g_ops.read = read_impl;"]

    _status, payload = client.json("/api/callbacks?path=g_ops")
    assert payload["data"]["count"] == 1
    _status, payload = client.json("/api/callbacks?field=read")
    assert payload["data"]["count"] == 1
    _status, payload = client.json("/api/callbacks?module=kernel")
    assert payload["data"]["count"] == 1
    _status, payload = client.json("/api/callbacks?module=fs")
    assert payload["data"]["count"] == 0


def test_fact_endpoints(client: UiClient) -> None:
    for endpoint, expected in (
        ("/api/branches?symbol=proc_init", 1),
        ("/api/assignments?symbol=proc_init", 1),
        ("/api/returns?symbol=proc_init", 1),
        ("/api/resources?symbol=proc_init", 1),
    ):
        status, payload = client.json(endpoint)
        assert status == 200, endpoint
        assert payload["data"]["count"] == expected, endpoint


# --------------------------------------------------------------------------- #
# Static frontend
# --------------------------------------------------------------------------- #


def test_ai_explanation_removed_from_static_assets() -> None:
    """The Web UI no longer shows or calls the removed AI explanation feature."""

    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    i18n = (STATIC_DIR / "i18n.js").read_text(encoding="utf-8")
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    style = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
    assert "explain" not in script.lower()
    assert "/api/explain" not in script
    assert "EventSource" not in script
    assert "tab.explain" not in i18n
    assert "explain." not in i18n
    assert "error.timeout" not in i18n
    assert "#explain-" not in html
    assert ".explain" not in style


def test_static_single_page_has_no_external_assets(client: UiClient) -> None:
    status, headers, body = client.get("/")
    assert status == 200
    assert "text/html" in headers.get("Content-Type", "")
    html = body.decode("utf-8")
    assert "<title>CA source explorer</title>" in html
    assert "/app.js" in html and "/style.css" in html and "/i18n.js" in html
    # v7: search results live in a floating dropdown, not a left results panel.
    assert 'id="search-dropdown"' in html
    assert 'id="conn-status"' in html
    assert 'id="results"' in html and 'id="results-count"' in html
    assert '<aside class="sidebar"' not in html
    assert "https://" not in html and "http://" not in html
    assert "cdn" not in html.lower()
    # Localizer and syntax highlighter are loaded before the application script.
    assert html.index("/i18n.js") < html.index("/highlight.js")
    assert html.index("/highlight.js") < html.index("/app.js")

    status, headers, body = client.get("/app.js")
    assert status == 200
    assert "javascript" in headers.get("Content-Type", "")
    script = body.decode("utf-8")
    assert "/api/health" in script
    assert "/api/source?symbol=" in script
    assert "/api/cfg?symbol=" in script
    # Fixed top source + persistent middle graph + right panel for tab/node info.
    assert "fn-source" in script
    assert "fn-graph" in script
    assert "fn-panel" in script
    # v7: inline accordion sidebar, node-source-only right panel.
    assert "renderAccordionContent" in script
    assert "data-section-toggle" in script
    assert "accordion-body" in script
    assert "search-dropdown" in script
    assert "source.nodeHint" in script
    # Remote mode: thin client + connection badge (AI explain was removed).
    assert "/api/ui" in script
    assert "loadUiMode" in script
    assert "retryConnection" in script
    assert "/api/explain" not in script
    assert "tab.explain" not in script
    assert "renderExplainSection" not in script
    assert "EventSource" not in script
    # Duplicated callers/callees tabs and the AI explanation tab are gone.
    assert '["callers"' not in script
    assert '["callees"' not in script
    # v8: draggable three-area layout persisted in localStorage.
    assert "ca.ui.layout" in script
    assert "layout-reset" in script
    assert "toggle-left" in script and "toggle-right" in script
    assert "beginSplitterDrag" in script
    assert "nudgeSplitter" in script
    assert 'role="separator"' in script
    assert "aria-orientation" in script
    for key in ("ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"):
        assert key in script, key
    for marker in ("32vh", "22%", "26%", "localStorage"):
        assert marker in script, marker
    assert "highlightLines" in script
    assert "graph-svg" in script
    # Click/double-click graph nodes: delayed single-click source + dblclick detail.
    assert "onViewDoubleClick" in script
    assert "nodeClickTimer" in script
    assert "setTimeout" in script
    assert "250" in script
    # Top source highlights the selected node's lines (or shows a location hint).
    assert "selected-node" in script
    assert "source.nodeOutside" in script
    assert "legendVirtual" in script and "legendConfidence" in script
    assert "arrow-virtual" in script and "graph-edge" in script
    assert "source.callSite" in script and "source.callSiteOutside" in script
    assert "source.callerCallsCurrent" in script
    assert "callSiteForNode" in script
    assert "callRange" in script
    assert "renderTopSource" in script
    # Function jump history + back navigation.
    assert "sessionStorage" in script
    assert "ca.ui.fnHistory" in script
    assert "fn-back" in script
    assert "data-jump" in script
    assert "ArrowLeft" in script
    assert "nav.backToPrev" in script
    # Browser-native Back: function views use pushState hash entries.
    assert "pushState" in script
    external = [
        url
        for url in re.findall(r"https?://[^\s\"']+", script)
        if "w3.org" not in url  # SVG namespace, not a fetch
    ]
    assert external == []

    status, headers, body = client.get("/style.css")
    assert status == 200
    assert "text/css" in headers.get("Content-Type", "")
    css = body.decode("utf-8")
    assert "@import" not in css
    assert "url(http" not in css
    # v8: full-viewport shell without max-width containers + internal splitters.
    assert "100vh" in css
    assert "max-width: 1560" not in css
    assert ".splitter" in css
    assert "--left-w" in css

    status, headers, body = client.get("/favicon.svg")
    assert status == 200
    assert "image/svg+xml" in headers.get("Content-Type", "")

    status, headers, _body = client.get("/")
    assert headers.get("Content-Security-Policy", "").startswith("default-src 'self'")


# --------------------------------------------------------------------------- #
# Localization (i18n)
# --------------------------------------------------------------------------- #

STATIC_DIR = Path(__file__).resolve().parent.parent / "src" / "ca" / "ui" / "static"


def _i18n_block(source: str, variable: str) -> str:
    match = re.search(
        rf"const {variable} = \{{(.*?)\n  \}};", source, re.S
    )
    assert match, f"{variable} dictionary not found in i18n.js"
    return match.group(1)


def _i18n_keys(source: str, variable: str) -> set[str]:
    return set(re.findall(r'"([A-Za-z][\w.]*)":', _i18n_block(source, variable)))


def _i18n_values(source: str, variable: str) -> dict[str, str]:
    return dict(
        re.findall(
            r'"([A-Za-z][\w.]*)": "((?:[^"\\]|\\.)*)"',
            _i18n_block(source, variable),
        )
    )


def test_i18n_asset_is_served(client: UiClient) -> None:
    status, headers, body = client.get("/i18n.js")
    assert status == 200
    assert "javascript" in headers.get("Content-Type", "")
    script = body.decode("utf-8")
    for marker in (
        "CA_I18N",
        "localStorage",
        "navigator.languages",
        "document.documentElement.lang",
        "MutationObserver",
        "ca:languagechange",
        "data-i18n",
    ):
        assert marker in script, marker
    external = [
        url
        for url in re.findall(r"https?://[^\s\"']+", script)
        if "w3.org" not in url
    ]
    assert external == []


def test_i18n_dictionary_is_bilingual_and_covers_ui() -> None:
    i18n_source = (STATIC_DIR / "i18n.js").read_text(encoding="utf-8")
    index_source = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    app_source = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    en_keys = _i18n_keys(i18n_source, "en")
    zh_keys = _i18n_keys(i18n_source, "zh")
    assert en_keys == zh_keys, "English and Chinese dictionaries must share keys"
    assert len(en_keys) >= 120, f"expected a complete UI dictionary, got {len(en_keys)}"

    used = set(
        re.findall(
            r'data-i18n(?:-placeholder|-title|-aria-label)?="([A-Za-z][\w.]*)"',
            index_source,
        )
    )
    used |= set(re.findall(r'\bt\("([A-Za-z][\w.]*)"', app_source))
    missing = sorted(used - en_keys)
    assert missing == [], f"UI text keys missing translations: {missing}"

    en = _i18n_values(i18n_source, "en")
    zh = _i18n_values(i18n_source, "zh")
    assert en["search.button"] == "Search"
    assert zh["search.button"] == "搜索"
    assert zh["nav.results"] == "结果"
    assert zh["tab.callers"] == "调用者"
    assert zh["stats.title"] == "数据库统计"
    assert "read-only" in en["footer.text"]
    assert "只读" in zh["footer.text"]
    assert "无法加载" in zh["error.loadFunction"]
    for key in ("status.searching", "common.functionCount", "note.edgeCount"):
        assert "{" in en[key] and "{" in zh[key], key
    # Technical identifiers (USR, API, SQLite) stay identical across languages.
    assert "USR" in en["field.usr"] and "USR" in zh["field.usr"]


def test_language_switch_wiring() -> None:
    index_source = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    i18n_source = (STATIC_DIR / "i18n.js").read_text(encoding="utf-8")
    assert 'data-lang="zh"' in index_source
    assert 'data-lang="en"' in index_source
    for marker in (
        'STORAGE_KEY = "ca.ui.lang"',
        'get("lang")',
        'startsWith("zh")',
        "ca:languagechange",
        "localStorage.setItem",
        "MutationObserver",
    ):
        assert marker in i18n_source, marker


def test_api_is_not_localized(client: UiClient) -> None:
    request = urllib.request.Request(
        f"{client.base_url}/api/search?q=proc_init",
        headers={"Accept-Language": "zh-CN,zh;q=0.9"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        localized = json.loads(response.read().decode("utf-8"))
    status, plain = client.json("/api/search?q=proc_init")
    assert status == 200
    assert localized == plain


def test_unknown_paths_and_api_errors(client: UiClient) -> None:
    status, _headers, body = client.get("/not-a-file")
    assert status == 404
    assert b"NOT_FOUND" in body

    status, payload = client.json("/api/not-an-endpoint")
    assert status == 404
    assert payload["ok"] is False
    assert payload["error"]["code"] == "NOT_FOUND"


# --------------------------------------------------------------------------- #
# Read-only guarantees
# --------------------------------------------------------------------------- #


def test_write_methods_are_rejected_and_db_is_untouched(
    client: UiClient, dataset: dict[str, object]
) -> None:
    db_path = Path(str(dataset["db"]))
    before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    for path in ("/api/search?q=main", "/api/stats", "/api/function/main"):
        status, _headers, body = client.request(path, method="POST")
        assert status == 405
        payload = json.loads(body.decode("utf-8"))
        assert payload["ok"] is False
        assert payload["error"]["code"] == "READ_ONLY"
        assert client.request(path, method="DELETE")[0] == 405
    # Read-only connections never write: the database bytes stay identical.
    for path in ("/api/health", "/api/search?q=proc", "/api/stats", "/api/callbacks"):
        assert client.json(path)[0] == 200
    after = hashlib.sha256(db_path.read_bytes()).hexdigest()
    assert before == after


def test_read_only_connection_rejects_writes(dataset: dict[str, object]) -> None:
    from ca.ui.server import _connect_read_only

    connection = _connect_read_only(str(dataset["db"]))
    try:
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM functions")
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("INSERT INTO meta (key, value) VALUES ('x', 'y')")
        assert connection.execute("SELECT COUNT(*) FROM functions").fetchone()[0] >= 4
    finally:
        connection.close()


# --------------------------------------------------------------------------- #
# Federated shards
# --------------------------------------------------------------------------- #


def test_federated_ui_shows_shard_provenance(federated_client: UiClient) -> None:
    status, payload = federated_client.json("/api/health")
    assert status == 200
    data = payload["data"]
    assert data["database"]["federated"] is True
    assert data["database"]["shards"] == ["shard_a", "shard_b"]

    _status, payload = federated_client.json("/api/search?q=main")
    rows = payload["data"]["items"]
    assert {row["name"] for row in rows} == {"alpha_main", "beta_main"}
    assert {row["shard"] for row in rows} == {"shard_a", "shard_b"}
    assert payload["data"]["federated"] is True

    _status, payload = federated_client.json("/api/function/alpha_main")
    data = payload["data"]
    assert data["federated"] is True
    assert data["shard"] == "shard_a"
    assert data["function"]["shard"] == "shard_a"

    _status, payload = federated_client.json("/api/stats")
    data = payload["data"]
    assert data["federated"] is True
    assert data["shard_count"] == 2
    assert {entry["shard"] for entry in data["shards"]} == {"shard_a", "shard_b"}
    assert data["tables"]["functions"] >= 2


def test_federated_explicit_db_list_and_skipped_shards(
    shard_dir: Path, tmp_path: Path
) -> None:
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_text("this is not a sqlite database", encoding="utf-8")
    # Explicit --db list (repeatable) behaves like --db-dir.
    server, thread, http = _start_server(
        [str(shard_dir / "shard_a.db"), str(shard_dir / "shard_b.db")]
    )
    try:
        _status, payload = http.json("/api/search?q=alpha")
        assert payload["data"]["items"][0]["shard"] == "shard_a"
    finally:
        _stop_server(server, thread)

    # A directory selector skips unreadable shards and reports them in health.
    directory = tmp_path / "shards-with-corrupt"
    directory.mkdir()
    for name in ("shard_a.db", "shard_b.db"):
        (directory / name).write_bytes((shard_dir / name).read_bytes())
    (directory / "corrupt.db").write_text("broken", encoding="utf-8")
    server, thread, http = _start_server(None, str(directory))
    try:
        _status, payload = http.json("/api/search?q=main")
        assert payload["data"]["federated"] is True
        assert len(payload["data"]["items"]) == 2
        _status, payload = http.json("/api/health")
        skipped = payload["data"]["database"]["skipped"]
        assert any("corrupt.db" in str(item.get("path", "")) for item in skipped)
        assert payload["data"]["db_ok"] is True
    finally:
        _stop_server(server, thread)


# --------------------------------------------------------------------------- #
# Source viewing (graph side panel)
# --------------------------------------------------------------------------- #


def test_source_api_returns_function_window(
    source_client: UiClient, source_dataset: dict[str, object]
) -> None:
    status, payload = source_client.json("/api/source?symbol=proc_init&context=2")
    assert status == 200 and payload["ok"] is True
    data = payload["data"]
    function = data["function"]
    assert function["name"] == "proc_init"
    assert function["line"] == 20
    assert function["end_line"] == 26
    assert data["file"] == "kernel/proc.c"
    assert data["context"] == 2
    assert data["start"] == 18
    assert data["end"] == 28
    assert data["total_lines"] == 520
    assert (data["function_start"], data["function_end"]) == (20, 26)
    assert data["truncated"] is False
    assert data["federated"] is False
    assert data["shard"] is None
    assert data["language"] == "c"
    assert len(data["lines"]) == 11
    assert data["lines"][2] == {"line": 20, "text": "int proc_init(void)"}
    assert [row["line"] for row in data["lines"]] == list(range(18, 29))
    # The resolved real path must stay inside the indexed repository root.
    repo_root = Path(str(source_dataset["repo"])).resolve()
    resolved = Path(data["path"]).resolve()
    assert resolved.relative_to(repo_root) == Path("kernel/proc.c")


def test_source_api_defaults_and_clamps_context(source_client: UiClient) -> None:
    # Default context is 3 lines around the function.
    _status, payload = source_client.json("/api/source?symbol=proc_init")
    assert (payload["data"]["start"], payload["data"]["end"]) == (17, 29)
    assert payload["data"]["context"] == 3

    # context is clamped to 0..10 instead of erroring on out-of-range values.
    _status, payload = source_client.json("/api/source?symbol=proc_init&context=99")
    assert payload["data"]["context"] == 10
    assert payload["data"]["start"] == 10
    assert payload["data"]["end"] == 36
    _status, payload = source_client.json("/api/source?symbol=proc_init&context=-5")
    assert payload["data"]["context"] == 0
    assert (payload["data"]["start"], payload["data"]["end"]) == (20, 26)
    _status, payload = source_client.json("/api/source?symbol=proc_init&context=0")
    assert (payload["data"]["start"], payload["data"]["end"]) == (20, 26)

    # A non-integer context is a normal argument error.
    status, payload = source_client.json("/api/source?symbol=proc_init&context=abc")
    assert status == 400
    assert payload["error"]["code"] == "INVALID_ARGUMENT"


def test_source_api_caps_response_at_400_lines(source_client: UiClient) -> None:
    status, payload = source_client.json("/api/source?symbol=long_fn&context=10")
    assert status == 200
    data = payload["data"]
    assert data["start"] == 30
    assert data["end"] == 429
    assert len(data["lines"]) == 400
    assert data["truncated"] is True


def test_source_api_rejects_escapes_and_missing_files(
    source_client: UiClient, source_dataset: dict[str, object]
) -> None:
    # The outside files exist, so a 404 proves the path guard (not a missing file).
    assert Path(str(source_dataset["outside"])).is_file()
    for symbol in ("escape_fn", "absolute_fn", "symlink_fn", "missing_fn"):
        status, payload = source_client.json(f"/api/source?symbol={symbol}")
        assert status == 404, symbol
        assert payload["ok"] is False
        assert payload["error"]["code"] == "NOT_FOUND", symbol

    # Unknown function / missing symbol.
    status, payload = source_client.json("/api/source?symbol=no_such_symbol")
    assert status == 404
    assert payload["error"]["code"] == "NOT_FOUND"
    status, payload = source_client.json("/api/source")
    assert status == 400
    assert payload["error"]["code"] == "INVALID_ARGUMENT"


def test_source_api_federated_uses_owning_shard(
    source_federated_client: UiClient,
) -> None:
    _status, payload = source_federated_client.json("/api/source?symbol=alpha_main")
    data = payload["data"]
    assert data["federated"] is True
    assert data["shard"] == "shard_a"
    text = "\n".join(row["text"] for row in data["lines"])
    assert "int alpha_main(void)" in text
    assert "shard_a line" in text

    _status, payload = source_federated_client.json("/api/source?symbol=beta_main")
    data = payload["data"]
    assert data["shard"] == "shard_b"
    text = "\n".join(row["text"] for row in data["lines"])
    assert "int beta_main(void)" in text

    status, payload = source_federated_client.json("/api/source?symbol=no_such_symbol")
    assert status == 404


# --------------------------------------------------------------------------- #
# Control-flow graph API (branches tab)
# --------------------------------------------------------------------------- #


def test_cfg_api_returns_blocks_edges_and_branch_annotations(
    client: UiClient,
) -> None:
    status, payload = client.json("/api/cfg?symbol=proc_init")
    assert status == 200 and payload["ok"] is True
    data = payload["data"]
    assert data["symbol"] == "proc_init"
    assert data["function"]["name"] == "proc_init"
    assert data["function"]["file"] == FILE
    assert data["deep_ready"] is True
    assert data["federated"] is False
    assert data["shard"] is None
    assert data["truncated"] is False
    assert data["stats"] == {"blocks": 3, "edges": 3, "branches": 1}

    blocks = {block["id"]: block for block in data["blocks"]}
    entry = next(block for block in data["blocks"] if block["is_entry"])
    assert entry["kind"] == "entry"
    assert entry["condition"] == "flags & PROC_READY"
    assert entry["branch_kind"] == "if"
    assert entry["is_exit"] is False
    assert any(block["is_exit"] for block in data["blocks"])
    assert all(block["id"] in blocks for block in data["blocks"])
    assert data["branches"][0]["condition"] == "flags & PROC_READY"

    labels = {edge["label"] for edge in data["edges"]}
    assert {"true", "false"} <= labels
    branch_edges = {
        edge["dst"]: edge["label"]
        for edge in data["edges"]
        if edge["src"] == entry["id"]
    }
    assert branch_edges[entry["true_block"]] == "true"
    assert branch_edges[entry["false_block"]] == "false"


def test_cfg_api_without_deep_facts(client: UiClient) -> None:
    status, payload = client.json("/api/cfg?symbol=proc_alloc")
    assert status == 200
    data = payload["data"]
    assert data["blocks"] == []
    assert data["edges"] == []
    assert data["branches"] == []
    assert data["deep_ready"] is False
    assert data["stats"] == {"blocks": 0, "edges": 0, "branches": 0}


def test_cfg_api_bad_symbol(client: UiClient) -> None:
    status, payload = client.json("/api/cfg?symbol=no_such_symbol_xyz")
    assert status == 404
    assert payload["ok"] is False
    assert payload["error"]["code"] == "NOT_FOUND"
    status, payload = client.json("/api/cfg")
    assert status == 400
    assert payload["error"]["code"] == "INVALID_ARGUMENT"


def test_cfg_api_federated_uses_owning_shard(
    source_federated_client: UiClient,
) -> None:
    _status, payload = source_federated_client.json("/api/cfg?symbol=alpha_main")
    data = payload["data"]
    assert data["federated"] is True
    assert data["shard"] == "shard_a"
    assert data["stats"]["blocks"] == 1
    assert data["blocks"][0]["is_entry"] is True

    _status, payload = source_federated_client.json("/api/cfg?symbol=beta_main")
    assert payload["data"]["shard"] == "shard_b"
    assert payload["data"]["blocks"]


# --------------------------------------------------------------------------- #
# Local syntax highlighting (no CDN / no external requests)
# --------------------------------------------------------------------------- #


def test_highlight_asset_is_local_and_served(client: UiClient) -> None:
    status, headers, body = client.get("/highlight.js")
    assert status == 200
    assert "javascript" in headers.get("Content-Type", "")
    script = body.decode("utf-8")
    for marker in (
        "CA_HIGHLIGHT",
        "highlightLines",
        "tok-keyword",
        "tok-type",
        "tok-comment",
        "tok-string",
        "tok-number",
        "tok-preproc",
        "escapeHtml",
    ):
        assert marker in script, marker
    external = [
        url
        for url in re.findall(r"https?://[^\s\"']+", script)
        if "w3.org" not in url
    ]
    assert external == []


def test_highlight_tokenizer_output() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available to execute the local highlighter")
    script = """
const path = %s;
global.window = {};
require(path);
const highlighter = window.CA_HIGHLIGHT;
const lines = [
  "#include <stdio.h>",
  "/* multi-line",
  "   comment */ int main(void) {",
  "  // line comment",
  '  const char *s = "a<b";',
  "  return 0x2A;",
  "}",
];
const html = highlighter.highlightLines(lines);
console.log(JSON.stringify(html));
"""
    script = script % json.dumps(str(STATIC_DIR / "highlight.js"))
    result = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    html = json.loads(result.stdout)
    assert len(html) == 7
    assert 'class="tok-preproc"' in html[0]
    assert "&lt;stdio.h&gt;" in html[0]  # escaped, not raw HTML
    assert 'class="tok-comment"' in html[1]
    # The block comment continues onto the next rendered line and then ends.
    assert 'class="tok-comment"' in html[2]
    assert 'class="tok-type"' in html[2]  # int
    assert 'class="tok-comment"' in html[3]
    assert 'class="tok-string"' in html[4]
    assert 'class="tok-keyword"' in html[4]  # const
    assert "a&lt;b" in html[4]
    assert 'class="tok-number"' in html[5]
    assert 'class="tok-keyword"' in html[5]  # return
    assert all("<script" not in line for line in html)




def test_function_jump_history_stack() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available to execute the UI history hooks")
    script = """
const appPath = %s;
const storage = new Map();
const session = {
  getItem: (key) => (storage.has(key) ? storage.get(key) : null),
  setItem: (key, value) => storage.set(key, String(value)),
};
global.window = {
  sessionStorage: session,
  localStorage: session,
  location: { hash: "", search: "" },
  history: { replaceState() {} },
  addEventListener() {},
  setTimeout: () => 0,
  clearTimeout() {},
};
global.document = {
  readyState: "loading",
  addEventListener() {},
  querySelectorAll: () => [],
  documentElement: { lang: "en" },
  title: "",
  body: {},
};
Object.defineProperty(globalThis, "navigator", {
  value: { languages: ["en"], language: "en" },
  configurable: true,
});
window.CA_I18N = { t: (key) => key };
require(appPath);
const hooks = window.CA_UI_TEST;
storage.set(
  "ca.ui.fnHistory",
  JSON.stringify([
    { symbol: "fn:a", tab: "callers" },
    { symbol: "fn:b", tab: "branches" },
  ])
);
// Simulate a page (re)load reading the persisted session stack.
delete require.cache[require.resolve(appPath)];
storage.forEach(() => {});
require(appPath);
const loaded = window.CA_UI_TEST;
const result = {
  loadedLength: loaded.historyLength(),
  loadedEntries: loaded.historyEntries(),
};
const popped = loaded.popFunctionHistory();
result.popped = popped;
result.afterPop = loaded.historyLength();
loaded.pushFunctionHistory({ symbol: "fn:a", tab: "overview" });
result.dedupedLength = loaded.historyLength();
result.dedupedTop = loaded.historyEntries().slice(-1)[0];
for (let index = 0; index < 60; index += 1) {
  loaded.pushFunctionHistory({ symbol: "fn:" + index, tab: "overview" });
}
result.cappedLength = loaded.historyLength();
result.cappedFirst = loaded.historyEntries()[0].symbol;
result.cappedLast = loaded.historyEntries().slice(-1)[0].symbol;
loaded.clearFunctionHistory();
result.clearedLength = loaded.historyLength();
result.hooks = typeof hooks.historyLength === "function";
console.log(JSON.stringify(result));
"""
    script = script % json.dumps(str(STATIC_DIR / "app.js"))
    result = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["hooks"] is True
    # sessionStorage persistence is read on module load
    assert data["loadedLength"] == 2, data
    assert data["loadedEntries"] == [
        {"symbol": "fn:a", "tab": "callers"},
        {"symbol": "fn:b", "tab": "branches"},
    ]
    # pop returns the most recent entry and shrinks the stack
    assert data["popped"] == {"symbol": "fn:b", "tab": "branches"}
    assert data["afterPop"] == 1
    # pushing the current top dedupes but updates its tab
    assert data["dedupedLength"] == 1
    assert data["dedupedTop"] == {"symbol": "fn:a", "tab": "overview"}
    # bounded stack: newest 50 kept
    assert data["cappedLength"] == 50
    assert data["cappedFirst"] == "fn:10"
    assert data["cappedLast"] == "fn:59"
    assert data["clearedLength"] == 0


# --------------------------------------------------------------------------- #
# Thin-client proxy behaviour (P1)
# --------------------------------------------------------------------------- #


def test_embedded_mode_advertises_itself(client: UiClient) -> None:
    status, payload = client.json("/api/ui")
    assert status == 200
    data = payload["data"]
    assert data["mode"] == "embedded"
    assert data["serve_url"] == ""
    assert data["features"]["jobs"] is False


def test_proxy_advertises_remote_mode(
    proxy_client: UiClient, fake_serve
) -> None:
    status, payload = proxy_client.json("/api/ui")
    assert status == 200
    data = payload["data"]
    assert data["mode"] == "remote"
    assert data["serve_url"] == fake_serve[0]
    assert data["features"]["jobs"] is True
    assert data["features"]["sse"] is True


def test_proxy_forwards_get_with_query_string(proxy_client: UiClient) -> None:
    status, payload = proxy_client.json("/api/health")
    assert status == 200
    assert payload["data"]["server"] == "ca-serve"
    status, payload = proxy_client.json("/api/search?q=proc&limit=3")
    assert status == 200
    assert payload["data"]["upstream"] is True


def test_proxy_forwards_job_post_and_rejects_other_posts(
    proxy_client: UiClient, fake_serve
) -> None:
    body = {"type": "ensure_deep", "symbol": "main", "payload": {}}
    status, payload = _post_json(proxy_client, "/api/jobs", body)
    assert status == 200
    assert payload["data"]["job"]["id"] == "job-new"
    received = fake_serve[1].received  # type: ignore[attr-defined]
    assert received == [("/api/jobs", body)]

    status, payload = _post_json(proxy_client, "/api/search", {"q": "x"})
    assert status == 405
    assert payload["error"]["code"] == "READ_ONLY"




def test_proxy_streams_sse_chunks(proxy_client: UiClient) -> None:
    content_type, body = _read_sse(f"{proxy_client.base_url}/events")
    assert "text/event-stream" in content_type
    text = body.decode("utf-8")
    assert 'data: {"type":"job.updated","event":"created"' in text
    assert 'data: {"type":"job.updated","event":"done"' in text
    assert ": ping" in text


def test_proxy_passes_through_upstream_errors(proxy_client: UiClient) -> None:
    status, payload = proxy_client.json("/api/nope")
    assert status == 404
    assert payload["error"]["code"] == "NOT_FOUND"


def test_proxy_returns_502_when_backend_is_down() -> None:
    server, thread, http = _start_proxy(
        f"http://127.0.0.1:{_closed_port()}", timeout=1.0
    )
    try:
        status, payload = http.json("/api/health")
        assert status == 502
        assert payload["error"]["code"] == "SERVE_UNAVAILABLE"
        status, _headers, body = http.request("/events")
        assert status == 502
        assert json.loads(body.decode("utf-8"))["error"]["code"] == (
            "SERVE_UNAVAILABLE"
        )
    finally:
        _stop_server(server, thread)


def test_proxy_query_timeout_returns_502(fake_serve) -> None:
    server, thread, http = _start_proxy(fake_serve[0], timeout=0.15)
    try:
        status, payload = http.json("/api/slow")
        assert status == 502
        assert payload["error"]["code"] == "SERVE_UNAVAILABLE"
    finally:
        _stop_server(server, thread)


def test_proxy_serves_static_assets(proxy_client: UiClient) -> None:
    status, headers, body = proxy_client.get("/")
    assert status == 200
    assert "text/html" in headers.get("Content-Type", "")
    assert b"CA source explorer" in body
    status, _headers, body = proxy_client.get("/app.js")
    assert status == 200
    assert b"/api/health" in body


def test_make_proxy_server_validates_url() -> None:
    from ca.cli.dbsource import DatabaseSourceError
    from ca.ui.server import make_proxy_server

    with pytest.raises(DatabaseSourceError):
        make_proxy_server("127.0.0.1:8765")


# --------------------------------------------------------------------------- #
# CLI wiring
# --------------------------------------------------------------------------- #


def test_cli_parser_wires_ui_subcommand() -> None:
    from ca.cli.main import build_parser

    parser = build_parser()
    args = parser.parse_args(["ui", "--db", "ca.db", "--port", "0"])
    assert args.command == "ui"
    assert args.db == ["ca.db"]
    assert args.db_dir is None
    assert args.host == "127.0.0.1"
    assert args.port == 0
    assert callable(args.func)

    args = parser.parse_args(["ui", "--db", "a.db", "--db", "b.db", "--db-dir", "shards"])
    assert args.db == ["a.db", "b.db"]
    assert args.db_dir == "shards"
    assert args.host == "127.0.0.1"

    args = parser.parse_args(["ui", "--db-dir", "shards", "--host", "127.0.0.2", "--port", "9000"])
    assert args.db_dir == "shards"
    assert args.host == "127.0.0.2"
    assert args.port == 9000


def test_ui_command_reports_database_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from ca.cli.main import main

    assert main(["ui", "--db", str(tmp_path / "missing.db"), "--port", "0"]) == 1
    captured = capsys.readouterr()
    assert "error:" in captured.err
    assert "no readable database" in captured.err
    assert captured.out == ""


def test_make_server_requires_a_database(tmp_path: Path) -> None:
    from ca.cli.dbsource import DatabaseSourceError
    from ca.ui.server import make_server

    with pytest.raises(DatabaseSourceError):
        make_server(None)
    with pytest.raises(DatabaseSourceError):
        make_server([str(tmp_path / "missing.db")])
    with pytest.raises(DatabaseSourceError):
        make_server(None, str(tmp_path / "missing-dir"))



def test_accordion_tab_ids_exclude_removed_tabs() -> None:
    """AI explanation, callers and callees tabs were removed from normalizeTab()."""

    source = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    match = re.search(r"const TAB_IDS = \[(.*?)\];", source, re.S)
    assert match is not None, "TAB_IDS block missing"
    tab_ids = re.findall(r'"([a-z]+)"', match.group(1))
    assert "explain" not in tab_ids, f"explain still in TAB_IDS: {tab_ids}"
    assert "callers" not in tab_ids
    assert "callees" not in tab_ids
