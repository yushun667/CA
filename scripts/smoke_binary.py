"""Exercise a frozen CA binary without importing the development checkout."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import socket
import sqlite3
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


def run(binary: Path, args: list[str], root: Path, env: dict[str, str], **kwargs):
    result = subprocess.run(
        [str(binary), *args], cwd=root, env=env, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=120, **kwargs,
    )
    if result.returncode:
        raise RuntimeError(f"{args!r} exited {result.returncode}\n{result.stderr}\n{result.stdout}")
    return result.stdout


def rpc(request_id: int, method: str, params: dict | None = None) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}


def initialize() -> dict:
    return rpc(1, "initialize", {
        "protocolVersion": "2024-11-05", "capabilities": {},
        "clientInfo": {"name": "ca-binary-smoke", "version": "1"},
    })


def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.load(response)


@contextlib.contextmanager
def serving(binary: Path, args: list[str], root: Path, env: dict[str, str]):
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as log:
        process = subprocess.Popen(
            [str(binary), *args, "--port", str(port)], cwd=root, env=env,
            stdout=log, stderr=log,
        )
        try:
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                ready = False
                try:
                    ready = bool(get_json(base + "/api/health").get("ok"))
                except (OSError, urllib.error.URLError):
                    time.sleep(0.15)
                if ready:
                    yield base
                    return
            log.seek(0)
            raise RuntimeError(f"server did not become healthy\n{log.read()}")
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)


def check_web(base: str) -> None:
    for path in ("/", "/app.js", "/i18n.js", "/highlight.js", "/style.css", "/favicon.svg"):
        with urllib.request.urlopen(base + path, timeout=5) as response:
            assert response.status == 200 and response.read(), path
    search = get_json(base + "/api/search?q=start")
    assert search["ok"] and search["data"]["count"] >= 1, search
    source = get_json(base + "/api/source?symbol=start")
    assert source["ok"] and source["data"]["lines"], source
    graph = get_json(base + "/api/graph?symbol=start&depth=2")
    assert graph["ok"] and len(graph["data"]["nodes"]) >= 2, graph


def check_http_mcp(base: str) -> None:
    for request in (initialize(), rpc(2, "tools/list"), rpc(3, "tools/call", {
        "name": "stats", "arguments": {},
    })):
        body = json.dumps(request).encode("utf-8")
        http = urllib.request.Request(base + "/mcp", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(http, timeout=10) as response:
            result = json.load(response)
        if isinstance(result, list):
            assert len(result) == 1, result
            result = result[0]
        assert result["id"] == request["id"] and "result" in result, result
        assert not result["result"].get("isError"), result


def smoke(binary: Path) -> dict:
    env = os.environ.copy()
    for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "CA_LIBCLANG_LIBRARY",
                 "CA_LIBCLANG_LIBRARY_FILE", "CA_LIBCLANG_LIBRARY_PATH"):
        env.pop(name, None)
    env["PYTHONNOUSERSITE"] = "1"
    with tempfile.TemporaryDirectory(prefix="ca binary smoke ") as directory:
        root = Path(directory)
        env["CA_DATA_DIR"] = str(root / "user data")
        source = root / "source tree"
        source.mkdir()
        (source / "a.c").write_text(
            "int helper(int value) { if (value > 0) return value; return 0; }\n",
            encoding="utf-8",
        )
        (source / "b.c").write_text(
            "int helper(int); int start(void) { return helper(3); }\n", encoding="utf-8",
        )
        db = root / "index.db"
        version = run(binary, ["version"], root, env).strip()
        assert "ca-analyzer" in version, version
        run(binary, ["index", "--repo", str(source), "--db", str(db),
                     "--source", "synthesized", "--jobs", "2"], root, env)
        with contextlib.closing(sqlite3.connect(db)) as connection:
            functions = connection.execute("SELECT count(*) FROM functions").fetchone()[0]
            units = connection.execute("SELECT count(*) FROM tus").fetchone()[0]
        assert functions >= 2 and units == 2, (functions, units)
        json.loads(run(binary, ["stats", "--db", str(db), "--json"], root, env))
        requests = (initialize(), rpc(2, "tools/list"), rpc(3, "tools/call", {
            "name": "stats", "arguments": {},
        }))
        stdout = run(binary, ["mcp", "--db", str(db)], root, env,
                     input="".join(json.dumps(item) + "\n" for item in requests))
        replies = [json.loads(line) for line in stdout.splitlines() if line.strip()]
        assert len(replies) == 3, replies
        for request, reply in zip(requests, replies):
            assert reply["id"] == request["id"] and "result" in reply, reply
            assert not reply["result"].get("isError"), reply
        assert any(tool["name"] == "stats" for tool in replies[1]["result"]["tools"])
        with serving(binary, ["ui", "--db", str(db)], root, env) as base:
            check_web(base)
        with serving(binary, ["desktop", "--db", str(db), "--no-browser"], root, env) as base:
            check_web(base)
            check_http_mcp(base)
        with serving(binary, ["desktop", "--no-browser"], root, env) as base:
            health = get_json(base + "/api/health")
            assert health["data"]["db_ok"], health
            assert (root / "user data" / "index.db").is_file()
        return {"ok": True, "binary": str(binary), "version": version,
                "translation_units": units, "functions": functions,
                "checks": ["version", "multiprocess-index", "stats", "mcp-stdio",
                           "web-ui-static-search-source-graph", "desktop-http-mcp",
                           "first-launch-empty-database"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = smoke(args.binary.resolve(strict=True))
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
