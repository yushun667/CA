"""``ca serve`` unified backend: query API, jobs, SSE and MCP over HTTP."""

from ca.server.app import ServeHTTPServer, make_server, public_job, serve
from ca.server.jobs import EventBus, JobService
from ca.server.service import JOB_MCP_TOOLS, ServeConfig, ServeError, ServeService

__all__ = [
    "EventBus",
    "JOB_MCP_TOOLS",
    "JobService",
    "ServeConfig",
    "ServeError",
    "ServeHTTPServer",
    "ServeService",
    "make_server",
    "public_job",
    "serve",
]
