"""Service state shared by the ``ca serve`` HTTP and MCP endpoints."""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ca.cli.dbsource import resolve_paths
from ca.cli.mcp import McpSession, TOOLS as MCP_TOOLS
from ca.core.schema import TOOL_VERSION
from ca.server.jobs import JobService
from ca.server.mcp_sessions import McpSessionRegistry
from ca.ui.server import QueryService, resolve_service

JOB_MCP_TOOLS: list[dict[str, Any]] = [
    {
        "name": "explain_jobs",
        "description": (
            "List or claim queued 'explain' jobs created by the Web UI. Use"
            " action='claim' to atomically take the oldest queued job, then"
            " submit_explanation when done."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "claim"]},
                "status": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "worker": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "submit_explanation",
        "description": (
            "Submit the explanation for a claimed explain job; marks the job done"
            " and emits a done event so the Web UI updates via SSE."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "explanation": {"type": "string"},
                "evidence": {},
                "worker": {"type": "string"},
            },
            "required": ["job_id", "explanation"],
            "additionalProperties": False,
        },
    },
]


EXPLAIN_FUNCTION_TOOL: dict[str, Any] = {
    "name": "explain_function",
    "description": (
        "Explain a function using the MCP client's own model via server-initiated"
        " sampling/createMessage. The answer is returned synchronously in this"
        " tool result; without a sampling-capable session the local heuristic is"
        " returned instead."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "lang": {"type": "string", "enum": ["en", "zh"]},
        },
        "required": ["symbol"],
        "additionalProperties": False,
    },
}


class ServeError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class ServeConfig:
    db_values: Any = field(default_factory=list)
    db_dir: str | None = None
    host: str = "127.0.0.1"
    port: int = 8765
    state_db: str | None = None
    repo: str = ""
    compdb: str = ""
    explain_grace_ms: int | None = None
    sampling_timeout: float | None = None


