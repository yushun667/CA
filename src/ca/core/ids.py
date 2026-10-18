"""Stable identifier rules shared by every producer and consumer.

Identity requirements:

- The same entity seen from different translation units must get the same id
  (headers are parsed repeatedly; definitions are deduplicated at ingest).
- A call site's ``callee_id`` must match the callee's ``FunctionRecord.id`` when
  both are derived from the same Clang USR, so the store can resolve calls
  without a second parsing pass.
- IDs must not depend on absolute machine paths or run-time ordering.

External-linkage functions/types are identified by their Clang USR. Internal
linkage adds the defining file because USRs are not guaranteed unique across
files for translation-unit-local entities. Cursors with an empty USR fall back
to name plus source location.
"""

from __future__ import annotations

import hashlib
from typing import Iterable

from ca.core.schema import Sloc

_SEPARATOR = "\x1f"
_DIGEST_CHARS = 24


def digest(*parts: object) -> str:
    """Deterministic short digest over normalized parts."""

    joined = _SEPARATOR.join("" if part is None else str(part) for part in parts)
    return hashlib.sha1(joined.encode("utf-8", "surrogatepass")).hexdigest()[:_DIGEST_CHARS]


def sloc_key(sloc: Sloc) -> str:
    return f"{sloc.file}:{sloc.line}:{sloc.col}"


def tu_id(file: str, directory: str, arguments: Iterable[str]) -> str:
    return "tu:" + digest(file, directory, _SEPARATOR.join(arguments))


def function_id(
    *,
    name: str,
    usr: str,
    sloc: Sloc,
    linkage: str,
) -> str:
    """Stable id for a function definition or declaration."""

    if usr:
        base = usr if linkage == "external" else f"{usr}|{sloc.file}"
    else:
        base = f"{name}|{sloc.file}|{sloc.line}|{sloc.col}"
    return "fn:" + digest(base)


def type_id(*, name: str, usr: str, sloc: Sloc, kind: str) -> str:
    base = usr if usr else f"{kind}|{name}|{sloc.file}|{sloc.line}|{sloc.col}"
    return "ty:" + digest(base)


def field_id(*, type_id_value: str, name: str, sloc: Sloc, index: int) -> str:
    return "field:" + digest(type_id_value, name, sloc.file, sloc.line, sloc.col, index)


def call_id(*, caller_id: str, callee_usr: str, callee_name: str, sloc: Sloc) -> str:
    callee = callee_usr or callee_name
    return "call:" + digest(caller_id, callee, sloc.file, sloc.line, sloc.col)


def cfg_block_id(function_id_value: str, index: int) -> str:
    return "bb:" + digest(function_id_value, str(index))


def cfg_edge_id(function_id_value: str, src: str, dst: str, kind: str) -> str:
    return "edge:" + digest(function_id_value, src, dst, kind)


def branch_id(function_id_value: str, sloc: Sloc, ordinal: int) -> str:
    return "branch:" + digest(function_id_value, sloc.file, str(sloc.line), str(sloc.col), str(ordinal))


def assignment_id(function_id_value: str, sloc: Sloc, ordinal: int) -> str:
    return "assign:" + digest(function_id_value, sloc.file, str(sloc.line), str(sloc.col), str(ordinal))


def return_id(function_id_value: str, sloc: Sloc, ordinal: int) -> str:
    return "ret:" + digest(function_id_value, sloc.file, str(sloc.line), str(sloc.col), str(ordinal))


def resource_op_id(function_id_value: str, sloc: Sloc, api_name: str, ordinal: int) -> str:
    return "res:" + digest(function_id_value, sloc.file, str(sloc.line), str(sloc.col), api_name, str(ordinal))


def call_target_id(call_id_value: str, callee_id: str, kind: str) -> str:
    return "ct:" + digest(call_id_value, callee_id, kind)


def finding_id(function_id_value: str, kind: str, acquire_id: str) -> str:
    return "finding:" + digest(function_id_value, kind, acquire_id)
