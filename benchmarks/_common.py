"""Shared helpers for CA benchmark scripts: paths, metadata, JSON results.

Benchmarks are run as plain scripts from the repository root, e.g.
``.venv/bin/python benchmarks/run_accuracy.py``; this module makes ``ca``
importable and records enough environment metadata to reproduce a result.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
DEFAULT_RESULTS_DIR = REPO_ROOT / "benchmarks" / "results"


def ensure_import_path() -> None:
    """Import ``ca`` from ``src`` and propagate the path to spawned workers."""

    src = str(SRC_DIR)
    if src not in sys.path:
        sys.path.insert(0, src)
    parts = [part for part in os.environ.get("PYTHONPATH", "").split(os.pathsep) if part]
    if src not in parts:
        os.environ["PYTHONPATH"] = os.pathsep.join([src, *parts])


def display_path(path: Path | str) -> str:
    """Repository-relative path for reports when possible."""

    candidate = Path(path)
    try:
        return str(candidate.resolve().relative_to(REPO_ROOT))
    except (ValueError, OSError):
        return str(candidate)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_revision(path: Path) -> str:
    """Revision of the repository rooted exactly at ``path``, else ``""``.

    Corpora often live inside this repository but are not their own Git checkout
    (e.g. an extracted kernel tarball); in that case the compiled-in metadata
    file is the authoritative corpus identity, not the enclosing repo.
    """

    resolved = Path(path).resolve()
    try:
        top = subprocess.run(
            ["git", "-C", str(resolved), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if top.returncode != 0 or not top.stdout.strip():
            return ""
        if Path(top.stdout.strip()).resolve() != resolved:
            return ""
        result = subprocess.run(
            ["git", "-C", str(resolved), "rev-parse", "--verify", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    revision = result.stdout.strip()
    if not revision:
        return ""
    dirty = subprocess.run(
        ["git", "-C", str(path), "status", "--porcelain"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    ).stdout.strip()
    return revision + ("-dirty" if dirty else "")


def _installed_version(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return ""


def memory_bytes() -> int | None:
    try:
        if sys.platform == "darwin":
            result = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            return int(result.stdout.strip()) if result.stdout.strip() else None
        pages = os.sysconf("SC_PAGE_SIZE")
        pages_total = os.sysconf("SC_PHYS_PAGES")
        return int(pages * pages_total)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def machine_info() -> dict[str, Any]:
    info: dict[str, Any] = {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "cpu_count": os.cpu_count(),
        "memory_bytes": memory_bytes(),
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "libclang": _installed_version("libclang"),
        "clang_python": _installed_version("clang"),
        "duckdb": _installed_version("duckdb"),
    }
    try:  # libclang's actual shared library path is useful when parsing breaks
        from clang.cindex import Config

        info["clang_library"] = str(Config.library_path or Config.library_file or "")
    except Exception:
        info["clang_library"] = ""
    return info


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def dataclass_dict(value: Any) -> dict[str, Any]:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    return dict(value)


def resolve_result_path(path: str | Path | None, default_name: str) -> Path:
    if path is None:
        return DEFAULT_RESULTS_DIR / default_name
    candidate = Path(path)
    return candidate if candidate.is_absolute() else REPO_ROOT / candidate


def write_result(path: Path, data: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n",
                    encoding="utf-8")
    return path


def command_line() -> str:
    return " ".join([display_path(Path(sys.executable)), *sys.argv])
