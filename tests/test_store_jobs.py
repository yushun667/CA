"""Persistent job queue + event log (unified backend / ``ca serve`` state DB).

Covers create/get/list, atomic claim with leases, complete/fail/requeue
ownership, expired-lease recovery, restart recovery, event replay for SSE
(``since_id``), pruning, stats, concurrent claim serialization, old-database
upgrade and the ``SqliteStore`` delegation.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from ca.core.schema import SCHEMA_VERSION
from ca.store import JobQueue, SqliteStore
from ca.store.jobs import JOB_STATUSES

JOB_KEYS = {
    "id",
    "kind",
    "status",
    "payload",
    "result",
    "error",
    "created_at",
    "updated_at",
    "claimed_by",
    "lease_until",
    "attempts",
}
EVENT_KEYS = {"id", "job_id", "type", "data", "created_at"}


@pytest.fixture()
def queue(tmp_path):
    instance = JobQueue.open(tmp_path / "state.db")
    yield instance
    instance.close()


def test_state_database_open_creates_v5_tables(tmp_path) -> None:
    path = tmp_path / "fresh" / "state.db"
    queue = JobQueue.open(path)
    try:
        tables = {
            row[0]
            for row in queue._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {"jobs", "job_events", "meta"} <= tables
        indexes = {
            row[0]
            for row in queue._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL"
            )
        }
        assert {
            "idx_jobs_status_created",
            "idx_jobs_kind_status_created",
            "idx_jobs_status_lease",
            "idx_job_events_job",
        } <= indexes
        meta = dict(queue._conn.execute("SELECT key, value FROM meta"))
        assert meta["store_schema_version"] == "8"
        assert meta["schema_version"] == SCHEMA_VERSION
    finally:
        queue.close()
    # reopen keeps existing state (idempotent schema)
    queue = JobQueue.open(path)
    try:
        job_id = queue.create_job("batch", {"symbol": "x"})
        assert queue.get_job(job_id)["status"] == "queued"
    finally:
        queue.close()


def test_sqlite_store_state_database_without_finalize(tmp_path) -> None:
    store = SqliteStore(tmp_path / "state.db")
    store.open()
    try:
        job_id = store.create_job("batch", {"symbol": "proc_alloc"})
        assert store.get_job(job_id)["kind"] == "batch"
        claimed = store.claim_job(claimed_by="worker-1", lease_seconds=30)
        assert claimed is not None and claimed["id"] == job_id
        assert store.complete_job(job_id, {"text": "ok"}, claimed_by="worker-1")
        assert store.job_stats()["by_status"]["done"] == 1
        assert store.stats()["tables"]["jobs"] == 1
        assert store.stats()["tables"]["job_events"] == 3
        assert store.latest_job_event_id() == 3
    finally:
        store.close()


def test_create_get_list(queue) -> None:
    first = queue.create_job("batch", {"symbol": "alpha", "nested": [1, 2]})
    second = queue.create_job("ensure_deep", None)
    explicit = queue.create_job("update", {"files": ["a.c"]}, job_id="job:custom")
    assert {first, second, explicit} == {job["id"] for job in queue.list_jobs(limit=10)}
    assert first.startswith("job:") and len(first) == len("job:") + 32

    job = queue.get_job(first)
    assert job is not None and set(job) == JOB_KEYS
    assert job["status"] == "queued"
    assert job["payload"] == {"symbol": "alpha", "nested": [1, 2]}
    assert job["result"] is None
    assert job["attempts"] == 0
    assert job["claimed_by"] == "" and job["lease_until"] == ""
    assert job["created_at"] and job["updated_at"]
    assert queue.get_job(second)["payload"] == {}
    assert queue.get_job("job:missing") is None

    listed = queue.list_jobs(kind="batch")
    assert [row["id"] for row in listed] == [first]
    assert [row["id"] for row in queue.list_jobs(status="queued", limit=10)] == [
        first,
        second,
        explicit,
    ]
    assert len(queue.list_jobs(limit=2, offset=1)) == 2
    assert queue.list_jobs(limit=0) == []
    with pytest.raises(ValueError):
        queue.list_jobs(status="bogus")
    with pytest.raises(ValueError):
        queue.create_job("")
    with pytest.raises(ValueError):
        queue.create_job("batch", job_id=first)


def test_claim_is_atomic_and_ordered(queue) -> None:
    first = queue.create_job("batch", {"n": 1})
    second = queue.create_job("batch", {"n": 2})
    claimed = queue.claim_job(claimed_by="w1", lease_seconds=30)
    assert claimed is not None
    assert claimed["id"] == first  # oldest first
    assert claimed["status"] == "running"
    assert claimed["attempts"] == 1
    assert claimed["claimed_by"] == "w1"
    assert claimed["lease_until"] > claimed["updated_at"]
    # A second worker gets the next job, not the running one.
    other = queue.claim_job(claimed_by="w2", lease_seconds=30)
    assert other is not None and other["id"] == second
    assert queue.claim_job(claimed_by="w3", lease_seconds=30) is None
    # Kind filter only claims matching jobs.
    third = queue.create_job("update", {})
    queue.create_job("batch", {})
    filtered = queue.claim_job("update", claimed_by="w4", lease_seconds=30)
    assert filtered is not None and filtered["id"] == third
    assert queue.claim_job(claimed_by="w5", lease_seconds=30)["kind"] == "batch"
    with pytest.raises(ValueError):
        queue.claim_job(claimed_by="w6", lease_seconds=0)


def test_complete_fail_ownership_and_idempotency(queue) -> None:
    job_id = queue.create_job("batch", {"symbol": "x"})
    claimed = queue.claim_job(claimed_by="w1", lease_seconds=30)
    assert claimed is not None
    assert queue.complete_job(job_id, {"answer": 42}, claimed_by="other") is False
    assert queue.complete_job(job_id, {"answer": 42}, claimed_by="w1") is True
    done = queue.get_job(job_id)
    assert done["status"] == "done"
    assert done["result"] == {"answer": 42}
    assert done["error"] == ""
    assert done["lease_until"] == ""
    assert queue.complete_job(job_id, {}, claimed_by="w1") is False
    assert queue.fail_job(job_id, "late") is False  # not running anymore

    failing = queue.create_job("batch", {})
    assert queue.claim_job(claimed_by="w1", lease_seconds=30)["id"] == failing
    assert queue.fail_job(failing, "boom", claimed_by="w2") is False
    assert queue.fail_job(failing, "boom", claimed_by="w1") is True
    failed = queue.get_job(failing)
    assert failed["status"] == "failed"
    assert failed["error"] == "boom"
    assert failed["result"] is None
    assert queue.fail_job(failing, "again", claimed_by="w1") is False
    assert queue.complete_job(failing, {}, claimed_by="w1") is False
    events = queue.list_job_events()
    types = [event["type"] for event in events]
    assert types.count("done") == 1
    assert types.count("failed") == 1


def test_requeue_and_attempts(queue) -> None:
    job_id = queue.create_job("prewarm", {})
    queue.claim_job(claimed_by="w1", lease_seconds=30)
    assert queue.requeue_job(job_id, error="retry", claimed_by="w2") is False
    assert queue.requeue_job(job_id, error="retry", claimed_by="w1") is True
    requeued = queue.get_job(job_id)
    assert requeued["status"] == "queued"
    assert requeued["claimed_by"] == "" and requeued["lease_until"] == ""
    assert requeued["attempts"] == 1
    assert queue.claim_job(claimed_by="w3", lease_seconds=30)["attempts"] == 2
    assert queue.job_stats()["by_status"] == {
        "queued": 0,
        "running": 1,
        "done": 0,
        "failed": 0,
    }


def test_expired_lease_is_requeued_and_recovered(queue) -> None:
    job_id = queue.create_job("batch", {})
    claimed = queue.claim_job(claimed_by="dead-worker", lease_seconds=30)
    assert claimed is not None
    # Simulate the worker dying: lease expires in the past.
    queue._conn.execute(
        "UPDATE jobs SET lease_until = ? WHERE id = ?", ("2000-01-01T00:00:00+00:00", job_id)
    )
    reclaimed = queue.claim_job(claimed_by="new-worker", lease_seconds=30)
    assert reclaimed is not None
    assert reclaimed["id"] == job_id
    assert reclaimed["claimed_by"] == "new-worker"
    assert reclaimed["attempts"] == 2
    event_types = [event["type"] for event in queue.list_job_events(job_id=job_id)]
    assert event_types == ["created", "claimed", "requeued", "claimed"]

    # requeue_expired only touches expired jobs, recover_running requeues all.
    queue._conn.execute(
        "UPDATE jobs SET lease_until = ? WHERE id = ?", ("2000-01-01T00:00:00+00:00", job_id)
    )
    assert queue.requeue_expired() == 1
    assert queue.get_job(job_id)["status"] == "queued"
    queue.claim_job(claimed_by="w", lease_seconds=30)
    assert queue.recover_running() == 1
    assert queue.get_job(job_id)["status"] == "queued"
    assert queue.recover_running() == 0


def test_renew_lease(queue) -> None:
    job_id = queue.create_job("batch", {})
    claimed = queue.claim_job(claimed_by="w1", lease_seconds=5)
    assert claimed is not None
    assert queue.renew_job_lease(job_id, claimed_by="w2", lease_seconds=30) is False
    assert queue.renew_job_lease(job_id, claimed_by="w1", lease_seconds=30) is True
    renewed = queue.get_job(job_id)
    assert renewed["lease_until"] > claimed["lease_until"]
    with pytest.raises(ValueError):
        queue.renew_job_lease(job_id, claimed_by="w1", lease_seconds=0)


def test_event_replay_for_sse(queue) -> None:
    first = queue.create_job("batch", {"n": 1})
    second = queue.create_job("batch", {"n": 2})
    claimed = queue.claim_job(claimed_by="w1", lease_seconds=30)
    queue.complete_job(first, {"ok": True}, claimed_by="w1")
    assert claimed is not None and claimed["id"] == first

    events = queue.list_job_events()
    assert [event["id"] for event in events] == [1, 2, 3, 4]
    assert [event["type"] for event in events] == ["created", "created", "claimed", "done"]
    for event in events:
        assert set(event) == EVENT_KEYS
        assert event["created_at"]
        assert event["data"]["job"]["id"] == event["job_id"]
    assert events[0]["data"]["job"]["payload"] == {"n": 1}
    assert events[-1]["data"]["job"]["result"] == {"ok": True}

    # Last-Event-ID style replay: strictly newer events only.
    assert [event["id"] for event in queue.list_job_events(since_id=2)] == [3, 4]
    assert queue.list_job_events(since_id=queue.latest_job_event_id()) == []
    assert [event["job_id"] for event in queue.list_job_events(job_id=second)] == [second]
    assert [event["id"] for event in queue.list_job_events(types=("claimed",))] == [3]
    assert len(queue.list_job_events(limit=2)) == 2
    assert queue.list_job_events(limit=0) == []
    with pytest.raises(ValueError):
        queue.list_job_events(types=("bogus",))
    with pytest.raises(ValueError):
        queue.append_job_event(first, "bogus")

    custom = queue.append_job_event(first, "claimed", {"progress": 0.5})
    assert custom == queue.latest_job_event_id()


def test_prune_events(queue) -> None:
    for index in range(20):
        queue.create_job("batch", {"n": index})
    assert queue.latest_job_event_id() == 20
    deleted = queue.prune_job_events(keep_last=5)
    assert deleted == 15
    remaining = queue.list_job_events(limit=100)
    assert [event["id"] for event in remaining] == [16, 17, 18, 19, 20]
    assert queue.prune_job_events(keep_last=100) == 0
    assert queue.prune_job_events(keep_last=0) == 0


def test_stats_by_kind_and_status(queue) -> None:
    queue.create_job("batch", {})
    queue.create_job("batch", {})
    queue.create_job("prewarm", {})
    claimed = queue.claim_job("batch", claimed_by="w", lease_seconds=30)
    queue.fail_job(claimed["id"], "err", claimed_by="w")
    stats = queue.job_stats()
    assert stats["total"] == 3
    assert stats["by_status"] == {"queued": 2, "running": 0, "done": 0, "failed": 1}
    assert stats["by_kind"] == {"batch": 2, "prewarm": 1}
    assert queue.count_jobs() == 3
    assert queue.count_jobs(kind="prewarm") == 1
    assert queue.count_jobs(status="failed") == 1


def test_concurrent_claims_have_single_winner(tmp_path) -> None:
    state_path = tmp_path / "state.db"
    setup = JobQueue.open(state_path)
    job_ids = {setup.create_job("batch", {"n": index}) for index in range(40)}
    setup.close()

    claimed_ids: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(4)
    errors: list[BaseException] = []

    def worker(worker_id: int) -> None:
        queue = JobQueue.open(state_path)
        try:
            barrier.wait(timeout=5)
            while True:
                job = queue.claim_job(claimed_by=f"worker-{worker_id}", lease_seconds=60)
                if job is None:
                    return
                with lock:
                    claimed_ids.append(job["id"])
                assert queue.complete_job(
                    job["id"], {"worker": worker_id}, claimed_by=f"worker-{worker_id}"
                )
        except BaseException as exc:  # pragma: no cover - failure reporting
            errors.append(exc)
        finally:
            queue.close()

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors, errors
    assert len(claimed_ids) == len(set(claimed_ids)) == 40
    assert set(claimed_ids) == job_ids
    verification = JobQueue.open(state_path)
    try:
        assert verification.job_stats()["by_status"]["done"] == 40
    finally:
        verification.close()


def test_old_database_upgrade_in_place(tmp_path) -> None:
    db_path = tmp_path / "upgraded.db"
    store = SqliteStore(db_path)
    store.open()
    store.create_job("batch", {"symbol": "x"})
    store.close()
    # Simulate a v4 (pre-jobs) store database.
    connection = sqlite3.connect(db_path)
    connection.executescript(
        "DROP TABLE IF EXISTS job_events;"
        "DROP TABLE IF EXISTS jobs;"
        "DELETE FROM meta WHERE key = 'store_schema_version';"
    )
    connection.commit()
    connection.close()

    reopened = SqliteStore(db_path)
    with reopened:
        # open() re-creates the runtime tables + indexes (no finalize needed).
        indexes = {
            row[0]
            for row in reopened.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL"
            )
        }
        assert {"idx_jobs_status_created", "idx_job_events_job"} <= indexes
        meta = dict(reopened.connection.execute("SELECT key, value FROM meta"))
        assert meta["store_schema_version"] == "8"
        job_id = reopened.create_job("batch", {"symbol": "after-upgrade"})
        assert reopened.claim_job(claimed_by="w", lease_seconds=30)["id"] == job_id
        assert reopened.complete_job(job_id, {"ok": True}, claimed_by="w") is True
        reopened.finalize()  # idempotent on the upgraded database
        assert reopened.job_stats()["by_status"]["done"] == 1


def test_jobs_join_outer_store_transaction(tmp_path) -> None:
    store = SqliteStore(tmp_path / "tx.db")
    store.open()
    try:
        store.begin()
        job_id = store.create_job("batch", {"symbol": "tx"})
        store.commit()
        assert store.get_job(job_id)["status"] == "queued"
        assert store.stats()["tables"]["jobs"] == 1
    finally:
        store.close()


def test_invalid_states_and_empty_queue(tmp_path) -> None:
    queue = JobQueue.open(tmp_path / "state.db")
    try:
        assert queue.claim_job(claimed_by="w") is None
        assert queue.requeue_expired() == 0
        assert queue.recover_running() == 0
        assert queue.job_stats() == {
            "total": 0,
            "by_status": {"queued": 0, "running": 0, "done": 0, "failed": 0},
            "by_kind": {},
        }
        assert set(JOB_STATUSES) == {"queued", "running", "done", "failed"}
    finally:
        queue.close()
