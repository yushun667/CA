"""State shared by the ``ca serve`` HTTP and MCP endpoints.

The service owns read-only query access (single DB or federated shards), the
durable generic job queue state DB and one stateless MCP protocol handler for
``/mcp``.  There is no AI explain endpoint, sampling session or agent-worker:
jobs are generic background tasks consumed through ``/api/jobs``/``/events``.
"""

from __future__ import annotations

import sqlite3
import threading
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
        self._lock = threading.RLock()
        self.query: QueryService = resolve_service(config.db_values, config.db_dir)
        self.paths = list(self.query.paths)
        self.federated = bool(self.query.federated)
        # The registry is fixed to the startup database: switching an active
        # variant must not make GET /api/registry read a different (possibly
        # empty) registry from the new variant DB.
        self.registry_path: str = self.paths[0] if self.paths else ""
        self.active_variant: dict[str, Any] | None = None
        self._detect_initial_active()
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
            "variant": self.active_variant,
            "registry_source": self.registry_path,
            "jobs": self.jobs.job_stats(),
            "mcp_endpoint": "/mcp",
            "events_endpoint": "/events",
        }

    # -- workspace registry + active variant ---------------------------- #
    def _read_registry(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        """Read workspaces/projects/variants from the fixed registry DB."""

        from ca.store.workspace import WorkspaceRegistry

        path = self.registry_path
        if not path or not Path(path).expanduser().is_file():
            return [], [], []
        connection = sqlite3.connect(f"file:{Path(path).expanduser()}?mode=ro", uri=True, timeout=5.0)
        connection.row_factory = sqlite3.Row
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if not {"workspaces", "projects", "variants"} <= tables:
                return [], [], []
            registry = WorkspaceRegistry(connection)
            workspaces = registry.list_workspaces()
            projects: list[dict[str, Any]] = []
            variants: list[dict[str, Any]] = []
            base = Path(path).expanduser().resolve().parent
            for workspace in workspaces:
                projects.extend(registry.list_projects(workspace["id"]))
                for variant in registry.list_variants(workspace["id"]):
                    raw = str(variant.get("db_path") or "").strip()
                    target = Path(raw).expanduser() if raw else None
                    if target is not None and not target.is_absolute():
                        target = base / target
                    item = dict(variant)
                    item["db_exists"] = bool(target and target.is_file())
                    variants.append(item)
            return workspaces, projects, variants
        except sqlite3.Error:
            return [], [], []
        finally:
            connection.close()

    def _detect_initial_active(self) -> None:
        """Mark a registered variant active when it points at the startup DB."""

        current = {
            str(Path(path).expanduser().resolve()) for path in self.paths
        }
        if not current:
            return
        try:
            _workspaces, _projects, variants = self._read_registry()
        except Exception:
            return
        base = Path(self.registry_path).expanduser().resolve().parent
        for variant in variants:
            raw = str(variant.get("db_path") or "").strip()
            if not raw:
                continue
            target = Path(raw).expanduser()
            if not target.is_absolute():
                target = base / target
            if str(target.resolve()) in current:
                self.active_variant = dict(variant)
                return

    def registry(self) -> dict[str, Any]:
        """Registry snapshot used by the UI selector (read-only, fixed source)."""

        with self._lock:
            workspaces, projects, variants = self._read_registry()
            active = dict(self.active_variant) if self.active_variant else None
            return {
                "workspaces": workspaces,
                "projects": projects,
                "variants": variants,
                "active_variant": active,
                "active_variant_id": (active or {}).get("id"),
                "registry_source": self.registry_path,
                "registry_db": self.registry_path,
                "available": bool(workspaces or projects or variants),
                "database": {"paths": list(self.paths), "federated": self.federated},
            }

    def set_active_variant(self, variant_identifier: Any) -> dict[str, Any]:
        """Switch query/MCP to a registered variant DB, atomically and read-only.

        The new :class:`QueryService` is opened and exercised before the swap;
        failures leave the previous database/session untouched. The switch is
        in-memory only (no registry row or index DB is written).
        """

        selector = str(variant_identifier or "").strip()
        if not selector:
            raise ServeError("INVALID_ARGUMENT", "variant_id is required")
        with self._lock:
            _workspaces, _projects, variants = self._read_registry()
            matches = [
                variant
                for variant in variants
                if variant["id"] == selector or variant["name"] == selector
            ]
            if not matches:
                raise ServeError("NOT_FOUND", f"variant not found: {selector}")
            if len(matches) > 1:
                ids = ", ".join(variant["id"] for variant in matches)
                raise ServeError(
                    "INVALID_ARGUMENT",
                    f"variant name {selector!r} is ambiguous; use one of: {ids}",
                )
            variant = matches[0]
            raw_path = str(variant.get("db_path") or "").strip()
            if not raw_path:
                raise ServeError(
                    "DB_UNAVAILABLE",
                    f"variant {variant['id']} has no db_path; build its index first",
                )
            target = Path(raw_path).expanduser()
            if not target.is_absolute():
                target = Path(self.registry_path).expanduser().resolve().parent / target
            target = target.resolve()
            if not target.is_file():
                raise ServeError(
                    "DB_UNAVAILABLE",
                    f"variant database not found for {variant['id']}: {target}",
                )
            candidate = QueryService([str(target)], federated=False)
            try:
                with candidate.open() as store:
                    store.stats()
            except Exception as exc:
                raise ServeError(
                    "DB_UNAVAILABLE",
                    f"cannot read variant database {target}: {type(exc).__name__}: {exc}",
                ) from exc
            active = dict(variant)
            active["db_path"] = str(target)
            active["db_exists"] = True
            self.query = candidate
            self.paths = [str(target)]
            self.federated = False
            self.active_variant = active
            self.mcp = McpSession(
                self.paths,
                repo=self.config.repo,
                compdb=self.config.compdb,
                federated=False,
            )
            return {
                "active_variant": active,
                "variant": active,
                "active_variant_id": active["id"],
                "database": {"paths": list(self.paths), "federated": False},
                "health": self.health(),
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
        """Handle one stateless MCP JSON-RPC message (21 tools)."""

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
