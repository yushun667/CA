"""Service state shared by the ``ca serve`` HTTP and MCP endpoints."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ca.cli.dbsource import resolve_paths
from ca.cli.mcp import McpSession, TOOLS as MCP_TOOLS
from ca.core.schema import TOOL_VERSION
from ca.server.jobs import JobService
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

    # -- lifecycle ------------------------------------------------------- #
    def close(self) -> None:
        self.jobs.close()

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
        return list(MCP_TOOLS) + list(JOB_MCP_TOOLS)

    def handle_mcp(self, message: Any) -> dict[str, Any] | None:
        if not isinstance(message, dict):
            raise ServeError("INVALID_ARGUMENT", "expected a JSON-RPC object")
        method = message.get("method")
        if method == "tools/list":
            request_id = message.get("id")
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"tools": self.mcp_tools()},
            }
        if method == "tools/call":
            params = message.get("params") or {}
            name = str(params.get("name") or "")
            arguments = params.get("arguments") or {}
            if name in ("explain_jobs", "submit_explanation"):
                return self._job_tool_response(message.get("id"), name, arguments)
        return self.mcp.handle(message)

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
        if job.get("status") != "running":
            raise ServeError(
                "CONFLICT", f"job {job_id} is {job.get('status')}, expected running"
            )
        claimed_by = str(job.get("claimed_by") or "") or None
        if not self.jobs.complete_job(job_id, result, claimed_by=claimed_by):
            raise ServeError("CONFLICT", f"job {job_id} could not be completed")
        return {"job": self.jobs.get_job(job_id), "worker": worker}
