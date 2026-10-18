"""Thin server adapter over the storage-owned job queue.

The canonical implementation lives in :mod:`ca.store.jobs` (frozen API v1,
``JobQueue``): the service opens a **separate writable state database**
(``<db-dir>/.ca/jobs.db`` by default) so shard indexes stay read-only.  This
module only owns the in-process event bus used by ``/events`` SSE streams and
the small glue that publishes newly appended store events to subscribers.
"""

from __future__ import annotations

import queue
import threading
from pathlib import Path
from typing import Any

try:  # storage-engine owns the canonical implementation (frozen API v1)
    from ca.store.jobs import JobQueue
except ImportError:  # pragma: no cover - until the store module lands
    JobQueue = None  # type: ignore[assignment]


class EventBus:
    """In-process fan-out for SSE subscribers (history is replayed from store)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: set[queue.Queue] = set()
        self._last_id = 0

    def subscribe(self) -> "EventSubscription":
        item: queue.Queue = queue.Queue(maxsize=512)
        with self._lock:
            self._subscribers.add(item)
        return EventSubscription(self, item)

    def _unsubscribe(self, item: queue.Queue) -> None:
        with self._lock:
            self._subscribers.discard(item)

    def publish(self, event: dict[str, Any]) -> None:
        event_id = int(event.get("id") or 0)
        with self._lock:
            self._last_id = max(self._last_id, event_id)
            subscribers = list(self._subscribers)
        for item in subscribers:
            try:
                item.put_nowait(event)
            except queue.Full:  # slow client: drop; SSE replays from the store
                pass

    def last_id(self) -> int:
        with self._lock:
            return self._last_id


class EventSubscription:
    def __init__(self, bus: EventBus, queue: queue.Queue) -> None:
        self.bus = bus
        self.queue = queue

    def get(self, timeout: float | None = None) -> dict[str, Any] | None:
        try:
            return self.queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self) -> None:
        self.bus._unsubscribe(self.queue)


class JobService:
    """Job queue + lifecycle glue used by the HTTP/MCP layer.

    Each server thread gets its own :class:`JobQueue` connection (sqlite3
    connections are not shareable across threads); claim/complete stay atomic
    because every operation is a single ``BEGIN IMMEDIATE`` transaction.
    """

    def __init__(self, state_db_path: str | Path) -> None:
        if JobQueue is None:  # pragma: no cover - early bring-up
            raise RuntimeError("job queue storage is not available yet")
        self.path = str(Path(state_db_path).expanduser())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.bus = EventBus()
        self._publish_lock = threading.Lock()
        self._local = threading.local()
        self._queues: list[Any] = []
        self._queues_lock = threading.Lock()
        # Open once in the owning thread so schema/state is ready immediately.
        self._queue()

    def _queue(self) -> Any:
        queue = getattr(self._local, "queue", None)
        if queue is None:
            queue = JobQueue.open(self.path)
            self._local.queue = queue
            with self._queues_lock:
                self._queues.append(queue)
        return queue

    @property
    def queue(self) -> Any:
        return self._queue()

    # -- lifecycle ------------------------------------------------------- #
    def close(self) -> None:
        with self._queues_lock:
            queues = list(self._queues)
            self._queues.clear()
        for queue in queues:
            try:
                queue.close()
            except Exception:  # connection owned by another (dead) thread
                pass
        self._local = threading.local()

    def recover(self) -> int:
        recovered = self._queue().recover_running()
        self.publish_new_events()
        return recovered

    def publish_new_events(self) -> list[dict[str, Any]]:
        """Forward every store event appended since the last publish."""

        published: list[dict[str, Any]] = []
        with self._publish_lock:
            while True:
                events = self._queue().list_job_events(
                    since_id=self.bus.last_id(), limit=100
                )
                if not events:
                    return published
                for event in events:
                    self.bus.publish(event)
                    published.append(event)

    # -- queue API (thin delegates; every mutation publishes new events) -- #
    def create_job(self, kind: str, payload: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        job_id = self._queue().create_job(kind, payload, **kwargs)
        self.publish_new_events()
        return self.get_job(job_id) or {"id": job_id}

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        return self._queue().get_job(job_id)

    def list_jobs(self, **kwargs: Any) -> list[dict[str, Any]]:
        return self._queue().list_jobs(**kwargs)

    def claim_job(self, kind: str | None = None, *, claimed_by: str = "", lease_seconds: int = 60) -> dict[str, Any] | None:
        job = self._queue().claim_job(
            kind, claimed_by=claimed_by, lease_seconds=lease_seconds
        )
        self.publish_new_events()
        return job

    def complete_job(self, job_id: str, result: dict[str, Any] | None = None, **kwargs: Any) -> bool:
        ok = self._queue().complete_job(job_id, result, **kwargs)
        self.publish_new_events()
        return ok

    def fail_job(self, job_id: str, error: str = "", **kwargs: Any) -> bool:
        ok = self._queue().fail_job(job_id, error, **kwargs)
        self.publish_new_events()
        return ok

    def requeue_job(self, job_id: str, **kwargs: Any) -> bool:
        ok = self._queue().requeue_job(job_id, **kwargs)
        self.publish_new_events()
        return ok

    def job_stats(self) -> dict[str, Any]:
        return self._queue().job_stats()

    def list_job_events(self, **kwargs: Any) -> list[dict[str, Any]]:
        return self._queue().list_job_events(**kwargs)

    def latest_job_event_id(self) -> int:
        return self._queue().latest_job_event_id()
