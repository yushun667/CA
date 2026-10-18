"""Compile-argument acquisition: real build parameters for every TU.

Contract (see ``ca.core.interfaces``)::

    load_compilation_database(path, repo_root=None) -> list[CompilationUnit]
    capture_build(command, output, repo_root, jobs=1) -> list[CompilationUnit]
    reconstruct_kernel_commands(build_dir, repo_root) -> list[CompilationUnit]
    synthesize_for_files(files, repo_root, language="c", extra_args=()) ->
    list[CompilationUnit]

Hard rule: a command is either captured from a real build (source names it) or
explicitly marked ``synthesized``. Never present guessed flags as real ones.

Modules: ``compdb`` (load/normalize/write), ``capture`` (CC/CXX wrapper
capture), ``kernel_cmd`` (Kbuild ``.cmd`` reconstruction), ``synth`` (explicit
fallback), ``corpus`` (xv6 / Linux kernel corpora under ``.tmp/``).
"""

from __future__ import annotations

from ca.build.capture import BuildCaptureError, CaptureSession, capture_build
from ca.build.compdb import (
    COMPILE_COMMANDS_NAME,
    infer_repo_root,
    is_c_family,
    language_for_path,
    load_compilation_database,
    units_to_entries,
    write_compilation_database,
)
from ca.build.corpus import (
    LINUX_KERNEL_COMPDB_NOTES,
    build_xv6_compdb,
    fetch_xv6,
    xv6_path,
)
from ca.build.kernel_cmd import reconstruct_kernel_commands
from ca.build.synth import synthesize_for_files

__all__ = [
    "BuildCaptureError",
    "COMPILE_COMMANDS_NAME",
    "CaptureSession",
    "LINUX_KERNEL_COMPDB_NOTES",
    "build_xv6_compdb",
    "capture_build",
    "fetch_xv6",
    "infer_repo_root",
    "is_c_family",
    "language_for_path",
    "load_compilation_database",
    "reconstruct_kernel_commands",
    "synthesize_for_files",
    "units_to_entries",
    "write_compilation_database",
    "xv6_path",
]
