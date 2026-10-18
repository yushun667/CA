"""``ca agent-session``: keep an MCP sampling session open to a ``ca serve``.

A sampling-capable MCP agent opens a long-lived side channel
(``POST /agent/session`` + ``GET .../events`` SSE), receives standard
``sampling/createMessage`` JSON-RPC requests from the server, generates the
answer **with the agent's own model** (or an external ``--command``), and posts
the JSON-RPC response back.  The Web UI's synchronous explain endpoint then
returns that answer directly; no jobs, claims or backend LLM configuration are
involved.

The default handler is a deterministic demo (useful for tests/offline demos);
real agents either implement the same JSON-RPC flow themselves or pass
``--command CMD`` (stdin = sampling params JSON, stdout = explanation text).
"""

from __future__ import annotations

import json
import shlex
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass
class AgentSessionConfig:
    serve_url: str
    name: str = "ca-agent-session"
    model: str = "demo-agent"
    command: str = ""
    once: bool = False
    heartbeat_interval: float = 15.0


def _base_url(serve_url: str) -> str:
    value = str(serve_url or "").strip()
    if not value:
        raise ValueError("serve URL is empty")
    if "://" not in value:
        value = "http://" + value
    return value.rstrip("/")


def _post(url: str, payload: dict[str, Any], timeout: float = 30.0) -> Any:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    return json.loads(body) if body.strip() else None


def _delete(url: str, timeout: float = 10.0) -> None:
    request = urllib.request.Request(url, method="DELETE")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read()
    except Exception:
        pass


def _demo_explanation(params: dict[str, Any], model: str) -> tuple[str, list[str]]:
    """Deterministic demo: echo a compact summary of the supplied facts."""

    messages = params.get("messages") or []
    user_text = ""
    if isinstance(messages, list) and messages:
        content = messages[0].get("content") if isinstance(messages[0], dict) else None
        if isinstance(content, dict):
            user_text = str(content.get("text") or "")
        elif isinstance(content, str):
            user_text = content
    metadata = params.get("metadata") or {}
    symbol = str(metadata.get("symbol") or "")
    header = f"[{model}] MCP sampling demo explanation"
    if symbol:
        header += f" for {symbol}"
    body = user_text.strip()
    if len(body) > 4000:
        body = body[:4000] + "\n…(truncated)"
    explanation = f"{header}\n\n{body}"
    evidence: list[str] = []
    if symbol:
        evidence.append(f"symbol={symbol}")
    evidence.append("sampling=createMessage")
    return explanation, evidence


def _run_command(command: str, params: dict[str, Any], timeout: float) -> tuple[str, str]:
    result = subprocess.run(
        shlex.split(command),
        input=json.dumps(params),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"agent command failed ({result.returncode}): {result.stderr.strip()[:500]}"
        )
    output = result.stdout.strip()
    if not output:
        raise RuntimeError("agent command produced no output")
    try:
        decoded = json.loads(output)
    except json.JSONDecodeError:
        return output, ""
    if isinstance(decoded, dict):
        explanation = str(decoded.get("explanation") or decoded.get("text") or "")
        model = str(decoded.get("model") or "")
        if explanation:
            return explanation, model
    return output, ""


def _handle_sampling(
    base: str,
    session_id: str,
    request_id: str,
    params: dict[str, Any],
    config: AgentSessionConfig,
) -> str:
    if config.command:
        explanation, command_model = _run_command(
            config.command, params, timeout=30.0
        )
        model = command_model or config.model
        evidence = ["provider=agent", "source=command"]
    else:
        explanation, evidence = _demo_explanation(params, config.model)
        model = config.model
    result = {
        "role": "assistant",
        "content": {"type": "text", "text": explanation},
        "model": model,
        "stopReason": "endTurn",
        "_meta": {"evidence": evidence},
    }
    _post(
        f"{base}/agent/session/{session_id}/response",
        {"jsonrpc": "2.0", "id": request_id, "result": result},
    )
    return explanation


def _heartbeat(base: str, session_id: str, interval: float, stop: threading.Event) -> None:
    while not stop.wait(interval):
        try:
            _post(f"{base}/agent/session/{session_id}/heartbeat", {"session_id": session_id})
        except Exception:
            return


def _iter_sse_events(stream: Any):
    buffer = ""
    for raw in stream:
        line = raw.decode("utf-8", errors="replace").rstrip("\n")
        if not line:
            if buffer:
                event = _parse_sse_block(buffer)
                buffer = ""
                if event is not None:
                    yield event
            continue
        if line.startswith(":"):
            continue
        buffer += line + "\n"


def _parse_sse_block(block: str) -> dict[str, Any] | None:
    data_lines = [line[len("data:"):].strip() for line in block.splitlines() if line.startswith("data:")]
    if not data_lines:
        return None
    try:
        payload = json.loads("\n".join(data_lines))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def run_agent_session(config: AgentSessionConfig) -> int:
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
    session_id = ""
    heartbeat: threading.Thread | None = None
    try:
        opened = _post(
            f"{base}/agent/session",
            {"name": config.name, "capabilities": {"sampling": {}}},
            timeout=15.0,
        )
        if not opened or not opened.get("ok"):
            raise RuntimeError(f"failed to open sampling session: {opened}")
        data = opened["data"]
        session_id = str(data["session_id"])
        events_url = base + str(data["events_url"])
        print(
            f"[agent-session] connected {base} session={session_id}"
            f" model={config.model} command={config.command or '(demo)'}",
            file=sys.stderr,
            flush=True,
        )
        heartbeat = threading.Thread(
            target=_heartbeat,
            args=(base, session_id, config.heartbeat_interval, stop),
            daemon=True,
        )
        heartbeat.start()
        with urllib.request.urlopen(events_url, timeout=3600) as stream:
            for event in _iter_sse_events(stream):
                if stop.is_set():
                    break
                if event.get("type") == "hello":
                    continue
                if event.get("method") != "sampling/createMessage":
                    continue
                request_id = str(event.get("id") or "")
                params = event.get("params") if isinstance(event.get("params"), dict) else {}
                try:
                    explanation = _handle_sampling(
                        base, session_id, request_id, params, config
                    )
                    print(
                        f"[agent-session] answered sampling {request_id}"
                        f" chars={len(explanation)}",
                        file=sys.stderr,
                        flush=True,
                    )
                except Exception as exc:
                    print(
                        f"[agent-session] sampling {request_id} failed:"
                        f" {type(exc).__name__}: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                    _post(
                        f"{base}/agent/session/{session_id}/response",
                        {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "error": {"code": -32000, "message": f"{type(exc).__name__}: {exc}"},
                        },
                    )
                if config.once:
                    return 0
    except KeyboardInterrupt:  # pragma: no cover - interactive path
        stop.set()
    except Exception as exc:
        print(f"error: agent session failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        stop.set()
        if session_id:
            _delete(f"{base}/agent/session/{session_id}")
        if heartbeat is not None:
            heartbeat.join(timeout=1)
        for signum, handler in previous.items():
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):
                pass
        print("[agent-session] closed", file=sys.stderr, flush=True)
    return 0
