"""Persistent job queue and event log for the unified CA backend.

The ``ca serve`` backend runs generic background analysis tasks (``ensure_deep``,
``prewarm``, ``update``, ...) as durable *jobs* shared by the HTTP API, the MCP
endpoint and the Web UI. Jobs and their state transitions are persisted in
SQLite so a client can disconnect/reconnect, an agent can claim work over MCP,
and the service can recover after a restart.

Two tables (created by ``schema.sql`` and therefore present right after
``SqliteStore.open()``, without ``finalize()``):

- ``jobs``: one row per task. Status is one of ``queued``, ``running``,
  ``done``, ``failed``. Workers claim a queued job atomically with a lease
  (``claimed_by`` + ``lease_until``); attempts are counted at claim time.
  Expired leases are requeued automatically by :meth:`JobQueue.claim_job` so a
  crashed worker cannot strand a job.
- ``job_events``: append-only, globally monotonically numbered event log
  (``created`` / ``claimed`` / ``done`` / ``failed`` / ``requeued``). SSE
  clients reconnect with ``since_id`` (``Last-Event-ID``) and replay the missed
  frames; :meth:`JobQueue.prune_job_events` keeps the last N events.

Backends: :class:`JobQueue` works on any SQLite connection.
:meth:`JobQueue.open` opens (and creates/upgrades) a standalone state database —
recommended for ``ca serve`` so the shard/index databases stay read-only. The
same API is also exposed on :class:`ca.store.SqliteStore` (tables in the index
database) for single-file setups and tests.

All writes are serialized by SQLite (one writer). ``claim``/``complete``/
``fail``/``requeue`` run in ``BEGIN IMMEDIATE`` transactions, so two workers
using different connections/threads can race safely: exactly one wins a job.
The connection itself is not thread-safe; use one writer thread or one
:class:`JobQueue` per thread (WAL + ``busy_timeout`` handle contention).

Returned dict keys are the CLI/HTTP/MCP contract:

- job: ``id, kind, status, payload, result, error, created_at, updated_at,
  claimed_by, lease_until, attempts`` (``payload``/``result`` decoded JSON;
  ``result`` is ``None`` until completed).
- event: ``id, job_id, type, data, created_at`` (``data`` decoded JSON, normally
  ``{"job": <job dict>}``).
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from ca.core.schema import SCHEMA_VERSION
from ca.store import writer

JOB_STATUSES = ("queued", "running", "done", "failed")
EVENT_TYPES = ("created", "claimed", "done", "failed", "requeued")
DEFAULT_LEASE_SECONDS = 60
_JOB_ID_PREFIX = "job:"

JOB_COLUMNS = (
    "id",
    "kind",
    "status",
    "payload_json",
    "result_json",
    "error",
    "created_at",
    "updated_at",
    "claimed_by",
    "lease_until",
    "attempts",
)
_EVENT_COLUMNS = ("id", "job_id", "type", "data_json", "created_at")

_SCHEMA_FILE = Path(__file__).with_name("schema.sql")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _now_plus(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(
        timespec="microseconds"
    )


def _encode_json(value: Any, *, empty: str = "{}") -> str:
    if value is None:
        return empty
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decode_json(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _job_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["payload"] = _decode_json(data.pop("payload_json", None), {})
    data["result"] = _decode_json(data.pop("result_json", None), None)
    data["attempts"] = int(data.get("attempts", 0))
    return data


def _event_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["data"] = _decode_json(data.pop("data_json", None), {})
    data["id"] = int(data["id"])
    return data


def _validate_status(status: str | None) -> None:
    if status is not None and status not in JOB_STATUSES:
        raise ValueError(f"unknown job status: {status!r}")


class JobQueue:
    """Durable job queue + event log (see module docstring for the contract)."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection
        self._owns_connection = False

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    @classmethod
    def open(cls, path: str | Path) -> "JobQueue":
        """Open/create a standalone state database (WAL) and return a queue.

        Applies the full schema idempotently (the same ``schema.sql`` as
        :class:`ca.store.SqliteStore`), so an empty/new file is ready for job
        operations immediately — no ``finalize()`` call is required.
        """

        state_path = Path(path).expanduser()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(state_path), timeout=30.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA temp_store=MEMORY")
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.executescript(_SCHEMA_FILE.read_text(encoding="utf-8"))
        connection.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
            (SCHEMA_VERSION,),
        )
        connection.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (writer.STORE_SCHEMA_META_KEY, writer.STORE_SCHEMA_VERSION),
        )
        queue = cls(connection)
        queue._owns_connection = True
        return queue

    def close(self) -> None:
        if self._owns_connection and self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None  # type: ignore[assignment]
                self._owns_connection = False

    def __enter__(self) -> "JobQueue":
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    @contextmanager
    def _write(self) -> Iterator[None]:
        """``BEGIN IMMEDIATE`` unless the caller already opened a transaction."""

        connection = self._conn
        started = False
        if not connection.in_transaction:
            connection.execute("BEGIN IMMEDIATE")
            started = True
        try:
            yield
        except BaseException:
            if started:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise
        else:
            if started:
                connection.execute("COMMIT")

    # ------------------------------------------------------------------ #
    # Event log (SSE replay)
    # ------------------------------------------------------------------ #
    def _append_event(self, job_id: str, event_type: str, data: Any) -> int:
        cursor = self._conn.execute(
            "INSERT INTO job_events (job_id, type, data_json, created_at) VALUES (?, ?, ?, ?)",
            (job_id, event_type, _encode_json(data), _now()),
        )
        return int(cursor.lastrowid or 0)

    def append_job_event(self, job_id: str, event_type: str, data: Any = None) -> int:
        """Append one event (for custom server/client progress events)."""

        if event_type not in EVENT_TYPES:
            raise ValueError(f"unknown event type: {event_type!r}")
        with self._write():
            return self._append_event(str(job_id), event_type, data or {})

    def list_job_events(
        self,
        *,
        since_id: int = 0,
        job_id: str | None = None,
        limit: int = 100,
        types: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Events with ``id > since_id`` in ascending id order (SSE replay)."""

        if limit <= 0:
            return []
        where = ["id > ?"]
        params: list[Any] = [int(since_id)]
        if job_id is not None:
            where.append("job_id = ?")
            params.append(str(job_id))
        if types:
            unknown = [item for item in types if item not in EVENT_TYPES]
            if unknown:
                raise ValueError(f"unknown event types: {unknown}")
            marks = ", ".join("?" for _ in types)
            where.append(f"type IN ({marks})")
            params.extend(types)
        sql = (
            f"SELECT {', '.join(_EVENT_COLUMNS)} FROM job_events "
            f"WHERE {' AND '.join(where)} ORDER BY id LIMIT ?"
        )
        params.append(int(limit))
        return [_event_row(row) for row in self._conn.execute(sql, params)]

    def latest_job_event_id(self) -> int:
        row = self._conn.execute("SELECT COALESCE(MAX(id), 0) FROM job_events").fetchone()
        return int(row[0])

    def prune_job_events(self, keep_last: int = 10000) -> int:
        """Keep only the newest ``keep_last`` events; returns rows deleted."""

        if keep_last <= 0:
            return 0
        cutoff_row = self._conn.execute(
            "SELECT id FROM job_events ORDER BY id DESC LIMIT 1 OFFSET ?",
            (int(keep_last) - 1,),
        ).fetchone()
        if cutoff_row is None:
            return 0
        cutoff = int(cutoff_row[0])
        with self._write():
            cursor = self._conn.execute("DELETE FROM job_events WHERE id < ?", (cutoff,))
            return int(cursor.rowcount or 0)

    # ------------------------------------------------------------------ #
    # Job lifecycle
    # ------------------------------------------------------------------ #
    def create_job(
        self,
        kind: str,
        payload: Any = None,
        *,
        job_id: str | None = None,
    ) -> str:
        """Create a queued job; returns its id (``job:`` + uuid by default)."""

        kind_value = str(kind or "").strip()
        if not kind_value:
            raise ValueError("job kind must not be empty")
        identifier = str(job_id).strip() if job_id else _JOB_ID_PREFIX + uuid.uuid4().hex
        if not identifier:
            raise ValueError("job id must not be empty")
        now = _now()
        with self._write():
            try:
                self._conn.execute(
                    "INSERT INTO jobs (id, kind, status, payload_json, result_json, error, "
                    "created_at, updated_at, claimed_by, lease_until, attempts) "
                    "VALUES (?, ?, 'queued', ?, '', '', ?, ?, '', '', 0)",
                    (identifier, kind_value, _encode_json(payload, empty="{}"), now, now),
                )
            except sqlite3.IntegrityError:
                raise ValueError(f"job already exists: {identifier}") from None
            job = self._fetch_job(identifier)
            self._append_event(identifier, "created", {"job": job})
        return identifier

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            f"SELECT {', '.join(JOB_COLUMNS)} FROM jobs WHERE id = ?", (str(job_id),)
        ).fetchone()
        return _job_row(row) if row is not None else None

    def list_jobs(
        self,
        *,
        kind: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Jobs filtered by kind/status, ordered by ``(created_at, id)``."""

        _validate_status(status)
        if limit <= 0:
            return []
        where: list[str] = []
        params: list[Any] = []
        if kind is not None:
            where.append("kind = ?")
            params.append(str(kind))
        if status is not None:
            where.append("status = ?")
            params.append(status)
        sql = f"SELECT {', '.join(JOB_COLUMNS)} FROM jobs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at, id LIMIT ? OFFSET ?"
        params.extend([int(limit), max(0, int(offset))])
        return [_job_row(row) for row in self._conn.execute(sql, params)]

    def _fetch_job(self, job_id: str) -> dict[str, Any] | None:
        return self.get_job(job_id)

    def _expire_leases(self) -> list[dict[str, Any]]:
        """Requeue running jobs whose lease expired; returns the affected jobs."""

        now = _now()
        rows = self._conn.execute(
            f"SELECT {', '.join(JOB_COLUMNS)} FROM jobs "
            "WHERE status = 'running' AND lease_until != '' AND lease_until < ?",
            (now,),
        ).fetchall()
        requeued: list[dict[str, Any]] = []
        for row in rows:
            job_id = row["id"]
            self._conn.execute(
                "UPDATE jobs SET status = 'queued', claimed_by = '', lease_until = '', "
                "updated_at = ? WHERE id = ? AND status = 'running'",
                (now, job_id),
            )
            job = self._fetch_job(job_id)
            if job is not None:
                self._append_event(job_id, "requeued", {"job": job, "reason": "lease_expired"})
                requeued.append(job)
        return requeued

    def requeue_expired(self) -> int:
        """Requeue jobs whose lease expired; returns the number requeued."""

        with self._write():
            return len(self._expire_leases())

    def recover_running(self) -> int:
        """Requeue every running job (service startup recovery); returns count."""

        with self._write():
            rows = self._conn.execute(
                f"SELECT {', '.join(JOB_COLUMNS)} FROM jobs WHERE status = 'running'"
            ).fetchall()
            now = _now()
            for row in rows:
                self._conn.execute(
                    "UPDATE jobs SET status = 'queued', claimed_by = '', lease_until = '', "
                    "updated_at = ? WHERE id = ? AND status = 'running'",
                    (now, row["id"]),
                )
                job = self._fetch_job(row["id"])
                if job is not None:
                    self._append_event(
                        row["id"], "requeued", {"job": job, "reason": "service_restart"}
                    )
            return len(rows)

    def claim_job(
        self,
        kind: str | None = None,
        *,
        claimed_by: str = "",
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> dict[str, Any] | None:
        """Atomically claim the oldest queued job (optionally of one kind).

        Expired running leases are requeued first. Returns the running job dict
        or ``None`` when no job is available. ``attempts`` is incremented and a
        ``claimed`` event is appended in the same transaction.
        """

        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        now = _now()
        lease_until = _now_plus(lease_seconds)
        sub_where = "status = 'queued'"
        sub_params: list[Any] = []
        if kind is not None:
            sub_where += " AND kind = ?"
            sub_params.append(str(kind))
        with self._write():
            self._expire_leases()
            row = self._conn.execute(
                "UPDATE jobs SET status = 'running', claimed_by = ?, lease_until = ?, "
                "attempts = attempts + 1, updated_at = ? "
                f"WHERE id = (SELECT id FROM jobs WHERE {sub_where} "
                "ORDER BY created_at, id LIMIT 1) RETURNING id",
                (str(claimed_by), lease_until, now, *sub_params),
            ).fetchone()
            if row is None:
                return None
            job = self._fetch_job(row["id"])
            if job is None:  # pragma: no cover - row cannot disappear in this txn
                return None
            self._append_event(row["id"], "claimed", {"job": job, "worker": str(claimed_by)})
            return job

    def renew_job_lease(
        self,
        job_id: str,
        *,
        claimed_by: str = "",
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> bool:
        """Extend a running job's lease (heartbeat); False if not owned/running."""

        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        with self._write():
            where = "id = ? AND status = 'running'"
            params: list[Any] = [str(job_id)]
            if claimed_by:
                where += " AND claimed_by = ?"
                params.append(str(claimed_by))
            row = self._conn.execute(
                f"UPDATE jobs SET lease_until = ?, updated_at = ? WHERE {where} RETURNING id",
                (_now_plus(lease_seconds), _now(), *params),
            ).fetchone()
            return row is not None

    def _finish(
        self,
        job_id: str,
        *,
        status: str,
        result: Any = None,
        error: str = "",
        claimed_by: str | None,
        event_type: str,
        event_extra: Mapping[str, Any] | None = None,
    ) -> bool:
        where = "id = ? AND status = 'running'"
        params: list[Any] = [str(job_id)]
        if claimed_by is not None:
            where += " AND claimed_by = ?"
            params.append(str(claimed_by))
        with self._write():
            row = self._conn.execute(
                "UPDATE jobs SET status = ?, result_json = ?, error = ?, updated_at = ?, "
                f"lease_until = '' WHERE {where} RETURNING id",
                (
                    status,
                    _encode_json(result, empty="") if result is not None else "",
                    str(error or ""),
                    _now(),
                    *params,
                ),
            ).fetchone()
            if row is None:
                return False
            job = self._fetch_job(str(job_id))
            data: dict[str, Any] = {"job": job}
            if event_extra:
                data.update(event_extra)
            self._append_event(str(job_id), event_type, data)
            return True

    def complete_job(
        self,
        job_id: str,
        result: Any = None,
        *,
        claimed_by: str | None = None,
    ) -> bool:
        """Finish a running job successfully; False if not running/owned."""

        return self._finish(
            job_id,
            status="done",
            result=result,
            claimed_by=claimed_by,
            event_type="done",
        )

    def fail_job(
        self,
        job_id: str,
        error: str = "",
        *,
        claimed_by: str | None = None,
    ) -> bool:
        """Mark a running job failed; False if not running/owned."""

        return self._finish(
            job_id,
            status="failed",
            error=error,
            claimed_by=claimed_by,
            event_type="failed",
            event_extra={"error": str(error or "")},
        )

    def requeue_job(
        self,
        job_id: str,
        *,
        error: str = "",
        claimed_by: str | None = None,
    ) -> bool:
        """Return a running job to the queue (attempts are preserved)."""

        where = "id = ? AND status = 'running'"
        params: list[Any] = [str(job_id)]
        if claimed_by is not None:
            where += " AND claimed_by = ?"
            params.append(str(claimed_by))
        with self._write():
            row = self._conn.execute(
                "UPDATE jobs SET status = 'queued', claimed_by = '', lease_until = '', "
                f"updated_at = ?, error = ? WHERE {where} RETURNING id",
                (_now(), str(error or ""), *params),
            ).fetchone()
            if row is None:
                return False
            job = self._fetch_job(str(job_id))
            self._append_event(str(job_id), "requeued", {"job": job, "error": str(error or "")})
            return True

    # ------------------------------------------------------------------ #
    # Aggregates
    # ------------------------------------------------------------------ #
    def count_jobs(self, *, kind: str | None = None, status: str | None = None) -> int:
        _validate_status(status)
        where: list[str] = []
        params: list[Any] = []
        if kind is not None:
            where.append("kind = ?")
            params.append(str(kind))
        if status is not None:
            where.append("status = ?")
            params.append(status)
        sql = "SELECT COUNT(*) FROM jobs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        return int(self._conn.execute(sql, params).fetchone()[0])

    def job_stats(self) -> dict[str, Any]:
        by_status = {status: 0 for status in JOB_STATUSES}
        for row in self._conn.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status"):
            if row[0] in by_status:
                by_status[row[0]] = int(row[1])
        by_kind = {
            str(row[0]): int(row[1])
            for row in self._conn.execute(
                "SELECT kind, COUNT(*) FROM jobs GROUP BY kind ORDER BY kind"
            )
        }
        return {
            "total": sum(by_status.values()),
            "by_status": by_status,
            "by_kind": by_kind,
        }


# Backwards-compatible alias for clients that guessed an alternative name.
JobStore = JobQueue

__all__ = [
    "DEFAULT_LEASE_SECONDS",
    "EVENT_TYPES",
    "JOB_STATUSES",
    "JobQueue",
    "JobStore",
]
