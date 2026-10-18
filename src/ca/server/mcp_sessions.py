"""Standard MCP sessions with server-initiated ``sampling/createMessage``.

``ca serve`` speaks MCP over HTTP with sessions (``Mcp-Session-Id``):

* ``POST /mcp`` with ``initialize`` creates a session; if the client declares
  ``capabilities.sampling`` the server may sample through it;
* the client keeps ``GET /mcp`` open (SSE stream) to receive server-initiated
  JSON-RPC requests;
* the client answers a ``sampling/createMessage`` request by POSTing the
  JSON-RPC response back to ``/mcp`` with the same session header;
* ``DELETE /mcp`` closes the session; idle sessions expire after a TTL.

This is the standard MCP reverse-request channel used by the Web UI explain
endpoint: there is no private agent-session CLI and no backend LLM.
"""

from __future__ import annotations

import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

DEFAULT_SESSION_TTL_SECONDS = 300.0


class SamplingUnavailable(RuntimeError):
    """No connected MCP session declared sampling capability."""


class SamplingTimeout(RuntimeError):
    """A sampling request was not answered before its timeout."""


class SamplingError(RuntimeError):
    """The MCP client answered a sampling request with a JSON-RPC error."""


@dataclass
class PendingSampling:
    request_id: str
    session_id: str
    event: threading.Event = field(default_factory=threading.Event)
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    created_at: float = field(default_factory=time.time)


@dataclass
class McpSamplingSession:
    session_id: str
    capabilities: dict[str, Any]
    client_info: dict[str, Any]
    connected_at: float
    last_seen: float
    events: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=256))
    pending: dict[str, PendingSampling] = field(default_factory=dict)
    closed: bool = False

    def sampling_capable(self) -> bool:
        return isinstance(self.capabilities.get("sampling"), (dict, bool, type(None))) and (
            "sampling" in self.capabilities
        )


class McpSessionRegistry:
    """In-process registry of standard MCP HTTP sessions (sampling capable)."""

    def __init__(self, *, session_ttl: float = DEFAULT_SESSION_TTL_SECONDS) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, McpSamplingSession] = {}
        self.session_ttl = max(30.0, float(session_ttl))

    # -- lifecycle ------------------------------------------------------- #
    def open_session(
        self, capabilities: dict[str, Any] | None = None, client_info: dict[str, Any] | None = None
    ) -> str:
        session_id = "mcp-session:" + uuid.uuid4().hex
        now = time.time()
        session = McpSamplingSession(
            session_id=session_id,
            capabilities=dict(capabilities or {}),
            client_info=dict(client_info or {}),
            connected_at=now,
            last_seen=now,
        )
        with self._lock:
            self._prune_locked(now)
            self._sessions[session_id] = session
        return session_id

    def close_session(self, session_id: str) -> bool:
        with self._lock:
            session = self._sessions.pop(session_id, None)
            if session is None:
                return False
            session.closed = True
            pending = list(session.pending.values())
            session.pending.clear()
        for item in pending:
            item.error = {"code": -32000, "message": "MCP session closed"}
            item.event.set()
        return True

    def close_all(self) -> None:
        with self._lock:
            ids = list(self._sessions)
        for session_id in ids:
            self.close_session(session_id)

    def get(self, session_id: str | None) -> McpSamplingSession | None:
        if not session_id:
            return None
        with self._lock:
            self._prune_locked(time.time())
            return self._sessions.get(session_id)

    def touch(self, session_id: str | None) -> bool:
        with self._lock:
            session = self._sessions.get(session_id or "")
            if session is None:
                return False
            session.last_seen = time.time()
            return True

    def _prune_locked(self, now: float) -> None:
        stale = [
            session_id
            for session_id, session in self._sessions.items()
            if not session.pending and now - session.last_seen > self.session_ttl
        ]
        for session_id in stale:
            session = self._sessions.pop(session_id, None)
            if session is not None:
                session.closed = True

    # -- introspection --------------------------------------------------- #
    def session_info(self, session_id: str) -> dict[str, Any] | None:
        session = self.get(session_id)
        if session is None:
            return None
        return {
            "session_id": session.session_id,
            "capabilities": session.capabilities,
            "client_info": session.client_info,
            "sampling": session.sampling_capable(),
            "connected_at": session.connected_at,
            "last_seen": session.last_seen,
            "pending": len(session.pending),
        }

    def sessions_info(self) -> list[dict[str, Any]]:
        with self._lock:
            self._prune_locked(time.time())
            ids = list(self._sessions)
        return [info for sid in ids if (info := self.session_info(sid)) is not None]

    def stats(self) -> dict[str, Any]:
        with self._lock:
            self._prune_locked(time.time())
            sessions = list(self._sessions.values())
        return {
            "sessions": len(sessions),
            "sampling_capable": sum(1 for item in sessions if item.sampling_capable()),
            "pending_requests": sum(len(item.pending) for item in sessions),
            "session_ttl_seconds": self.session_ttl,
        }

    def has_sampling_session(self, *, exclude: str | None = None) -> bool:
        with self._lock:
            self._prune_locked(time.time())
            return any(
                session.sampling_capable()
                and not session.closed
                and session.session_id != exclude
                for session in self._sessions.values()
            )

    # -- server -> client sampling --------------------------------------- #
    def request_sampling(
        self,
        params: dict[str, Any],
        *,
        timeout: float = 30.0,
        preferred_session_id: str | None = None,
        exclude_session_id: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        with self._lock:
            self._prune_locked(time.time())
            candidates = [
                session
                for session in self._sessions.values()
                if session.sampling_capable()
                and not session.closed
                and session.session_id != exclude_session_id
            ]
            if not candidates:
                raise SamplingUnavailable("no connected MCP sampling session")
            preferred = next(
                (item for item in candidates if item.session_id == preferred_session_id),
                None,
            )
            session = preferred or min(candidates, key=lambda item: item.connected_at)
            request_id = "smp:" + uuid.uuid4().hex
            pending = PendingSampling(request_id=request_id, session_id=session.session_id)
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
            raise SamplingUnavailable("MCP session event queue is full") from exc
        if not pending.event.wait(max(0.1, float(timeout))):
            with self._lock:
                session.pending.pop(request_id, None)
            raise SamplingTimeout(f"sampling request timed out after {timeout}s")
        if pending.error:
            raise SamplingError(str(pending.error.get("message") or pending.error))
        return dict(pending.result or {}), session.session_id

    def respond(
        self,
        session_id: str | None,
        request_id: str,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> bool:
        session = self.get(session_id)
        if session is None:
            return False
        with self._lock:
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
