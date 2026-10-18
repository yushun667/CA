"""State shared by the ``ca serve`` HTTP and MCP endpoints.

The service owns read-only query access (single DB or federated shards), the
durable generic job queue state DB and one stateless MCP protocol handler for
``/mcp``.  There is no AI explain endpoint, sampling session or agent-worker:
jobs are generic background tasks consumed through ``/api/jobs``/``/events``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ca.cli.mcp import TOOLS as MCP_TOOLS
from ca.cli.mcp import McpSession
from ca.core.schema import TOOL_VERSION
from ca.server.jobs import JobService
from ca.ui.server import QueryService, resolve_service


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
    """Owns query access, the generic job queue and the stateless MCP handler."""

    def __init__(self, config: ServeConfig) -> None:
        self.config = config
        self.query: QueryService = resolve_service(config.db_values, config.db_dir)
        self.paths = list(self.query.paths)
        self.federated = bool(self.query.federated)
        self.started_at = time.time()
        self.state_db = self._resolve_state_db()
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

    def _resolve_state_db(self) -> str:
        if self.config.state_db:
            return str(Path(self.config.state_db).expanduser())
        if self.config.db_dir:
            return str(Path(self.config.db_dir).expanduser() / ".ca" / "jobs.db")
        if len(self.paths) == 1:
            path = Path(self.paths[0]).expanduser()
            return str(path.with_name(path.stem + ".serve-state.db"))
        return str(Path("ca-serve-state.db"))

    def close(self) -> None:
        self.jobs.close()

    # -- health ---------------------------------------------------------- #
    def health(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "server": "ca-serve",
            "version": TOOL_VERSION,
            "started_at": time.strftime(
                "%Y-%m-%dT%H:%M:%S%z", time.localtime(self.started_at)
            ),
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

    # -- generic jobs ---------------------------------------------------- #
    def create_job(self, body: dict[str, Any]) -> dict[str, Any]:
        kind = str(body.get("kind") or body.get("type") or "").strip()
        if not kind:
            raise ServeError("INVALID_ARGUMENT", "job 'kind' is required")
        payload = body.get("payload")
        if payload is None:
            payload = {}
        elif not isinstance(payload, dict):
            raise ServeError("INVALID_ARGUMENT", "job 'payload' must be an object")
        return self.jobs.create_job(kind, dict(payload))

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        return self.jobs.get_job(job_id)

    def list_jobs(
        self,
        *,
        kind: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
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
        return self.jobs.claim_job(
            str(kind) if kind else None, claimed_by=worker, lease_seconds=lease
        )

    def submit_job_result(self, job_id: str, body: dict[str, Any]) -> dict[str, Any]:
        job = self.jobs.get_job(job_id)
        if job is None:
            raise ServeError("NOT_FOUND", f"job not found: {job_id}")
        result = body.get("result")
        if result is None:
            result = {key: body[key] for key in ("summary", "output") if key in body}
        if not isinstance(result, dict):
            raise ServeError("INVALID_ARGUMENT", "job 'result' must be an object")
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
        claimed_by = str(job.get("claimed_by") or "") or None
        if not self.jobs.complete_job(job_id, result, claimed_by=claimed_by):
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

    # -- stateless MCP /mcp ---------------------------------------------- #
    def handle_mcp(self, message: Any) -> dict[str, Any] | None:
        """Handle one stateless MCP JSON-RPC message (18 tools)."""

        if not isinstance(message, dict):
            raise ServeError("INVALID_ARGUMENT", "expected a JSON-RPC object")
        method = message.get("method")
        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": message.get("id"),
                "result": {"tools": list(MCP_TOOLS)},
            }
        return self.mcp.handle(message)
