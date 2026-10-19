"""Desktop launcher for the native bundle and ``ca desktop``.

An empty first-run index is kept in the user's data directory. Selected indexes
are not reinitialized. All launcher diagnostics go to stderr so CLI/MCP stdout
keeps its existing contract.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import os
from pathlib import Path
import signal
import sys
import threading
from typing import Any
import webbrowser

DEFAULT_PORT = 8766


def data_directory() -> Path:
    """Return a writable, per-user location independent of the install folder."""

    override = os.environ.get("CA_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "CA"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "CA"
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "ca"


def database_path(value: str | None) -> Path:
    """Validate an existing selection or initialize only the default empty DB."""

    if value:
        path = Path(value).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"database does not exist: {path}; create it with ca index")
        return path
    path = data_directory() / "index.db"
    if not path.exists():
        from ca.store.sqlite_store import SqliteStore

        store = SqliteStore(path)
        try:
            store.open()
            store.finalize()
        finally:
            store.close()
        print(
            f"[desktop] Created an empty index: {path}\n"
            f'[desktop] Index your sources with: ca index --repo <repository> --db "{path}"',
            file=sys.stderr,
            flush=True,
        )
    return path


def _make_server(args: argparse.Namespace, path: Path) -> Any:
    from ca.server import ServeConfig, make_server

    identity = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]
    config = ServeConfig(
        db_values=[str(path)],
        host="127.0.0.1",
        port=DEFAULT_PORT if args.port is None else args.port,
        state_db=str(data_directory() / f"desktop-{identity}.serve-state.db"),
        repo=args.repo or "",
        compdb=args.compdb or "",
    )
    try:
        return make_server(config)
    except OSError as exc:
        # A second desktop instance must not silently display an unrelated
        # process on the usual port. Explicit port choices remain strict.
        busy = exc.errno == errno.EADDRINUSE or getattr(exc, "winerror", None) in (10013, 10048)
        if args.port is not None or not busy:
            raise
        config.port = 0
        return make_server(config)


def _open_browser(url: str) -> None:
    """Open a system browser without inheriting bundled-library search paths."""

    original: dict[str, str | None] = {}
    dll_directory = None
    try:
        if getattr(sys, "frozen", False):
            if sys.platform == "win32":
                import ctypes

                dll_directory = ctypes.windll.kernel32.SetDllDirectoryW
                dll_directory(None)
            else:
                for name in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"):
                    original[name] = os.environ.get(name)
                    previous = os.environ.get(name + "_ORIG")
                    if previous is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = previous
        if not webbrowser.open(url):
            print(f"[desktop] Open this address in your browser: {url}", file=sys.stderr)
    except Exception as exc:
        print(f"[desktop] Could not open a browser ({exc}); open {url}", file=sys.stderr)
    finally:
        for name, value in original.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        if dll_directory is not None:
            dll_directory(str(getattr(sys, "_MEIPASS", "")) or None)


def launch(args: argparse.Namespace) -> int:
    """Run UI, query API and HTTP MCP in the same local backend process."""

    if args.port is not None and not 0 <= args.port <= 65535:
        raise ValueError("port must be between 0 and 65535")
    path = database_path(args.db)
    server = _make_server(args, path)
    stop = threading.Event()
    previous: dict[int, Any] = {}

    def on_signal(_signum: int, _frame: Any) -> None:
        stop.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[signum] = signal.signal(signum, on_signal)
        except (ValueError, OSError):  # Embedded callers may use another thread.
            pass
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.2},
        name="ca-desktop-http",
        daemon=True,
    )
    started = False
    try:
        thread.start()
        started = True
        print(f"[desktop] CA browser UI: {server.url}", file=sys.stderr, flush=True)
        print(f"[desktop] HTTP MCP: {server.url}/mcp; database: {path}", file=sys.stderr, flush=True)
        print("[desktop] Press Ctrl+C or close this console to stop CA.", file=sys.stderr, flush=True)
        if not args.no_browser:
            _open_browser(server.url)
        while not stop.wait(0.2):
            if not thread.is_alive():
                raise RuntimeError("desktop HTTP server stopped unexpectedly")
    except KeyboardInterrupt:
        stop.set()
    finally:
        if started:
            server.shutdown()
            thread.join(timeout=5)
        server.server_close()
        server.service.close()
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return 0
