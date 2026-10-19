"""Native launch contracts, exercised without requiring a packaging toolchain."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

from ca import desktop


@pytest.mark.parametrize(
    ("platform", "relative"),
    [
        ("win32", Path("AppData/Local/CA")),
        ("darwin", Path("Library/Application Support/CA")),
        ("linux", Path(".local/share/ca")),
    ],
)
def test_user_data_directory_is_outside_installation(monkeypatch, tmp_path, platform, relative):
    for key in ("CA_DATA_DIR", "LOCALAPPDATA", "XDG_DATA_HOME"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(sys, "platform", platform)
    assert desktop.data_directory() == tmp_path / relative


def test_first_run_creates_real_empty_index_and_never_overwrites_selection(monkeypatch, tmp_path):
    from ca.store.sqlite_store import SqliteStore

    monkeypatch.setenv("CA_DATA_DIR", str(tmp_path / "user data"))
    path = desktop.database_path(None)
    store = SqliteStore(path)
    store.open()
    try:
        assert store.stats()["tables"]["functions"] == 0
        assert store.query_functions("anything") == []
    finally:
        store.close()
    digest = hashlib.sha256(path.read_bytes()).digest()
    assert desktop.database_path(str(path)) == path
    assert desktop.database_path(None) == path
    assert hashlib.sha256(path.read_bytes()).digest() == digest
    missing = tmp_path / "typo.db"
    with pytest.raises(ValueError, match="database does not exist"):
        desktop.database_path(str(missing))
    assert not missing.exists()


def test_default_busy_port_falls_back_but_explicit_choice_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("CA_DATA_DIR", str(tmp_path))
    path = desktop.database_path(None)
    occupied = socket.socket()
    occupied.bind(("127.0.0.1", 0))
    occupied.listen()
    port = occupied.getsockname()[1]
    monkeypatch.setattr(desktop, "DEFAULT_PORT", port)
    args = argparse.Namespace(port=None, repo=None, compdb=None)
    server = desktop._make_server(args, path)
    try:
        assert server.server_address[1] != port
    finally:
        server.server_close()
        server.service.close()
    args.port = port
    try:
        with pytest.raises(OSError):
            desktop._make_server(args, path)
    finally:
        occupied.close()


def test_desktop_loopback_bind_does_not_require_reverse_dns(monkeypatch, tmp_path):
    monkeypatch.setenv("CA_DATA_DIR", str(tmp_path))
    path = desktop.database_path(None)

    def fail_lookup(_host):
        raise OSError("reverse DNS must not be needed to bind a loopback server")

    monkeypatch.setattr(socket, "getfqdn", fail_lookup)
    args = argparse.Namespace(port=0, repo=None, compdb=None)
    server = desktop._make_server(args, path)
    try:
        assert server.server_name == "127.0.0.1"
        assert server.server_port == server.server_address[1] > 0
    finally:
        server.server_close()
        server.service.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Winsock address-sharing behavior")
def test_windows_desktop_does_not_share_legacy_ui_listener(monkeypatch, tmp_path):
    from ca.ui.server import make_server as make_ui_server

    monkeypatch.setenv("CA_DATA_DIR", str(tmp_path))
    path = desktop.database_path(None)
    legacy = make_ui_server([str(path)], port=0)
    try:
        assert legacy.socket.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR) == 1
        legacy_port = legacy.server_address[1]
        monkeypatch.setattr(desktop, "DEFAULT_PORT", legacy_port)
        args = argparse.Namespace(port=None, repo=None, compdb=None)
        server = desktop._make_server(args, path)
        try:
            assert server.server_address[1] != legacy_port
            assert server.socket.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR) == 0
            assert server.socket.getsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE) == 1
            args.port = legacy_port
            with pytest.raises(OSError):
                desktop._make_server(args, path)
            # A later listener using the old reuse policy cannot steal requests
            # from the newly created desktop backend either.
            with socket.socket() as duplicate:
                duplicate.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                with pytest.raises(OSError):
                    duplicate.bind(server.server_address)
        finally:
            server.server_close()
            server.service.close()
    finally:
        legacy.server_close()


def test_desktop_process_serves_ui_and_mcp_without_stdout(monkeypatch, tmp_path):
    environment = os.environ.copy()
    environment["CA_DATA_DIR"] = str(tmp_path / "user data")
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    error_path = tmp_path / "desktop.log"
    output_path = tmp_path / "stdout.log"
    with error_path.open("w", encoding="utf-8") as error, output_path.open("w") as output:
        process = subprocess.Popen(
            [sys.executable, "-m", "ca", "desktop", "--no-browser", "--port", "0"],
            env=environment, stdout=output, stderr=error,
        )
        try:
            deadline = time.monotonic() + 15
            url = None
            while time.monotonic() < deadline:
                text = error_path.read_text(encoding="utf-8")
                match = re.search(r"CA browser UI: (http://127\.0\.0\.1:\d+)", text)
                if match:
                    url = match.group(1)
                    break
                assert process.poll() is None, text
                time.sleep(0.05)
            assert url is not None, error_path.read_text(encoding="utf-8")
            with urllib.request.urlopen(url + "/", timeout=5) as response:
                assert b"<html" in response.read().lower()
            with urllib.request.urlopen(url + "/api/stats", timeout=5) as response:
                payload = json.load(response)
                assert payload["ok"] is True
                assert payload["data"]["tables"]["functions"] == 0
            request = urllib.request.Request(
                url + "/mcp",
                data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                payload = json.load(response)
                assert payload["id"] == 1
                assert any(tool["name"] == "find_symbol" for tool in payload["result"]["tools"])
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    assert output_path.read_text() == ""


def test_failed_browser_does_not_stop_service_or_write_stdout(monkeypatch, capsys):
    def fail(_url):
        raise OSError("no system browser")

    monkeypatch.setattr(desktop.webbrowser, "open", fail)
    desktop._open_browser("http://127.0.0.1:8766")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "http://127.0.0.1:8766" in captured.err
