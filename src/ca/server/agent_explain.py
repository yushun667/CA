"""Synchronous explain via MCP sampling (server -> connected agent).

The Web UI explain tab calls ``POST /api/explain/agent``; the backend collects
function facts and sends a standard MCP ``sampling/createMessage`` request to an
already connected sampling-capable agent session, waits synchronously for the
agent's model to answer, and returns the explanation in the same HTTP response.
There is **no backend LLM configuration, no job queue and no claim step**.

Without a connected sampling agent, or on timeout/agent error, the endpoint
falls back to the local heuristic (HTTP 200) with ``fallback_reason`` so the page
never blocks.  Only the function facts/source (bounded) are sent to the agent.
"""

from __future__ import annotations

import time
from typing import Any

from ca.server.sampling import (
    SamplingError,
    SamplingGateway,
    SamplingTimeout,
    SamplingUnavailable,
)
from ca.ui.server import (
    _collect_explain_facts,
    _facts_summary,
    _heuristic_explanation,
    _llm_prompt,
)

DEFAULT_SAMPLING_TIMEOUT = 30.0
MAX_SAMPLING_TIMEOUT = 120.0


def _sampling_params(facts: dict[str, Any], language: str) -> dict[str, Any]:
    """Build MCP 2025-06-18 ``CreateMessageRequest.params``."""

    messages = _llm_prompt(facts, language)
    system = str(messages[0].get("content") or "") if messages else ""
    user = str(messages[1].get("content") or "") if len(messages) > 1 else ""
    function = facts.get("function") or {}
    return {
        "messages": [
            {"role": "user", "content": {"type": "text", "text": user}},
        ],
        "systemPrompt": system,
        "includeContext": "none",
        "modelPreferences": {
            "costPriority": 0.5,
            "speedPriority": 0.5,
            "intelligencePriority": 0.5,
        },
        "temperature": 0.2,
        "maxTokens": 2000,
        "metadata": {
            "source": "ca-serve",
            "symbol": str(function.get("name") or ""),
            "function_id": str(function.get("id") or ""),
            "language": language,
        },
    }


def _result_text(result: dict[str, Any]) -> str:
    """Extract assistant text from a ``CreateMessageResult`` (text content)."""

    content = result.get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()
    if isinstance(content, dict):
        kind = str(content.get("type") or "text")
        if kind == "text":
            text = content.get("text")
            if isinstance(text, str) and text.strip():
                return text.strip()
    for key in ("explanation", "text"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise SamplingError("sampling result has no text content")


def _evidence(facts: dict[str, Any]) -> list[str]:
    function = facts.get("function") or {}
    evidence: list[str] = []
    location = f"{function.get('file')}:{function.get('line')}"
    if location.strip(":") and not location.startswith(":"):
        evidence.append(location)
    summary = _facts_summary(facts)
    for key in (
        "branch_count",
        "return_count",
        "resource_count",
        "caller_count",
        "callee_count",
    ):
        value = summary.get(key)
        if value:
            evidence.append(f"{key.replace('_count', '')}={value}")
    for finding in (summary.get("findings") or [])[:3]:
        if isinstance(finding, dict) and finding.get("kind"):
            evidence.append(f"finding:{finding['kind']}@L{finding.get('line', '?')}")
    if not facts.get("deep_ready"):
        evidence.append("deep_ready=false")
    return evidence


def explain_via_sampling(
    query_service: Any,
    gateway: SamplingGateway,
    symbol: str,
    language: str = "en",
    timeout: float = DEFAULT_SAMPLING_TIMEOUT,
) -> dict[str, Any]:
    """Collect facts, ask a connected agent over MCP sampling, return the answer."""

    started = time.perf_counter()
    language = "zh" if str(language or "").lower().startswith("zh") else "en"
    facts = _collect_explain_facts(query_service, symbol)
    function = facts["function"]
    provider = "heuristic"
    model = ""
    explanation = ""
    fallback_reason = ""
    session_id = ""
    if not gateway.has_sampling_session():
        fallback_reason = "no connected MCP sampling agent session"
    else:
        try:
            result, session_id = gateway.request_sampling(
                _sampling_params(facts, language), timeout=timeout
            )
            explanation = _result_text(result)
            model = str(result.get("model") or "")
            provider = "agent"
        except SamplingUnavailable as exc:
            fallback_reason = f"sampling unavailable: {exc}"
        except SamplingTimeout as exc:
            fallback_reason = str(exc)
        except SamplingError as exc:
            fallback_reason = f"sampling agent error: {exc}"
        except Exception as exc:  # malformed agent payloads must not 500 the page
            fallback_reason = f"sampling failed: {type(exc).__name__}: {exc}"
    if provider != "agent":
        model = ""
        explanation = _heuristic_explanation(facts, language, fallback_reason)
    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
    summary = _facts_summary(facts)
    return {
        "symbol": symbol,
        "function": {
            "id": str(function.get("id") or ""),
            "name": str(function.get("name") or ""),
            "qualified_name": str(function.get("qualified_name") or ""),
            "kind": str(function.get("kind") or ""),
            "file": str(function.get("file") or ""),
            "line": int(function.get("line") or 0),
            "end_line": int(function.get("end_line") or 0),
            "signature": str(function.get("signature") or ""),
            "return_type": str(function.get("return_type") or ""),
            "shard": str(function.get("shard") or ""),
        },
        "provider": provider,
        "model": model,
        "language": language,
        "explanation": explanation,
        "evidence": _evidence(facts),
        "facts_summary": summary,
        "cached": False,
        "fallback": provider != "agent",
        "fallback_reason": fallback_reason,
        "session_id": session_id,
        "elapsed_ms": elapsed_ms,
    }
