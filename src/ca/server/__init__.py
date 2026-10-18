"""``ca serve`` unified backend: query API, generic jobs, SSE and MCP."""

from ca.server.app import ServeHTTPServer, make_server, public_job, serve
from ca.server.jobs import EventBus, JobService
from ca.server.service import ServeConfig, ServeError, ServeService

__all__ = [
    "EventBus",
    "JobService",
    "ServeConfig",
    "ServeError",
    "ServeHTTPServer",
    "ServeService",
    "make_server",
    "public_job",
    "serve",
]