class ServeService:
    """Owns query access, the job queue and the MCP protocol session."""

    def __init__(self, config: ServeConfig) -> None:
        self.config = config
        self.query: QueryService = resolve_service(config.db_values, config.db_dir)
        self.paths = list(self.query.paths)
        self.federated = bool(self.query.federated)
        self.started_at = time.time()
        state_db = config.state_db
        if not state_db:
            if config.db_dir:
                base = Path(config.db_dir).expanduser()
            elif len(self.paths) == 1:
                base = Path(self.paths[0]).expanduser().with_name(
                    Path(self.paths[0]).stem + ".serve-state.db"
                )
            else:
                base = Path("ca-serve-state.db")
            if config.db_dir:
                state_db = str(base / ".ca" / "jobs.db")
            else:
                state_db = str(base)
        self.state_db = str(Path(state_db).expanduser())
        self.jobs = JobService(self.state_db)
        recovered = self.jobs.recover()
        if recovered:
            print(
                f"[serve] recovered {recovered} interrupted running job(s)",
                file=__import__("sys").stderr,
                flush=True,
            )
        self.mcp = McpSession(
            self.paths, repo=config.repo, compdb=config.compdb, federated=self.federated
        )
        self.mcp_sessions = McpSessionRegistry()
        self._auto_lock = threading.Lock()
        self._auto_timers: dict[str, threading.Timer] = {}
        self._reschedule_auto_explain()

    # -- lifecycle ------------------------------------------------------- #
    def close(self) -> None:
        with self._auto_lock:
            timers = list(self._auto_timers.values())
            self._auto_timers.clear()
        for timer in timers:
            timer.cancel()
        self.mcp_sessions.close_all()
        self.jobs.close()

    # -- synchronous agent explain (MCP sampling, no jobs/queue) --------- #
    def sampling_timeout(self) -> float:
        """Seconds to wait for a connected agent's sampling response."""

        if self.config.sampling_timeout is not None:
            try:
                value = float(self.config.sampling_timeout)
            except (TypeError, ValueError):
                value = 30.0
        else:
            raw = os.environ.get("CA_SAMPLING_TIMEOUT", "").strip()
            try:
                value = float(raw) if raw else 30.0
            except ValueError:
                value = 30.0
        return max(1.0, min(value, 120.0))

    def explain_agent(
        self, symbol: str, language: str = "en", *, preferred_session_id: str | None = None
    ) -> dict[str, Any]:
        """Synchronous explain over MCP sampling; heuristic fallback (HTTP 200)."""

        from ca.server.agent_explain import explain_via_sampling

        if not str(symbol or "").strip():
            raise ServeError("INVALID_ARGUMENT", "symbol is required")
        return explain_via_sampling(
            self.query,
            self.mcp_sessions,
            str(symbol).strip(),
            language,
            timeout=self.sampling_timeout(),
            preferred_session_id=preferred_session_id,
        )

    # -- explain auto-complete ------------------------------------------- #
    def explain_grace_ms(self) -> int:
        """Grace before the server completes an unclaimed explain job itself.

        Priority: ``ServeConfig.explain_grace_ms`` -> ``CA_EXPLAIN_GRACE_MS``
        environment -> 2000 ms. ``0``/negative disables auto-completion (agents
        own the queue).
        """

        if self.config.explain_grace_ms is not None:
            return int(self.config.explain_grace_ms)
        raw = os.environ.get("CA_EXPLAIN_GRACE_MS", "").strip()
        if not raw:
            return 2000
        try:
            return int(raw)
        except ValueError:
            return 2000

    def _schedule_auto_explain(self, job_id: str, delay_ms: int | None = None) -> None:
        grace = self.explain_grace_ms() if delay_ms is None else int(delay_ms)
        if grace <= 0:
            return
        timer = threading.Timer(grace / 1000.0, self._auto_complete_explain, args=(job_id,))
        timer.daemon = True
        with self._auto_lock:
            previous = self._auto_timers.pop(job_id, None)
            if previous is not None:
                previous.cancel()
            self._auto_timers[job_id] = timer
        timer.start()

    def _cancel_auto_explain(self, job_id: str) -> None:
        with self._auto_lock:
            timer = self._auto_timers.pop(job_id, None)
        if timer is not None:
            timer.cancel()

    def _reschedule_auto_explain(self) -> None:
        if self.explain_grace_ms() <= 0:
            return
        for job in self.jobs.list_jobs(kind="explain", status="queued", limit=100):
            self._schedule_auto_explain(str(job.get("id") or ""))

    def _auto_complete_explain(self, job_id: str) -> None:
        with self._auto_lock:
            self._auto_timers.pop(job_id, None)
        job = self.jobs.get_job(job_id)
        if job is None or job.get("status") != "queued":
            return
        payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
        symbol = str(payload.get("symbol") or "").strip()
        claimed = self.jobs.claim_job(
            "explain", claimed_by="server:auto", lease_seconds=120
        )
        if claimed is None:
            return  # an agent won the claim first; never overwrite it
        if claimed.get("id") != job_id:
            # Multiple queued explain jobs: claim_job() returns the oldest, which
            # may be a different job than this timer. Requeue it (no data loss) and
            # retry this job shortly instead of leaving the other one running.
            other_id = str(claimed.get("id") or "")
            self.jobs.requeue_job(other_id, error="", claimed_by="server:auto")
            self._schedule_auto_explain(other_id)
            self._schedule_auto_explain(job_id, delay_ms=max(20, self.explain_grace_ms() // 4))
            return
        try:
            if not symbol:
                raise ServeError("INVALID_ARGUMENT", "explain job has no symbol")
            from ca.ui.server import api_explain

            language = str(payload.get("lang") or payload.get("language") or "en")
            result = dict(api_explain(self.query, {"symbol": symbol, "language": language}))
            result["fallback"] = True
            result["auto_completed"] = True
            self.jobs.complete_job(job_id, result, claimed_by="server:auto")
            print(
                f"[serve] auto-completed explain job {job_id} ({result.get('provider')})",
                file=__import__("sys").stderr,
                flush=True,
            )
        except Exception as exc:  # keep the queue clean, surface the reason
            self.jobs.fail_job(
                job_id,
                f"auto explain failed: {type(exc).__name__}: {exc}",
                claimed_by="server:auto",
            )

    # -- health ---------------------------------------------------------- #
    def health(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "server": "ca-serve",
            "version": TOOL_VERSION,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(self.started_at)),
            "uptime_seconds": round(time.time() - self.started_at, 3),
            "database": {
                "paths": self.paths,
                "federated": self.federated,
                "state_db": self.state_db,
            },
            "jobs": self.jobs.job_stats(),
            "explain_grace_ms": self.explain_grace_ms(),
            "sampling": {
                **self.mcp_sessions.stats(),
                "transport": "mcp-http-sse",
                "mcp_endpoint": "/mcp",
                "explain_endpoint": "/api/explain/agent",
                "explain_tool": "explain_function",
                "timeout_seconds": self.sampling_timeout(),
            },
            "mcp_endpoint": "/mcp",
            "events_endpoint": "/events",
        }

    # -- jobs ------------------------------------------------------------ #
    def create_job(self, body: dict[str, Any]) -> dict[str, Any]:
        kind = str(body.get("kind") or body.get("type") or "").strip()
        if not kind:
            raise ServeError("INVALID_ARGUMENT", "job 'kind' is required")
        payload = body.get("payload")
        if payload is None:
            payload = {}
        elif not isinstance(payload, dict):
            raise ServeError("INVALID_ARGUMENT", "job 'payload' must be an object")
        merged = dict(payload)
        for key in ("symbol", "question", "path", "repo", "lang"):
            if key in body and key not in merged:
                merged[key] = body[key]
        job = self.jobs.create_job(kind, merged)
        if kind == "explain":
            self._schedule_auto_explain(str(job.get("id") or ""))
        return job

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        return self.jobs.get_job(job_id)

    def list_jobs(self, *, kind: str | None = None, status: str | None = None, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        items = self.jobs.list_jobs(kind=kind, status=status, limit=limit, offset=offset)
        return {
            "items": items,
            "count": len(items),
            "limit": max(1, min(1000, int(limit))),
            "offset": max(0, int(offset)),
            "stats": self.jobs.job_stats(),
        }

    def claim_job(self, body: dict[str, Any]) -> dict[str, Any] | None:
        worker = str(body.get("worker") or body.get("claimed_by") or "http:agent")
        kind = body.get("kind")
        lease = int(body.get("lease_seconds") or 60)
        job = self.jobs.claim_job(
            str(kind) if kind else None, claimed_by=worker, lease_seconds=lease
        )
        if job is None:
            return None
        self._cancel_auto_explain(str(job.get("id") or ""))
        job["claimed_by"] = job.get("claimed_by") or worker
        return job

    def submit_job_result(self, job_id: str, body: dict[str, Any]) -> dict[str, Any]:
        job = self.jobs.get_job(job_id)
        if job is None:
            raise ServeError("NOT_FOUND", f"job not found: {job_id}")
        result = body.get("result")
        if result is None:
            result = {
                key: body[key]
                for key in ("explanation", "evidence", "summary")
                if key in body
            }
        if not isinstance(result, dict):
            raise ServeError("INVALID_ARGUMENT", "job 'result' must be an object")
        result = self._agent_result(dict(result), str(body.get("provider") or "agent"))
        worker = str(body.get("worker") or body.get("claimed_by") or "http:agent")
        if job.get("status") == "queued":
            claimed = self.jobs.claim_job(
                str(job.get("kind") or "") or None, claimed_by=worker
            )
            job = claimed or job
        self._cancel_auto_explain(job_id)
        if job.get("status") != "running":
            raise ServeError(
                "CONFLICT", f"job {job_id} is {job.get('status')}, expected running"
            )
        claimed_by = str(job.get("claimed_by") or "")
        if not self.jobs.complete_job(job_id, result, claimed_by=claimed_by or None):
            raise ServeError("CONFLICT", f"job {job_id} could not be completed")
        return self.jobs.get_job(job_id) or {}

    def fail_job(self, job_id: str, body: dict[str, Any]) -> dict[str, Any]:
        error = str(body.get("error") or "failed")
        job = self.jobs.get_job(job_id)
        if job is None:
            raise ServeError("NOT_FOUND", f"job not found: {job_id}")
        claimed_by = str(job.get("claimed_by") or "") or None
        if not self.jobs.fail_job(job_id, error, claimed_by=claimed_by):
            raise ServeError("CONFLICT", f"job {job_id} could not be failed")
        return self.jobs.get_job(job_id) or {}

    # -- MCP /mcp -------------------------------------------------------- #
    def mcp_tools(self) -> list[dict[str, Any]]:
        return list(MCP_TOOLS) + list(JOB_MCP_TOOLS) + [EXPLAIN_FUNCTION_TOOL]

    def _jsonrpc_result(self, request_id: Any, data: Any) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [
                    {"type": "text", "text": json.dumps(data, ensure_ascii=False)}
                ],
                "isError": False,
            },
        }

    def _explain_function_tool(
        self, request_id: Any, arguments: dict[str, Any], session_id: str | None
    ) -> dict[str, Any]:
        symbol = str(arguments.get("symbol") or arguments.get("name") or "").strip()
        if not symbol:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "ok": False,
                                    "tool": "explain_function",
                                    "error": {
                                        "code": "INVALID_ARGUMENT",
                                        "message": "symbol is required",
                                    },
                                }
                            ),
                        }
                    ],
                    "isError": True,
                },
            }
        try:
            data = self.explain_agent(
                symbol,
                str(arguments.get("lang") or arguments.get("language") or "en"),
                preferred_session_id=session_id,
            )
            envelope = {"ok": True, "tool": "explain_function", "data": data}
        except ServeError as exc:
            envelope = {
                "ok": False,
                "tool": "explain_function",
                "error": {"code": exc.code, "message": exc.message},
            }
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [{"type": "text", "text": json.dumps(envelope, ensure_ascii=False)}],
                "isError": not envelope["ok"],
            },
        }

    def handle_mcp(
        self, message: Any, session_id: str | None = None
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Handle one MCP message; returns ``(response, session_id)``.

        ``initialize`` creates a standard MCP session (the app echoes
        ``Mcp-Session-Id``); a sampling-capable session is later used for
        server-initiated ``sampling/createMessage``.  Client responses to those
        requests (id-only JSON-RPC result/error) are correlated here.
        """

        if not isinstance(message, dict):
            raise ServeError("INVALID_ARGUMENT", "expected a JSON-RPC object")
        method = message.get("method")
        if method == "initialize":
            params = message.get("params") if isinstance(message.get("params"), dict) else {}
            capabilities = params.get("capabilities")
            client_info = params.get("clientInfo")
            new_session = self.mcp_sessions.open_session(
                capabilities if isinstance(capabilities, dict) else {},
                client_info if isinstance(client_info, dict) else {},
            )
            return self.mcp.handle(message), new_session
        if method == "tools/list":
            request_id = message.get("id")
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"tools": self.mcp_tools()},
            }, None
        if method == "tools/call":
            params = message.get("params") or {}
            name = str(params.get("name") or "")
            arguments = params.get("arguments") or {}
            if name in ("explain_jobs", "submit_explanation"):
                return self._job_tool_response(message.get("id"), name, arguments), None
            if name == "explain_function":
                return self._explain_function_tool(
                    message.get("id"), arguments, session_id
                ), None
        if message.get("method") is None and "id" in message:
            # JSON-RPC response from the client (sampling/createMessage answer).
            result = message.get("result")
            error = message.get("error")
            self.mcp_sessions.respond(
                session_id,
                str(message.get("id")),
                result if isinstance(result, dict) else None,
                error if isinstance(error, dict) else None,
            )
            return None, None
        return self.mcp.handle(message), None

    def _job_tool_response(
        self, request_id: Any, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            if name == "explain_jobs":
                data = self._explain_jobs_tool(arguments)
            else:
                data = self._submit_explanation_tool(arguments)
            envelope = {"ok": True, "tool": name, "data": data}
        except ServeError as exc:
            envelope = {
                "ok": False,
                "tool": name,
                "error": {"code": exc.code, "message": exc.message},
            }
        except Exception as exc:  # never leak tracebacks to agents
            envelope = {
                "ok": False,
                "tool": name,
                "error": {"code": "INTERNAL", "message": f"{type(exc).__name__}: {exc}"},
            }
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [{"type": "text", "text": json.dumps(envelope, ensure_ascii=False)}],
                "isError": not envelope["ok"],
            },
        }

    def _explain_jobs_tool(self, arguments: dict[str, Any]) -> dict[str, Any]:
        action = str(arguments.get("action") or "list")
        kind = str(arguments.get("kind") or "explain")
        if action == "claim":
            worker = str(arguments.get("worker") or "mcp:agent")
            job = self.jobs.claim_job(
                kind,
                claimed_by=worker,
                lease_seconds=int(arguments.get("lease_seconds") or 120),
            )
            if job is not None:
                self._cancel_auto_explain(str(job.get("id") or ""))
            return {"action": "claim", "job": job, "worker": worker}
        status = arguments.get("status")
        limit = int(arguments.get("limit") or 50)
        jobs = self.jobs.list_jobs(
            kind=kind, status=str(status) if status else None, limit=limit
        )
        return {"action": "list", "items": jobs, "count": len(jobs)}

    @staticmethod
    def _agent_result(result: dict[str, Any], source: str = "agent") -> dict[str, Any]:
        """Add provider metadata so the UI can render heuristic/llm/agent badges."""

        enriched = dict(result)
        enriched.setdefault("provider", source)
        if enriched.get("provider") == "agent":
            enriched.setdefault("model", "mcp-agent")
        enriched.setdefault("elapsed_ms", 0)
        return enriched

    def _submit_explanation_tool(self, arguments: dict[str, Any]) -> dict[str, Any]:
        job_id = str(arguments.get("job_id") or "").strip()
        if not job_id:
            raise ServeError("INVALID_ARGUMENT", "job_id is required")
        explanation = arguments.get("explanation")
        if not isinstance(explanation, str) or not explanation.strip():
            raise ServeError("INVALID_ARGUMENT", "explanation is required")
        result = self._agent_result(
            {
                "explanation": explanation,
                **({"evidence": arguments["evidence"]} if "evidence" in arguments else {}),
            }
        )
        worker = str(arguments.get("worker") or "mcp:agent")
        job = self.jobs.get_job(job_id)
        if job is None:
            raise ServeError("NOT_FOUND", f"job not found: {job_id}")
        if job.get("status") == "queued":
            claimed = self.jobs.claim_job(
                str(job.get("kind") or "") or None, claimed_by=worker
            )
            if claimed is not None and claimed.get("id") == job_id:
                job = claimed
        self._cancel_auto_explain(job_id)
        if job.get("status") != "running":
            raise ServeError(
                "CONFLICT", f"job {job_id} is {job.get('status')}, expected running"
            )
        claimed_by = str(job.get("claimed_by") or "") or None
        if not self.jobs.complete_job(job_id, result, claimed_by=claimed_by):
            raise ServeError("CONFLICT", f"job {job_id} could not be completed")
        return {"job": self.jobs.get_job(job_id), "worker": worker}
