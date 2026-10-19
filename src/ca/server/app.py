"""HTTP backend for ``ca serve``: query API + jobs + SSE + registry + MCP endpoint.

One long-lived process owns the (optionally federated) read-only query service,
a writable job state database (``<db-dir>/.ca/jobs.db`` by default) and the
in-process event bus.  Browser UI and AI agents are two clients of the same
backend: ``/api/*`` queries, ``/api/jobs`` + ``/events`` for async work, and
``/mcp`` for JSON-RPC agents (``ca mcp --serve-url`` proxies stdio to it).
"""

from __future__ import annotations

import json
import signal
import socket
import sqlite3
import sys
import threading
import time
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from ca.cli.dbsource import DatabaseSourceError
from ca.cli.graph import GraphError
from ca.server.jobs import JobService
from ca.server.service import ServeConfig, ServeError, ServeService
from ca.ui.server import (
    FACT_ENDPOINTS,
    STATIC_DIR,
    STATIC_FILES,
    ApiError,
    api_callbacks,
    api_callees,
    api_callers,
    api_cfg,
    api_detail,
    api_facts,
    api_graph,
    api_health,
    api_search,
    api_source,
    api_stats,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
SSE_HEARTBEAT_SECONDS = 15.0
SSE_REPLAY_LIMIT = 500


def public_job(job: dict[str, Any] | None) -> dict[str, Any] | None:
    """Stable client-facing job shape (``type``/``symbol`` aliases are additive)."""

    if job is None:
        return None
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    result = job.get("result") if isinstance(job.get("result"), dict) else None
    return {
        "id": job.get("id", ""),
        "type": job.get("kind", "") or job.get("type", ""),
        "kind": job.get("kind", "") or job.get("type", ""),
        "status": job.get("status", "queued"),
        "symbol": payload.get("symbol", "") if isinstance(payload, dict) else "",
        "payload": payload,
        "result": result,
        "error": job.get("error") or "",
        "claimed_by": job.get("claimed_by") or "",
        "attempts": job.get("attempts", 0),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "lease_until": job.get("lease_until"),
    }


def _params(query: str) -> dict[str, str]:
    return {
        key: values[0] for key, values in parse_qs(query, keep_blank_values=True).items()
    }


def _int_param(params: dict[str, str], name: str, default: int) -> int:
    raw = params.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise ServeError("INVALID_ARGUMENT", f"parameter {name!r} must be an integer")


def _public_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    public: list[dict[str, Any]] = []
    for event in events:
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        job = data.get("job") if isinstance(data.get("job"), dict) else None
        public.append(
            {
                "type": "job.updated",
                "event": event.get("type", ""),
                "event_id": event.get("id", 0),
                "job": public_job(job),
                "reason": data.get("reason", ""),
                "worker": data.get("worker", ""),
            }
        )
    return public


def make_handler(service: ServeService) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "ca-serve/0.1"
        protocol_version = "HTTP/1.1"

        # -- helpers -------------------------------------------------- #
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            print(f"[serve] {self.address_string()} {format % args}", file=sys.stderr, flush=True)

        def _send_json(
            self,
            status: HTTPStatus | int,
            payload: Any,
            extra_headers: dict[str, str] | None = None,
        ) -> None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(int(status))
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for key, value in (extra_headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _ok(self, data: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
            self._send_json(status, {"ok": True, "data": data})

        def _error(self, status: HTTPStatus, code: str, message: str) -> None:
            self._send_json(status, {"ok": False, "error": {"code": code, "message": message}})

        def _body(self) -> dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            if not raw:
                return {}
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ServeError("INVALID_ARGUMENT", f"invalid JSON body: {exc}") from None
            if not isinstance(payload, dict):
                raise ServeError("INVALID_ARGUMENT", "JSON body must be an object")
            return payload

        def _static(self, name: str) -> None:
            path = STATIC_DIR / name
            if not path.is_file() or name not in STATIC_FILES:
                self._error(HTTPStatus.NOT_FOUND, "NOT_FOUND", f"not found: {name}")
                return
            body = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", STATIC_FILES[name])
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        # -- GET ------------------------------------------------------ #
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            parsed = urlsplit(self.path)
            path = parsed.path or "/"
            params = _params(parsed.query)
            try:
                if path in ("/", "/index.html"):
                    self._static("index.html")
                    return
                if path.startswith("/") and path.count("/") == 1 and path[1:] in STATIC_FILES:
                    self._static(path[1:])
                    return
                if path in ("/api/health", "/health"):
                    health = api_health(service.query, service.started_at)
                    health["features"] = {
                        "jobs": True,
                        "sse": True,
                        "mcp": True,
                        "registry": True,
                        "federated": service.federated,
                    }
                    health["jobs"] = service.jobs.job_stats()
                    health["mcp_endpoint"] = "/mcp"
                    health["events_endpoint"] = "/events"
                    self._ok(health)
                    return
                if path == "/api/registry":
                    self._ok(service.registry())
                    return
                if path == "/api/jobs":
                    data = service.list_jobs(
                        kind=params.get("type") or params.get("kind") or None,
                        status=params.get("status") or None,
                        limit=_int_param(params, "limit", 100),
                        offset=_int_param(params, "offset", 0),
                    )
                    data["items"] = [public_job(job) for job in data["items"]]
                    self._ok(data)
                    return
                if path.startswith("/api/jobs/") and path.count("/") == 3:
                    job_id = unquote(path[len("/api/jobs/") :])
                    job = service.get_job(job_id)
                    if job is None:
                        self._error(HTTPStatus.NOT_FOUND, "NOT_FOUND", f"job not found: {job_id}")
                        return
                    self._ok({"job": public_job(job)})
                    return
                if path == "/events":
                    self._stream_events(params)
                    return
                self._route_ui_get(path, params)
            except ServeError as exc:
                self._error(HTTPStatus.BAD_REQUEST, exc.code, exc.message)
            except DatabaseSourceError as exc:
                self._error(HTTPStatus.SERVICE_UNAVAILABLE, "DB_UNAVAILABLE", str(exc))
            except ApiError as exc:
                self._error(exc.status, exc.code, exc.message)
            except (ValueError, GraphError) as exc:
                self._error(HTTPStatus.BAD_REQUEST, "INVALID_ARGUMENT", str(exc))
            except sqlite3.Error as exc:
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "DB_ERROR", f"{type(exc).__name__}: {exc}")
            except Exception as exc:  # pragma: no cover - defensive last resort
                print(traceback.format_exc(), file=sys.stderr, flush=True)
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "INTERNAL", f"{type(exc).__name__}: {exc}")

        def _route_ui_get(self, path: str, params: dict[str, str]) -> None:
            query = service.query
            if path == "/api/search":
                self._ok(api_search(query, params))
                return
            if path in ("/api/function", "/api/detail"):
                symbol = params.get("symbol") or params.get("name") or ""
                self._ok(api_detail(query, symbol))
                return
            if path.startswith("/api/function/"):
                self._ok(api_detail(query, unquote(path[len("/api/function/") :])))
                return
            if path == "/api/callers":
                self._ok(api_callers(query, params))
                return
            if path == "/api/callees":
                self._ok(api_callees(query, params))
                return
            if path == "/api/graph":
                self._ok(api_graph(query, params))
                return
            if path == "/api/stats":
                self._ok(api_stats(query))
                return
            if path == "/api/callbacks":
                self._ok(api_callbacks(query, params))
                return
            if path == "/api/source":
                self._ok(api_source(query, params))
                return
            if path == "/api/cfg":
                self._ok(api_cfg(query, params))
                return
            if path in FACT_ENDPOINTS:
                self._ok(api_facts(query, params, FACT_ENDPOINTS[path]))
                return
            if path == "/api/features":
                self._ok({"jobs": True, "sse": True, "mcp": True, "federated": service.federated})
                return
            if path.startswith("/api/"):
                self._error(HTTPStatus.NOT_FOUND, "NOT_FOUND", f"unknown endpoint: {path}")
                return
            self._error(HTTPStatus.NOT_FOUND, "NOT_FOUND", f"not found: {path}")

        # -- POST ----------------------------------------------------- #
        def do_POST(self) -> None:  # noqa: N802 - http.server API
            parsed = urlsplit(self.path)
            path = parsed.path or "/"
            try:
                if path == "/mcp":
                    self._handle_mcp()
                    return
                if path == "/api/registry/active":
                    body = self._body()
                    self._ok(service.set_active_variant(body.get("variant_id")))
                    return
                if path == "/api/jobs":
                    self._ok({"job": public_job(service.create_job(self._body()))}, HTTPStatus.CREATED)
                    return
                parts = [part for part in path.split("/") if part]
                if len(parts) == 4 and parts[:2] == ["api", "jobs"]:
                    job_id = unquote(parts[2])
                    action = parts[3]
                    body = self._body()
                    if action == "claim":
                        job = service.claim_job(body)
                        if job is None:
                            self._ok({"job": None, "claimed": False})
                        else:
                            self._ok({"job": public_job(job), "claimed": True})
                        return
                    if action == "result":
                        self._ok({"job": public_job(service.submit_job_result(job_id, body))})
                        return
                    if action == "fail":
                        self._ok({"job": public_job(service.fail_job(job_id, body))})
                        return
                    if action == "requeue":
                        job = service.get_job(job_id)
                        if job is None:
                            raise ServeError("NOT_FOUND", f"job not found: {job_id}")
                        ok = service.jobs.requeue_job(job_id)
                        self._ok({"job": public_job(service.get_job(job_id)), "requeued": ok})
                        return
                self._error(HTTPStatus.NOT_FOUND, "NOT_FOUND", f"unknown endpoint: {path}")
            except ServeError as exc:
                status = HTTPStatus.BAD_REQUEST
                if exc.code == "NOT_FOUND":
                    status = HTTPStatus.NOT_FOUND
                elif exc.code == "CONFLICT":
                    status = HTTPStatus.CONFLICT
                elif exc.code == "DB_UNAVAILABLE":
                    status = HTTPStatus.SERVICE_UNAVAILABLE
                elif exc.code == "INTERNAL":
                    status = HTTPStatus.INTERNAL_SERVER_ERROR
                self._error(status, exc.code, exc.message)
            except DatabaseSourceError as exc:
                self._error(HTTPStatus.SERVICE_UNAVAILABLE, "DB_UNAVAILABLE", str(exc))
            except ApiError as exc:
                self._error(exc.status, exc.code, exc.message)
            except sqlite3.Error as exc:
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "DB_ERROR", f"{type(exc).__name__}: {exc}")
            except Exception as exc:  # pragma: no cover
                print(traceback.format_exc(), file=sys.stderr, flush=True)
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "INTERNAL", f"{type(exc).__name__}: {exc}")

        def _handle_mcp(self) -> None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length > 0 else b"{}"
                message = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32700, "message": f"parse error: {exc}"},
                    },
                )
                return
            try:
                if isinstance(message, list):
                    responses = [
                        response
                        for response in (service.handle_mcp(item) for item in message)
                        if response is not None
                    ]
                    if not responses:
                        self.send_response(HTTPStatus.ACCEPTED)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    self._send_json(HTTPStatus.OK, responses)
                    return
                response = service.handle_mcp(message)
            except ServeError as exc:
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "jsonrpc": "2.0",
                        "id": message.get("id") if isinstance(message, dict) else None,
                        "error": {"code": -32602, "message": exc.message},
                    },
                )
                return
            except Exception as exc:  # pragma: no cover
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "jsonrpc": "2.0",
                        "id": message.get("id") if isinstance(message, dict) else None,
                        "error": {
                            "code": -32603,
                            "message": f"internal error: {type(exc).__name__}: {exc}",
                        },
                    },
                )
                return
            if response is None:
                self.send_response(HTTPStatus.ACCEPTED)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._send_json(HTTPStatus.OK, response)

        # -- SSE ------------------------------------------------------ #        # -- SSE ------------------------------------------------------ #
        def _stream_events(self, params: dict[str, str]) -> None:
            since_id = 0
            header_id = self.headers.get("Last-Event-ID")
            if header_id:
                try:
                    since_id = int(header_id)
                except ValueError:
                    since_id = 0
            if params.get("since_id"):
                try:
                    since_id = int(params["since_id"])
                except ValueError:
                    raise ServeError("INVALID_ARGUMENT", "since_id must be an integer") from None
            subscription = service.jobs.bus.subscribe()
            try:
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "keep-alive")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                self.wfile.write(b"retry: 3000\n\n")
                self.wfile.flush()
                last_sent = since_id
                replay = service.jobs.list_job_events(since_id=since_id, limit=SSE_REPLAY_LIMIT)
                for event in replay:
                    last_sent = max(last_sent, int(event.get("id") or 0))
                    self._write_event(event)
                while True:
                    event = subscription.get(timeout=SSE_HEARTBEAT_SECONDS)
                    if event is None:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        continue
                    if int(event.get("id") or 0) <= last_sent:
                        continue
                    last_sent = int(event.get("id") or 0)
                    self._write_event(event)
            except OSError:  # client disconnected mid-stream (BrokenPipe/Reset)
                pass
            finally:
                subscription.close()

        def _write_event(self, event: dict[str, Any]) -> None:
            public = _public_events([event])
            if not public:
                return
            payload = json.dumps(public[0], ensure_ascii=False, separators=(",", ":"))
            self.wfile.write(
                f"id: {event.get('id', 0)}\ndata: {payload}\n\n".encode("utf-8")
            )
            self.wfile.flush()

        # -- other methods ------------------------------------------- #
        def _method_not_allowed(self) -> None:
            self._error(HTTPStatus.METHOD_NOT_ALLOWED, "METHOD_NOT_ALLOWED", "unsupported method")

        do_PUT = _method_not_allowed  # noqa: N815
        do_PATCH = _method_not_allowed  # noqa: N815

        do_DELETE = _method_not_allowed  # noqa: N815

    return Handler


class ServeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = sys.platform != "win32"

    def __init__(self, address: tuple[str, int], service: ServeService) -> None:
        self.service = service
        self.started_at = time.time()
        super().__init__(address, make_handler(service))

    def server_bind(self) -> None:
        if sys.platform == "win32":
            # Winsock SO_REUSEADDR can bind an active listener and route the
            # browser to either process. Exclusive binding makes conflicts fail
            # before desktop chooses another port, including legacy UI servers.
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        # HTTPServer.server_bind performs an unnecessary reverse DNS lookup,
        # which can stall local startup on hosts without working DNS. The
        # actual bound address is sufficient for this non-CGI HTTP server.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}"


def make_server(config: ServeConfig) -> ServeHTTPServer:
    service = ServeService(config)
    try:
        return ServeHTTPServer((config.host, int(config.port)), service)
    except OSError:
        service.close()
        raise


def serve(config: ServeConfig) -> int:
    """Run the backend until SIGINT/SIGTERM; returns a CLI exit code."""

    try:
        server = make_server(config)
    except DatabaseSourceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: cannot bind {config.host}:{config.port}: {exc}", file=sys.stderr)
        return 1
    print(
        f"[serve] CA backend listening on {server.url}"
        f" (db={', '.join(server.service.paths)}; federated="
        f"{'yes' if server.service.federated else 'no'}; state={server.service.state_db})",
        file=sys.stderr,
        flush=True,
    )
    print(
        f"[serve] endpoints: {server.url}/api/* {server.url}/mcp {server.url}/events {server.url}/api/jobs",
        file=sys.stderr,
        flush=True,
    )
    stop = threading.Event()
    previous: dict[int, Any] = {}

    def _on_signal(signum: int, _frame: Any) -> None:
        stop.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[signum] = signal.signal(signum, _on_signal)
        except (ValueError, OSError):  # not in the main thread
            pass
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.2},
        name="ca-serve-http",
        daemon=True,
    )
    try:
        thread.start()
        while not stop.wait(0.2):
            pass
    except KeyboardInterrupt:  # pragma: no cover - interactive path
        stop.set()
    finally:
        server.shutdown()
        server.server_close()
        for signum, handler in previous.items():
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):
                pass
        server.service.close()
        print("[serve] server stopped", file=sys.stderr, flush=True)
    return 0
