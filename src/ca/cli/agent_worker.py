"""``ca agent-worker``: a real MCP agent that consumes explain jobs.

The worker talks to a running ``ca serve`` backend over HTTP: it uses MCP
``explain_jobs`` to atomically claim a queued explain job, obtains a source
explanation from the backend's read-only ``/api/explain`` endpoint (heuristic
or ``CA_LLM_*`` when configured), then writes the result back with
``submit_explanation`` so the Web UI receives the SSE ``done`` event.

It is a small demonstration client, not a sandboxed agent: it never opens the
database itself, has no writes beyond the job API, and exits cleanly on
SIGINT/SIGTERM. Run it alongside ``ca serve`` when agent-sourced explanations
should take precedence over the server's grace auto-completion.
"""

from __future__ import annotations

import json
import signal
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass
class AgentWorkerConfig:
    serve_url: str
    worker: str = "ca-agent-worker"
    interval: float = 1.0
    limit: int = 0
    language: str = "en"
    once: bool = False


def _base_url(serve_url: str) -> str:
    value = str(serve_url or "").strip()
    if not value:
        raise ValueError("serve URL is empty")
    if "://" not in value:
        value = "http://" + value
    return value.rstrip("/")


def _mcp_url(base: str) -> str:
    return base + "/mcp"


def _post_json(url: str, payload: Any, timeout: float = 60.0) -> Any:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    return json.loads(body) if body.strip() else None


def _get_json(url: str, timeout: float = 30.0) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _call_tool(base: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    response = _post_json(
        _mcp_url(base),
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    envelope = json.loads(response["result"]["content"][0]["text"])
    if not envelope.get("ok"):
        error = envelope.get("error") or {}
        raise RuntimeError(f"{name}: {error.get('code')}: {error.get('message')}")
    return envelope.get("data") or {}


def _initialize(base: str) -> None:
    _post_json(
        _mcp_url(base),
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "ca-agent-worker", "version": "0.1"},
            },
        },
    )


def _claim(base: str, config: AgentWorkerConfig) -> dict[str, Any] | None:
    data = _call_tool(
        base,
        "explain_jobs",
        {
            "action": "claim",
            "worker": config.worker,
            "lease_seconds": max(120, int(config.interval * 4)),
        },
    )
    job = data.get("job")
    return dict(job) if isinstance(job, dict) else None


def _explain(base: str, job: dict[str, Any], config: AgentWorkerConfig) -> tuple[str, list[str]]:
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    symbol = str(payload.get("symbol") or job.get("symbol") or "").strip()
    if not symbol:
        raise RuntimeError("explain job has no symbol")
    language = str(payload.get("lang") or payload.get("language") or config.language)
    query = urllib.parse.urlencode({"symbol": symbol, "language": language})
    try:
        data = _get_json(f"{base}/api/explain?{query}")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"explain API returned {exc.code}") from exc
    if not data.get("ok", True):
        raise RuntimeError(f"explain API error: {data.get('error')}")
    explanation = str(data.get("data", {}).get("explanation") or "").strip()
    if not explanation:
        raise RuntimeError("explain API returned an empty explanation")
    function = (data.get("data", {}).get("function") or {})
    evidence: list[str] = []
    location = f"{function.get('file')}:{function.get('line')}"
    if location.strip(":") and not location.startswith(":"):
        evidence.append(location)
    summary = data.get("data", {}).get("facts_summary") or {}
    if summary.get("branch_count") is not None:
        evidence.append(f"branches={summary.get('branch_count')}")
    if summary.get("resource_kind_count") is not None:
        evidence.append(f"resources={summary.get('resource_kind_count')}")
    return explanation, evidence


def _submit(
    base: str, job_id: str, explanation: str, evidence: list[str], config: AgentWorkerConfig
) -> dict[str, Any]:
    return _call_tool(
        base,
        "submit_explanation",
        {
            "job_id": job_id,
            "explanation": explanation,
            "evidence": evidence,
            "worker": config.worker,
        },
    )


def run_worker(config: AgentWorkerConfig) -> int:
    """Claim/explain/submit until ``--once``/``--limit`` or SIGINT/SIGTERM."""

    try:
        base = _base_url(config.serve_url)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    stop = threading.Event()
    previous: dict[int, Any] = {}

    def _on_signal(signum: int, _frame: Any) -> None:
        stop.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[signum] = signal.signal(signum, _on_signal)
        except (ValueError, OSError):
            pass
    try:
        _initialize(base)
        print(
            f"[agent-worker] connected {base} as {config.worker}",
            file=sys.stderr,
            flush=True,
        )
        processed = 0
        empty_polls = 0
        try:
            while not stop.is_set():
                if config.limit and processed >= config.limit:
                    break
                job = _claim(base, config)
                if job is None:
                    empty_polls += 1
                    if config.once:
                        print("[agent-worker] no queued explain job", file=sys.stderr, flush=True)
                        return 0
                    if stop.wait(config.interval):
                        break
                    continue
                job_id = str(job.get("id") or "")
                symbol = str((job.get("payload") or {}).get("symbol") or "")
                try:
                    explanation, evidence = _explain(base, job, config)
                    result = _submit(base, job_id, explanation, evidence, config)
                    processed += 1
                    print(
                        f"[agent-worker] done {job_id} symbol={symbol}"
                        f" chars={len(explanation)} evidence={len(evidence)}"
                        f" status={(result.get('job') or {}).get('status')}",
                        file=sys.stderr,
                        flush=True,
                    )
                except Exception as exc:  # a bad job must not kill the worker
                    print(
                        f"[agent-worker] failed {job_id} symbol={symbol}:"
                        f" {type(exc).__name__}: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                if config.once:
                    return 0
        except KeyboardInterrupt:  # pragma: no cover - interactive path
            stop.set()
        finally:
            for signum, handler in previous.items():
                try:
                    signal.signal(signum, handler)
                except (ValueError, OSError):
                    pass
    except Exception as exc:
        print(f"error: agent worker failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"[agent-worker] stopped after {processed} job(s)", file=sys.stderr, flush=True)
    return 0
