"""``ca serve`` unified backend: query API, jobs, SSE, MCP and sampling."""

from ca.server.agent_explain import explain_via_sampling
from ca.server.app import ServeHTTPServer, make_server, public_job, serve
from ca.server.jobs import EventBus, JobService
from ca.server.sampling import (
    SamplingError,
    SamplingGateway,
    SamplingTimeout,
    SamplingUnavailable,
)
from ca.server.service import JOB_MCP_TOOLS, ServeConfig, ServeError, ServeService

__all__ = [
    "EventBus",
    "JOB_MCP_TOOLS",
    "JobService",
    "SamplingError",
    "SamplingGateway",
    "SamplingTimeout",
    "SamplingUnavailable",
    "ServeConfig",
    "ServeError",
    "ServeHTTPServer",
    "ServeService",
    "explain_via_sampling",
    "make_server",
    "public_job",
    "serve",
]
