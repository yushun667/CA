"""MCP sampling gateway: server -> connected agent ``sampling/createMessage``.

The Web UI explain tab must call the model that the user's MCP agent already
uses (agent-side model), not a backend-configured LLM.  A sampling-capable
agent therefore keeps a long-lived side channel open:

1. ``POST /agent/session`` opens a session (declares ``capabilities.sampling``);
2. ``GET /agent/session/<id>/events`` is the server->agent SSE stream;
3. the server sends a standard JSON-RPC request
   ``sampling/createMessage`` (MCP 2025-06-18 shape);
4. the agent answers ``POST /agent/session/<id>/response`` with the JSON-RPC
   response (``result`` = ``CreateMessageResult``);
5. the waiting ``POST /api/explain/agent`` HTTP request returns synchronously.

No jobs, no queue, no backend LLM configuration: only an already connected
sampling-capable agent can serve the request; otherwise the API falls back to
the local heuristic immediately.
"""

from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

DEFAULT_SESSION_TTL_SECONDS = 120.0


class SamplingError(RuntimeError):
    """Sampling request failed on the agent side (JSON-RPC error response)."""


class SamplingUnavailable(RuntimeError):
    """No sampling-capable agent session is connected."""


class SamplingTimeout(RuntimeError):
    """The sampling request exceeded its timeout."""


@dataclass
class PendingSampling:
    request_id: str
    session_id: str
    event: threading.Event = field(default_factory=threading.Event)
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    created_at: float = field(default_factory=time.time)


@dataclass
class SamplingSession:
    id: str
    name: str
    capabilities: dict[str, Any]
    connected_at: float
    last_seen: float
    events: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=256))
    pending: dict[str, PendingSampling] = field(default_factory=dict)
    closed: bool = False

    def sampling_capable(self) -> bool:
        return bool(self.capabilities.get("sampling") is not None)


class SamplingGateway:
    def __init__(self, *, session_ttl: float = DEFAULT_SESSION_TTL_SECONDS) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, SamplingSession] = {}
        self.session_ttl = max(10.0, float(session_ttl))

    # -- session lifecycle ---------------------------------------------- #
    def open_session(
        self, name: str = "", capabilities: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        session_id = "smp-session:" + uuid.uuid4().hex
        now = time.time()
        session = SamplingSession(
            id=session_id,
            name=str(name or "mcp-agent"),
            capabilities=dict(capabilities or {}),
            connected_at=now,
            last_seen=now,
        )
        with self._lock:
            self._prune_locked(now)
            self._sessions[session_id] = session
        return self.session_info(session_id) or {}

    def close_session(self, session_id: str) -> bool:
        with self._lock:
            session = self._sessions.pop(session_id, None)
            if session is None:
                return False
            session.closed = True
            pending = list(session.pending.values())
            session.pending.clear()
        for item in pending:
            item.error = {"code": -32000, "message": "sampling session closed"}
            item.event.set()
        return True

    def close_all(self) -> None:
        with self._lock:
            ids = list(self._sessions)
        for session_id in ids:
            self.close_session(session_id)

    def touch(self, session_id: str) -> bool:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return False
            session.last_seen = time.time()
            return True

    def _prune_locked(self, now: float) -> None:
        stale = [
            session_id
            for session_id, session in self._sessions.items()
            if not session.pending
            and now - session.last_seen > self.session_ttl
        ]
        for session_id in stale:
            session = self._sessions.pop(session_id, None)
            if session is not None:
                session.closed = True

    def get_session(self, session_id: str) -> SamplingSession | None:
        with self._lock:
            return self._sessions.get(session_id)

    def session_info(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._prune_locked(time.time())
            session = self._sessions.get(session_id)
            if session is None:
                return None
            return self._info(session)

    @staticmethod
    def _info(session: SamplingSession) -> dict[str, Any]:
        return {
            "session_id": session.id,
            "name": session.name,
            "capabilities": session.capabilities,
            "sampling": session.sampling_capable(),
            "connected_at": session.connected_at,
            "last_seen": session.last_seen,
            "pending": len(session.pending),
            "events_url": f"/agent/session/{session.id}/events",
            "response_url": f"/agent/session/{session.id}/response",
            "heartbeat_url": f"/agent/session/{session.id}/heartbeat",
        }

    def sessions_info(self) -> list[dict[str, Any]]:
        with self._lock:
            self._prune_locked(time.time())
            sessions = list(self._sessions.values())
        return [self._info(session) for session in sessions]

    def stats(self) -> dict[str, Any]:
        sessions = self.sessions_info()
        return {
            "sessions": len(sessions),
            "sampling_capable": sum(1 for item in sessions if item.get("sampling")),
            "pending_requests": sum(int(item.get("pending") or 0) for item in sessions),
            "session_ttl_seconds": self.session_ttl,
        }

    def has_sampling_session(self) -> bool:
        with self._lock:
            self._prune_locked(time.time())
            return any(session.sampling_capable() for session in self._sessions.values())

    # -- request/response ----------------------------------------------- #
    def request_sampling(
        self, params: dict[str, Any], *, timeout: float = 30.0
    ) -> tuple[dict[str, Any], str]:
        """Send ``sampling/createMessage`` to the oldest sampling agent; wait."""

        with self._lock:
            self._prune_locked(time.time())
            candidates = [
                session
                for session in self._sessions.values()
                if session.sampling_capable() and not session.closed
            ]
            if not candidates:
                raise SamplingUnavailable("no sampling-capable agent session connected")
            session = min(candidates, key=lambda item: item.connected_at)
            request_id = "smp:" + uuid.uuid4().hex
            pending = PendingSampling(request_id=request_id, session_id=session.id)
            session.pending[request_id] = pending
            session.last_seen = time.time()
        event = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "sampling/createMessage",
            "params": params,
        }
        try:
            session.events.put_nowait(event)
        except queue.Full as exc:
            with self._lock:
                session.pending.pop(request_id, None)
            raise SamplingUnavailable("agent session event queue is full") from exc
        if not pending.event.wait(max(0.1, float(timeout))):
            with self._lock:
                session.pending.pop(request_id, None)
            raise SamplingTimeout(f"sampling request timed out after {timeout}s")
        if pending.error:
            raise SamplingError(str(pending.error.get("message") or pending.error))
        return dict(pending.result or {}), session.id

    def respond(
        self,
        session_id: str,
        request_id: str,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> bool:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return False
            pending = session.pending.pop(str(request_id), None)
            if pending is None:
                return False
            session.last_seen = time.time()
        if error is not None:
            pending.error = dict(error)
        else:
            pending.result = dict(result or {})
        pending.event.set()
        return True
