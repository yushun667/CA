"""libclang-based extraction of one compilation unit.

Entry point: :func:`parse_compilation_unit` implements the ``ca.parse`` contract
from :mod:`ca.core.interfaces`:

    parse_compilation_unit(tu: CompilationUnit, options=None) -> ParsedUnit

The returned :class:`ParsedTranslationUnit` keeps the live
``clang.cindex.TranslationUnit`` (and per-function cursors) inside the worker
process so the analyzer can reuse the same AST. Only
``ParsedTranslationUnit.parse_result()`` detaches plain, picklable records.

Compile arguments come from the build stage and are *real* flags (possibly
GCC-specific). The parser therefore:

- runs libclang with ``-working-directory <tu.directory>`` and the absolute
  source path so relative ``-I``/``-include`` paths resolve like the build did;
- drops output/produced-artifact flags (``-o``, ``-MD``/``-MF``/``-MT``, ...)
  that only make libclang write files or fail;
- infers an explicit ``--target`` when the real compiler was a cross compiler
  (driver triple, ``-march=rv64*``, ``-mabi=lp64*``) because libclang defaults
  to the host target and rejects e.g. ``-mcmodel=medany`` otherwise;
- adds ``-isysroot $(xcrun --show-sdk-path)`` for Darwin user-space targets
  that lack a sysroot (Apple SDK headers are not found by libclang otherwise);
- for a command captured from a newer compiler, replays it with only frontend
  passthrough options removed (``-Xclang``/``-mllvm``) when the shipped libclang
  rejects them, keeping the real target/sysroot and recording a note. OpenHarmony
  databases are handled the same way: a tiny preflight parse learns the OH clang
  vendor options this libclang rejects (``-fstack-protector-ret-strong``,
  ``-fcfi-trap-function=...``, ``-enable-trivial-...``), they are stripped with a
  note, and codegen-only groups (``-fsanitize*``/``-fprofile*``/``-fcoverage*``)
  are ignored for AST parsing; the OH driver's implicit resource-dir and libc++
  include directories are discovered from the (possibly relative) driver path so
  one full parse per TU suffices. Missing *generated* headers (libc++'s
  ``__config_site``, the musl sysroot under ``out/...``) are a corpus
  environment gap, not an option-compatibility issue and are reported as such;
- if the first attempt produces error diagnostics (or fails to load), retries
  once with semantics-preserving warning flags such as ``-Werror`` removed and
  keeps the better attempt. The retry is recorded as a ``note`` diagnostic.

Known limitations (deliberate, reported rather than hidden): include paths and
macros are only as good as the build database (``synthesized`` units may miss
both); macro-generated calls are attributed to the expansion site and only
flagged when ``ParseOptions.detect_macro_expansions`` is enabled; function
pointer calls are marked ``indirect``/unresolved; USR-less cursors fall back to
name+location, so a forward declaration and its later definition may not join.

Backend selection: the default is the libclang bundled with the pinned
``clang`` wheel. ``CA_LIBCLANG_LIBRARY`` (or ``CA_LIBCLANG_LIBRARY_PATH``) selects
a matched-version backend before bindings load - OpenHarmony acceptance runs on
OH's ``libclang.so.15`` with PyPI ``clang==15.0.6.1`` bindings on ``PYTHONPATH``
(xv6/FreeRTOS/local keep libclang 18). libclang 15 needs the source operand gone,
has no bundled C++ headers, and segfaults in ``clang_getFieldDeclBitWidth`` on
OH libc++ bitfields; those are handled here. Per-cursor extraction isolation
keeps one unreadable AST node from discarding a whole TU's records.

For header-heavy C++ corpora, ``ParseOptions.record_scope="main_file"`` emits
functions/calls for the TU's own file only (types/fields stay for layout), and
``emit_header_types=False`` additionally drops header type/field records; the
OpenHarmony pipeline uses these to cut per-TU serialization (header definitions
are ~98% of records and dominate downstream analysis). Query UX keeps working
through ``ParseOptions.emit_header_declarations=True``: in main-file scope the
repo-header function declarations are indexed as ``is_definition=False`` records
(signature/position/USR, deduplicated by id with definitions winning, bounded by
``header_declaration_limit``) so ``find_symbol`` can report declaration
locations and "definition missing" without header bodies or duplicate records.

Function-pointer types (``tests/test_parse_fnptr.py``): typedefs keep their
underlying spelling (``int (*)(int)``), parameters/fields keep the declared
spelling (typedef name when used, raw pointer type otherwise) and function
signatures embed the same text, so analyzers can match indirect calls; the
``CallRecord`` itself has no function-pointer type field (join the callee
variable name to ParamRecord/FieldRecord).

C++ coverage (see ``tests/test_parse_cpp_basic.py``): namespaces, class/struct,
enum (incl. ``enum class``), ``using`` aliases, constructors/destructors,
inline/const/static member functions, overloads via USR, virtual call sites
(``kind="virtual"`` + ``is_virtual``, resolved to the static target), operator
overload calls, and template declarations (``FUNCTION_TEMPLATE``/``CLASS_TEMPLATE``
records). libclang does not expose template instantiations as declarations, so
an instantiated call keeps the specialization USR (used for the callee id and a
store placeholder) but has no instantiated ``FunctionRecord``; inheritance
base-class lists are not representable in ``ca.core.schema.TypeRecord`` (no
``bases`` field) and are deferred to stage 3, where ``TypeRecord.bases`` should
be evaluated together with dynamic dispatch and template instances; constructor
calls are only visible as ``CALL_EXPR`` inside member initializer lists (plain
object definitions/``new`` are not calls); member operator calls count the
implicit object in ``arg_count``. A compile command
captured from a newer compiler may contain ``-Xclang`` passthrough options the
bundled libclang rejects; a compat replay drops only those, keeps the real
target, and records a note diagnostic.
"""

from __future__ import annotations

import functools
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

import clang.cindex as ci

from ca.core import ids
from ca.core.schema import (
    CallRecord,
    CompilationUnit,
    Diagnostic,
    FieldRecord,
    FunctionRecord,
    ParamRecord,
    ParseResult,
    Sloc,
    TypeRecord,
)
from ca.parse import cursor_utils as cu

_FUNCTION_KINDS = cu.FUNCTION_KINDS
_TYPE_KINDS = cu.TYPE_KINDS

# Arguments that only tell the compiler to emit files. libclang both ignores and
# rejects some of them (a real xv6 build's "-MD -o kernel/proc.o" produces
# "error opening 'kernel/proc.d'"), so they are removed before parsing.
_OUTPUT_FLAGS_WITH_VALUE = frozenset(
    {
        "-o",
        "-MF",
        "-MT",
        "-MQ",
        "-MJ",
        "-serialize-diagnostics",
        "-dependency-file",
        "-header-include-file",
        "-coverage-notes-file",
        "-coverage-data-file",
    }
)
_OUTPUT_FLAGS_NO_VALUE = frozenset(
    {
        "-c",
        "-S",
        "-E",
        "-MD",
        "-MMD",
        "-M",
        "-MM",
        "-MP",
        "-MG",
        "-MV",
        "-fsyntax-only",
    }
)
# Link-only flags. They cannot change the AST but libclang reports them as
# "argument unused during compilation"; with a build's -Werror that becomes an
# error diagnostic, so they are removed up front (the real xv6 database has
# "-fno-pie -no-pie" on every TU).
_LINK_ONLY_FLAGS = frozenset(
    {
        "-no-pie",
        "-fno-pie",
        "-pie",
        "-shared",
        "-static",
        "-rdynamic",
        "-nostartfiles",
        "-nodefaultlibs",
    }
)
_LINK_ONLY_PREFIXES = ("-Wl,", "-l", "-L", "-z")
# Target-selection flags removed by the host fallback when libclang cannot
# represent the build's cross target (it then parses the same C sources with the
# host target; the degradation is recorded as a note diagnostic).
_TARGET_VALUE_FLAGS = frozenset({"-target", "--target", "-arch"})
_MACHINE_FLAGS_WITH_VALUE = frozenset({"-mcpu", "-mtune", "-mfpu", "-mfloat-abi"})
# Positional arguments that are not source inputs of this TU (object files,
# dependency files, or the source path itself when the database embeds it).
_POSITIONAL_DROP_SUFFIXES = (".o", ".a", ".so", ".d", ".i", ".s", ".bc")
_IGNORABLE_ERROR_FLAGS = ("-Werror", "-Wfatal-errors", "-pedantic-errors")
# Instrumentation/profiling flags change codegen only, never the AST. Cross
# compilers (OH clang) pass flags whose support data the bundled libclang lacks
# (e.g. "missing sanitizer ignorelist"), which would otherwise fail the TU.
_CODEGEN_ONLY_PREFIXES = (
    "-fsanitize=",
    "-fsanitize-",
    "-fno-sanitize",
    "-fprofile",
    "-fno-profile",
    "-fcoverage",
    "-ftest-coverage",
    "-fno-coverage",
)
_SYSROOT_FLAGS = ("-isysroot", "-sysroot", "--sysroot")
_SEVERITY_NAMES = {0: "ignored", 1: "note", 2: "warning", 3: "error", 4: "fatal"}
_UNKNOWN_ARGUMENT_RE = re.compile(r"unknown argument: ['\u2018\u2019\"](?P<flag>-[^'\"\u2019]+)['\u2019\"]")
_UNKNOWN_WARNING_OPTION_RE = re.compile(r"unknown warning option ['\u2018\u2019\"](?P<flag>-W[^'\"\u2019]+)['\u2019\"]")
_MAX_OPTION_REPLAYS = 8
_GNU_DRIVER_RE = re.compile(r"(?:^|-)(?:gcc|g\+\+|cc|c\+\+)(?:-\d+(?:\.\d+)*)?(?:\.exe)?$")
_DRIVER_TRIPLE_RE = re.compile(
    r"^(?P<triple>[A-Za-z0-9_.]+(?:-[A-Za-z0-9_.]+){2,})"
    r"-(?:gcc|g\+\+|clang|clang\+\+|cc|c\+\+)"
    r"(?:-\d+(?:\.\d+){0,3})?$"
)

_UNSET = object()
_apple_sdk_cache: Any = _UNSET
_driver_include_cache: dict[str, str] = {}
_clang_resource_cache: dict[str, tuple[str, str, str]] = {}
# Compiler options already proven unsupported by this bundled libclang. OH-style
# databases repeat the same vendor flags on every TU; after the first diagnostic
# round learns them, later TUs strip them up front (still recorded as a note),
# which avoids a second full parse for every translation unit.
_learned_unknown_options: set[str] = set()
# One tiny probe parse per process learns the cross compiler's unsupported
# options up front, so even the first full TU needs only one parse.
_preflight_done = False


@dataclass
class ParseOptions:
    """Tunables for one parse; defaults are the production behavior.

    ``relax_on_errors`` retries without warning-as-error flags when the first
    attempt reports errors. ``infer_target``/``add_sysroot`` control the
    argument fixups described in the module docstring; ``host_fallback`` allows
    one last attempt with the cross target/machine flags stripped when this
    libclang build cannot represent the build's target at all.
    ``include_declarations``
    keeps declaration-only ``FunctionRecord``s (needed to resolve calls into
    headers that are not defined in this TU). ``record_scope`` selects the
    record set: ``"all"`` (default, every repo-local entity) or ``"main_file"``
    (functions/calls only for the TU's own file, types/fields stay for layout);
    ``emit_header_types=False`` additionally stops per-TU header type/field
    records (large header-heavy corpora; the AST still sees them). Both are
    independent switches with no schema change and are measured in the OH
    quality report. ``detect_macro_expansions`` enables the comparatively
    expensive token-based macro flag.
    """

    relax_on_errors: bool = True
    infer_target: bool = True
    add_sysroot: bool = True
    host_fallback: bool = True
    include_declarations: bool = True
    # "all" records every repo-local entity (headers included). "main_file"
    # records functions/calls only for the TU's own file (types/fields stay, they
    # describe layouts); large header-heavy corpora use it to cut per-TU
    # serialization substantially. Store-side dedupe is unchanged.
    record_scope: str = "all"
    # When False, TypeRecord/FieldRecord are only emitted for entities defined in
    # the TU's own file. Header definitions still exist in the AST for the
    # analyzer; this only cuts per-TU serialization on header-heavy corpora
    # (OpenHarmony C++ TUs otherwise emit ~3.5k header types each).
    emit_header_types: bool = True
    # Additive opt-in for query UX (find_symbol): under record_scope="main_file"
    # also emit repo-header function *declarations* (is_definition=False, with
    # signature/position/USR). Header definitions stay excluded, records are
    # deduplicated by function id within the TU (definitions win), and the cap
    # keeps header-heavy C++ TUs bounded. Default False keeps prior behavior.
    emit_header_declarations: bool = False
    header_declaration_limit: int = 2000
    # task-38 dependency contract: collect the TU's repo-relative include facts
    # (direct includes + repo-local include edges) into ParseResult. Vendored
    # toolchain trees (OH ``prebuilts/``) are excluded by default: they are
    # repo-local yet immutable, and their header graph dominates the edge set
    # (~620 of ~750 edges/TU measured on OH sample). ``include_edges_limit``
    # bounds pathological header graphs; truncation is recorded as a note.
    collect_include_files: bool = True
    include_path_excludes: tuple[str, ...] = ("prebuilts/",)
    include_edges_limit: int = 5000
    preflight_unknown_options: bool = True
    detect_macro_expansions: bool = False
    extra_args: tuple[str, ...] = ()
    clang_options: int = 0

    def __post_init__(self) -> None:
        self.extra_args = tuple(self.extra_args)
        if self.record_scope not in ("all", "main_file"):
            raise ValueError(f"unsupported record_scope: {self.record_scope!r}")
        self.include_path_excludes = tuple(self.include_path_excludes)
        if self.header_declaration_limit < 0:
            raise ValueError(
                f"header_declaration_limit must be >= 0: {self.header_declaration_limit!r}"
            )


class ParsedTranslationUnit:
    """Concrete :class:`ca.core.interfaces.ParsedUnit`.

    Records are detached dataclasses; the clang TU and cursors are kept as
    private live state for the analyzer and are never pickled.
    """

    def __init__(
        self,
        *,
        unit: CompilationUnit,
        clang_tu: ci.TranslationUnit | None,
        functions: Sequence[FunctionRecord],
        types: Sequence[TypeRecord],
        fields: Sequence[FieldRecord],
        calls: Sequence[CallRecord],
        diagnostics: Sequence[Diagnostic],
        definition_cursors: Mapping[str, ci.Cursor],
        parse_ok: bool,
        elapsed_ms: float,
        error: str = "",
        include_files: Sequence[str] = (),
        include_edges: Sequence[tuple[str, str]] = (),
    ) -> None:
        self.tu = unit
        self.functions = list(functions)
        self.types = list(types)
        self.fields = list(fields)
        self.calls = list(calls)
        self.diagnostics = list(diagnostics)
        self.parse_ok = parse_ok
        self.elapsed_ms = elapsed_ms
        self.error = error
        self.include_files = tuple(include_files)
        self.include_edges = tuple(include_edges)
        self._clang_tu = clang_tu
        self._definition_cursors = dict(definition_cursors)

    # -- live AST access ---------------------------------------------------- #

    def clang_tu(self) -> ci.TranslationUnit | None:
        return self._clang_tu

    def definition_cursor(self, function_id: str) -> ci.Cursor | None:
        return self._definition_cursors.get(function_id)

    # -- detached data ------------------------------------------------------ #

    def parse_result(self) -> ParseResult:
        return ParseResult(
            tu=self.tu,
            functions=list(self.functions),
            types=list(self.types),
            fields=list(self.fields),
            calls=list(self.calls),
            diagnostics=list(self.diagnostics),
            include_files=tuple(self.include_files),
            include_edges=tuple(self.include_edges),
            parse_ok=self.parse_ok,
            elapsed_ms=self.elapsed_ms,
            error=self.error,
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"ParsedTranslationUnit(file={self.tu.file!r}, ok={self.parse_ok}, "
            f"functions={len(self.functions)}, types={len(self.types)}, "
            f"calls={len(self.calls)}, diagnostics={len(self.diagnostics)})"
        )


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def parse_compilation_unit(
    tu: CompilationUnit,
    options: ParseOptions | Mapping[str, Any] | None = None,
) -> ParsedTranslationUnit:
    """Parse one translation unit and extract its records.

    Never raises for a broken TU: a failed parse returns a unit with
    ``parse_ok=False`` and ``error`` set, keeping whatever records were
    extracted before the failure. Unexpected programming errors still surface.
    """

    opts = _normalize_options(options)
    started = time.perf_counter()
    source = _absolute_source(tu)
    mapper = cu.PathMapper(
        tu.repo_root or os.path.dirname(source),
        base_dir=tu.directory or os.path.dirname(source),
    )

    if not os.path.isfile(source):
        return _failed_unit(tu, started, f"source file not found: {source}")

    # Faithful attempts first (real argv + target/sysroot fixups, then warnings
    # relaxed). A parse with zero error diagnostics stops the search, so the
    # common case costs a single libclang invocation; a loaded-but-imperfect
    # attempt is only accepted when no better faithful attempt follows.
    global _preflight_done
    attempts = _argument_attempts(tu, opts, source)
    if attempts and not _preflight_done:
        _preflight_done = True
        learned = _preflight_learn_unknown_options(attempts[0][1], source, opts)
        if learned:
            attempts = _argument_attempts(tu, opts, source)
    chosen, load_errors = _evaluate_attempts(attempts, source, opts, tu, mapper)

    # A captured command from a newer compiler may carry frontend passthrough
    # options that the bundled libclang rejects; drop only those and retry with
    # the real target and semantic flags intact.
    if chosen is None:
        compat_chosen, compat_errors = _evaluate_attempts(
            _compat_attempts(tu, opts, source), source, opts, tu, mapper
        )
        load_errors.extend(compat_errors)
        chosen = compat_chosen

    # Host-target recovery is a last resort for libclang builds that cannot
    # represent the build target at all; it is recorded as a note diagnostic and
    # is never used to paper over real diagnostics when a faithful parse loaded.
    if chosen is None:
        host_chosen, host_errors = _evaluate_attempts(
            _host_fallback_attempts(tu, opts, source), source, opts, tu, mapper
        )
        load_errors.extend(host_errors)
        chosen = host_chosen

    # Cross compiler vendors pass options the bundled libclang rejects (e.g. OH
    # clang's -fstack-protector-ret-strong / -fcfi-trap-function / -enable-...).
    # They arrive as error diagnostics while the AST is still usable, so remove
    # exactly those argv tokens, replay, and record the degradation.
    if chosen is not None and chosen[2] and opts.relax_on_errors:
        chosen = _strip_unknown_options(chosen, source, opts, tu, mapper)

    if chosen is None:
        detail = "; ".join(load_errors[-3:]) or "unknown load error"
        return _failed_unit(tu, started, f"libclang could not load TU: {detail}")

    clang_tu, diagnostics, errors, attempt_note, _attempt_arguments = chosen
    include_edges_truncated = False
    if opts.collect_include_files:
        include_files, include_edges, include_edges_truncated = _collect_include_facts(
            clang_tu,
            source,
            mapper,
            excludes=tuple(opts.include_path_excludes),
            edge_limit=int(opts.include_edges_limit),
        )
    else:
        include_files, include_edges = (), ()
    if attempt_note:
        diagnostics.append(Diagnostic(tu_id=tu.id, severity="note", message=attempt_note))
    learned_removed = [arg for arg in tu.arguments if arg in _learned_unknown_options]
    if learned_removed:
        diagnostics.append(
            Diagnostic(
                tu_id=tu.id,
                severity="note",
                message="removed compiler options previously found unsupported by the "
                "bundled libclang: " + _format_flags(learned_removed),
            )
        )
    toolchain_note = _toolchain_injection_note(tu, source)
    if toolchain_note:
        diagnostics.append(Diagnostic(tu_id=tu.id, severity="note", message=toolchain_note))
    dropped_codegen = _codegen_only_flags(tu.arguments)
    if dropped_codegen:
        diagnostics.append(
            Diagnostic(
                tu_id=tu.id,
                severity="note",
                message="ignored codegen-only options for AST parsing: "
                + _format_flags(dropped_codegen),
            )
        )

    extractor = _Extractor(tu, opts, mapper, mapper.relative(source) or "")
    parse_ok = not errors
    error = ""
    try:
        extractor.extract(clang_tu.cursor)
    except Exception as exc:  # pragma: no cover - defensive isolation
        parse_ok = False
        error = f"extraction failed: {type(exc).__name__}: {exc}"
    if include_edges_truncated:
        diagnostics.append(
            Diagnostic(
                tu_id=tu.id,
                severity="note",
                message="include edges truncated at "
                f"{opts.include_edges_limit} per TU "
                "(ParseOptions.include_edges_limit)",
            )
        )
    if extractor.header_declarations_truncated:
        diagnostics.append(
            Diagnostic(
                tu_id=tu.id,
                severity="note",
                message="header declarations truncated at "
                f"{opts.header_declaration_limit} per TU "
                "(ParseOptions.header_declaration_limit)",
            )
        )
    if extractor.skipped_cursors:
        diagnostics.append(
            Diagnostic(
                tu_id=tu.id,
                severity="note",
                message=f"skipped {extractor.skipped_cursors} unreadable AST cursor(s); "
                f"first error: {extractor.skipped_cursor_error[:160]}",
            )
        )

    if not error and errors:
        error = _summarize_errors(errors)

    elapsed_ms = 1000.0 * (time.perf_counter() - started)
    functions, types, fields, calls = extractor.finalize()
    return ParsedTranslationUnit(
        unit=tu,
        clang_tu=clang_tu,
        functions=functions,
        types=types,
        fields=fields,
        calls=calls,
        diagnostics=diagnostics,
        definition_cursors=extractor.definition_cursors,
        parse_ok=parse_ok,
        elapsed_ms=elapsed_ms,
        error=error,
        include_files=include_files,
        include_edges=include_edges,
    )


# --------------------------------------------------------------------------- #
# Argument preparation
# --------------------------------------------------------------------------- #


def _normalize_options(
    options: ParseOptions | Mapping[str, Any] | None,
) -> ParseOptions:
    if options is None:
        return ParseOptions()
    if isinstance(options, ParseOptions):
        return options
    if isinstance(options, Mapping):
        return ParseOptions(**dict(options))
    raise TypeError(f"unsupported parse options: {type(options)!r}")


def _absolute_source(tu: CompilationUnit) -> str:
    path = tu.file
    if not os.path.isabs(path):
        path = os.path.join(tu.directory or os.getcwd(), path)
    return os.path.abspath(path)


def _prepare_arguments(
    tu: CompilationUnit,
    options: ParseOptions,
    source: str,
    *,
    host_fallback: bool = False,
) -> list[str]:
    """Build the libclang argv for one attempt.

    ``host_fallback=True`` strips cross-target/machine flags and the inferred
    ``--target`` so this libclang build can still parse the C sources with its
    host target (only used when every faithful attempt failed to load).
    """

    directory = tu.directory or os.path.dirname(source)
    arguments = _sanitize_arguments(tu.arguments, source, directory)
    target = _explicit_target(arguments)
    if not target and options.infer_target:
        target = _inferred_target(tu, arguments)
    prefix: list[str] = []
    if directory and not any(arg == "-working-directory" for arg in arguments):
        prefix.extend(["-working-directory", os.path.abspath(directory)])

    if host_fallback:
        arguments = _strip_target_flags(arguments)
        if options.add_sysroot and _target_is_darwin("") and not _has_sysroot(arguments):
            sdk_path = _apple_sdk_path()
            if sdk_path:
                prefix.extend(["-isysroot", sdk_path])
        return prefix + arguments + list(options.extra_args)

    explicit = bool(_explicit_target(arguments))
    if target and not explicit:
        prefix.append("--target=" + target)
    # xpack GCC defaults to an rv32 ABI; the real build wrapper injects the ABI.
    # Make the rv64 ABI explicit for libclang when the database does not.
    if target == "riscv64-unknown-elf" and not any(
        arg.startswith("-mabi=") for arg in arguments
    ):
        prefix.append("-mabi=lp64d")

    if options.add_sysroot and _target_is_darwin(target):
        if not _has_sysroot(arguments):
            sdk_path = _apple_sdk_path()
            if sdk_path:
                prefix.extend(["-isysroot", sdk_path])

    # A cross compiler knows its own freestanding/builtin headers (stdarg.h,
    # stddef.h) through the driver, not through argv; the wheel's libclang has no
    # resource-dir headers, so ask the real driver once and pass the directories
    # as -isystem (GCC internal include for GCC, clang resource include for OHOS
    # / other clang cross toolchains).
    if options.infer_target and not _target_is_darwin(target):
        # Also covers host-tool TUs from OH databases: they carry no --target but
        # still use the OH clang toolchain (and thus its resource/libc++ headers).
        driver_path = _driver_executable(tu, source) or tu.driver
        resource_root, resource_include, libcxx_include = _clang_toolchain_includes(driver_path)
        if resource_root and not any(arg == "-resource-dir" for arg in arguments):
            prefix.extend(["-resource-dir", resource_root])
        include_dirs: list[str] = []
        gnu_include = _gnu_include_dir(driver_path)
        if gnu_include:
            include_dirs.append(gnu_include)
        if not resource_root and resource_include:
            # Only fall back to -isystem when -resource-dir could not be passed:
            # adding both makes clang 15 resolve libc++ headers against the
            # resource stddef/cstddef chain and breaks OH libc++ (nullptr_t /
            # __promote errors).
            include_dirs.append(resource_include)
        if libcxx_include and _unit_is_cpp(tu, source) and not _has_nostdinc(arguments):
            if not any(arg == libcxx_include for arg in arguments):
                include_dirs.append(libcxx_include)
                if not any(arg.startswith("-stdlib=") for arg in arguments):
                    prefix.append("-stdlib=libc++")
        if include_dirs and not _has_nostdinc(arguments):
            for include_dir in include_dirs:
                if not any(arg == include_dir for arg in arguments):
                    prefix.extend(["-isystem", include_dir])

    return prefix + arguments + list(options.extra_args)


_FRONTEND_PASSTHROUGH_FLAGS = frozenset(
    {"-Xclang", "-Xpreprocessor", "-Xassembler", "-Xlinker", "-mllvm", "-Xwarn"}
)


def _strip_target_flags(arguments: Sequence[str]) -> list[str]:
    """Remove target/ABI/machine flags for a host-target fallback parse."""

    stripped: list[str] = []
    skip_value = False
    for arg in arguments:
        if skip_value:
            skip_value = False
            continue
        if arg in _TARGET_VALUE_FLAGS or arg in ("-isysroot", "-sysroot", "--sysroot"):
            skip_value = True
            continue
        if arg.startswith(("--target=", "-target=", "-isysroot=", "-sysroot=", "--sysroot=")):
            continue
        if arg in _MACHINE_FLAGS_WITH_VALUE:
            skip_value = True
            continue
        if arg in _FRONTEND_PASSTHROUGH_FLAGS or arg.startswith(
            tuple(flag + "=" for flag in _FRONTEND_PASSTHROUGH_FLAGS)
        ):
            skip_value = arg in _FRONTEND_PASSTHROUGH_FLAGS
            continue
        if arg.startswith("-m"):
            continue
        stripped.append(arg)
    return stripped


def _strip_frontend_passthrough_flags(arguments: Sequence[str]) -> list[str]:
    """Drop ``-Xclang ...`` style passthrough options.

    A build database captured from a newer compiler may contain cc1 options
    (e.g. Xcode 21's ``-Xclang -clang-vendor-feature=...``) that an older
    bundled libclang rejects at load time. Dropping them keeps the real target
    and all semantic flags while restoring a loadable TU.
    """

    stripped: list[str] = []
    skip_value = False
    for arg in arguments:
        if skip_value:
            skip_value = False
            continue
        if arg in _FRONTEND_PASSTHROUGH_FLAGS:
            skip_value = True
            continue
        if arg.startswith(tuple(flag + "=" for flag in _FRONTEND_PASSTHROUGH_FLAGS)):
            continue
        stripped.append(arg)
    return stripped


def _format_flags(flags: Sequence[str], limit: int = 8) -> str:
    visible = " ".join(flags[:limit])
    if len(flags) > limit:
        visible += f" ... (+{len(flags) - limit})"
    return visible


def _codegen_only_flags(arguments: Sequence[str]) -> list[str]:
    return [
        arg
        for arg in arguments
        if arg in ("-fprofile-arcs", "-ftest-coverage") or arg.startswith(_CODEGEN_ONLY_PREFIXES)
    ]


_CPP_SUFFIXES = (".cpp", ".cc", ".cxx", ".c++", ".hpp", ".hh", ".hxx")


def _unit_is_cpp_source(source: str) -> bool:
    return os.path.splitext(source or "")[1].lower() in _CPP_SUFFIXES


def _unit_is_cpp(tu: CompilationUnit, source: str) -> bool:
    language = (tu.language or "").lower()
    if language.startswith(("c++", "cpp", "cxx")):
        return True
    return os.path.splitext(source or tu.file)[1].lower() in (
        ".cpp",
        ".cc",
        ".cxx",
        ".c++",
        ".hpp",
        ".hh",
        ".hxx",
    )


def _sanitize_arguments(
    arguments: Sequence[str],
    source: str,
    directory: str,
) -> list[str]:
    """Remove flags that make libclang write files or treat extra inputs.

    A real ``compile_commands.json`` command line repeats the source file and
    names produced object/dependency files; libclang receives the source path
    separately and rejects a second positional input, so those are dropped here.
    """

    source_key = os.path.normpath(source)
    source_name = os.path.basename(source)
    sanitized: list[str] = []
    skip_value = False
    for arg in arguments:
        if skip_value:
            skip_value = False
            continue
        if arg == "--":
            break
        if arg in _OUTPUT_FLAGS_WITH_VALUE:
            skip_value = True
            continue
        if any(arg.startswith(flag + "=") for flag in _OUTPUT_FLAGS_WITH_VALUE):
            continue
        if arg in _OUTPUT_FLAGS_NO_VALUE:
            continue
        if arg.startswith("-Wp,") or arg.startswith("-Wa,"):
            continue
        if arg in _LINK_ONLY_FLAGS or arg.startswith(_LINK_ONLY_PREFIXES):
            continue
        if arg.startswith(_CODEGEN_ONLY_PREFIXES) or arg in ("-fprofile-arcs", "-ftest-coverage"):
            continue
        if arg in _learned_unknown_options:
            continue
        if not arg.startswith("-"):
            if _is_same_input_file(arg, directory, source_key, source_name):
                continue
            if arg.lower().endswith(_POSITIONAL_DROP_SUFFIXES):
                continue
        sanitized.append(arg)
    return sanitized


def _is_same_input_file(arg: str, directory: str, source_key: str, source_name: str) -> bool:
    candidates = {os.path.normpath(arg)}
    if not os.path.isabs(arg):
        candidates.add(os.path.normpath(os.path.join(directory, arg)))
        candidates.add(os.path.normpath(os.path.abspath(arg)))
    if source_key in candidates:
        return True
    if _realpath(source_key) in {_realpath(candidate) for candidate in candidates}:
        return True
    # A bare basename only matches when the argument directory is the TU dir.
    return arg == source_name and os.path.normpath(directory) == os.path.dirname(source_key)


@functools.lru_cache(maxsize=4096)
def _realpath(path: str) -> str:
    try:
        return os.path.realpath(path)
    except OSError:  # pragma: no cover - defensive
        return path


def _explicit_target(arguments: Sequence[str]) -> str:
    for index, arg in enumerate(arguments):
        if arg.startswith("--target=") or arg.startswith("-target="):
            return arg.split("=", 1)[1]
        if arg in ("--target", "-target") and index + 1 < len(arguments):
            return arguments[index + 1]
    return ""


def _machine_flags(arguments: Sequence[str]) -> tuple[str, str]:
    march = ""
    mabi = ""
    for arg in arguments:
        if arg.startswith("-march="):
            march = arg.split("=", 1)[1]
        elif arg.startswith("-mabi="):
            mabi = arg.split("=", 1)[1]
    return march, mabi


def _inferred_target(tu: CompilationUnit, arguments: Sequence[str]) -> str:
    """Recover the cross target a GCC-style command implies.

    Real ``compile_commands.json`` entries usually do not contain ``--target``
    (the cross compiler knows its own triple), but libclang defaults to the host
    target and then rejects e.g. ``-march=rv64gc``/``-mcmodel=medany``.

    Toolchain prefixes are not always valid target triples: the xpack
    ``riscv-none-elf-gcc`` binary needs ``riscv64-unknown-elf`` /
    ``riscv32-unknown-elf`` (libclang rejects ``--target=riscv-none-elf``). The
    riscv prefix is therefore qualified with the ``-march``/``-mabi`` bitness.
    """

    march, mabi = _machine_flags(arguments)
    riscv32 = march.startswith("rv32") or mabi in ("ilp32", "ilp32d", "ilp32f")
    riscv64 = march.startswith("rv64") or mabi in ("lp64", "lp64d", "lp64f", "lp64q")
    driver = os.path.basename(tu.driver or "")
    match = _DRIVER_TRIPLE_RE.match(driver)
    if match:
        triple = match.group("triple")
        if triple.split("-")[0] == "riscv":
            # Toolchain prefix (e.g. xpack "riscv-none-elf-gcc") is not a target
            # triple and does not encode the ABI width; the ABI comes from
            # -march/-mabi and defaults to rv64 for bare-metal riscv drivers.
            return "riscv32-unknown-elf" if riscv32 else "riscv64-unknown-elf"
        return triple
    if "riscv" in driver.lower():
        return "riscv32-unknown-elf" if riscv32 else "riscv64-unknown-elf"
    if riscv64:
        return "riscv64-unknown-elf"
    if riscv32:
        return "riscv32-unknown-elf"
    return ""


def _target_is_darwin(target: str) -> bool:
    if not target:
        return sys.platform == "darwin"
    lowered = target.lower()
    return "darwin" in lowered or "apple" in lowered or "macos" in lowered


def _has_sysroot(arguments: Sequence[str]) -> bool:
    return any(
        arg in _SYSROOT_FLAGS or any(arg.startswith(flag + "=") for flag in _SYSROOT_FLAGS)
        for arg in arguments
    )


def _has_nostdinc(arguments: Sequence[str]) -> bool:
    return any(arg == "-nostdinc" for arg in arguments)


def _gnu_include_dir(driver: str) -> str:
    """Cached ``<driver> -print-file-name=include`` for GCC-style drivers."""

    cached = _driver_include_cache.get(driver)
    if cached is not None:
        return cached
    include_dir = ""
    executable = driver if os.path.isabs(driver) else shutil.which(driver)
    if executable and os.path.isfile(executable):
        if _GNU_DRIVER_RE.search(os.path.basename(executable)):
            try:
                completed = subprocess.run(
                    [executable, "-print-file-name=include"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):  # pragma: no cover - env dependent
                completed = None
            if completed is not None and completed.returncode == 0:
                candidate = completed.stdout.strip()
                if candidate and candidate != "include" and os.path.isdir(candidate):
                    include_dir = candidate
    _driver_include_cache[driver] = include_dir
    return include_dir


def _clang_toolchain_includes(driver: str) -> tuple[str, str, str]:
    """Cached (resource dir, resource include, libc++ include) of a clang toolchain.

    The PyPI libclang wheel ships only the shared library; compiler-provided
    headers (``stdarg.h``/``stddef.h``) and the C++ standard library live in the
    build toolchain's install tree. A cross driver (e.g. OpenHarmony's OHOS
    clang 15) resolves both implicitly, so libclang needs them passed explicitly:
    the resource dir is queried with ``-print-resource-dir`` and the libc++ tree
    is derived from it (``<llvm>/lib/clang/<ver>/include`` ->
    ``<llvm>/include/c++/v1``). Cached once per driver.
    """

    cached = _clang_resource_cache.get(driver)
    if cached is not None:
        return cached
    resource_dir = ""
    resource_include = ""
    libcxx_include = ""
    executable = driver if os.path.isabs(driver) else shutil.which(driver)
    if executable and os.path.isfile(executable):
        name = os.path.basename(executable).lower()
        if name.startswith("clang") or name in ("cc", "c++", "gcc", "g++"):
            try:
                completed = subprocess.run(
                    [executable, "-print-resource-dir"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):  # pragma: no cover - env dependent
                completed = None
            if completed is not None and completed.returncode == 0:
                resource_dir = completed.stdout.strip()
                candidate = os.path.join(resource_dir, "include")
                if os.path.isdir(candidate):
                    resource_include = candidate
                llvm_root = os.path.normpath(
                    os.path.join(resource_dir, "..", "..", "..")
                )
                # OH ships a configured libc++ under include/libcxx-ohos (with
                # the generated __config_site); the legacy include/c++/v1 tree
                # lacks it. Prefer the configured tree, keep legacy as fallback.
                candidates = [
                    os.path.join(llvm_root, "include", "libcxx-ohos", "include", "c++", "v1"),
                    os.path.join(llvm_root, "include", "c++", "v1"),
                ]
                configured = [path for path in candidates if os.path.isfile(os.path.join(path, "vector"))]
                preferred = [path for path in configured if os.path.isfile(os.path.join(path, "__config_site"))]
                if preferred:
                    libcxx_include = preferred[0]
                elif configured:
                    libcxx_include = configured[0]
    result = (resource_dir, resource_include, libcxx_include)
    _clang_resource_cache[driver] = result
    return result


def _apple_sdk_path() -> str:
    """Cached ``xcrun --show-sdk-path``; empty when unavailable (non-Apple CI)."""

    global _apple_sdk_cache
    if _apple_sdk_cache is not _UNSET:
        return _apple_sdk_cache
    path = ""
    if sys.platform == "darwin" and shutil.which("xcrun"):
        try:
            completed = subprocess.run(
                ["xcrun", "--show-sdk-path"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):  # pragma: no cover - env dependent
            completed = None
        if completed is not None and completed.returncode == 0:
            candidate = completed.stdout.strip()
            if candidate and os.path.isdir(candidate):
                path = candidate
    _apple_sdk_cache = path
    return path


def _relax_arguments(arguments: Sequence[str]) -> list[str]:
    return [
        arg
        for arg in arguments
        if not any(arg == flag or arg.startswith(flag + "=") for flag in _IGNORABLE_ERROR_FLAGS)
    ]


def _relaxed_note(before: Sequence[str], after: Sequence[str]) -> str:
    removed = [arg for arg in before if arg not in after]
    if not removed:
        return ""
    return "parse retried with relaxed warnings: removed " + " ".join(removed)


def _argument_attempts(
    tu: CompilationUnit,
    options: ParseOptions,
    source: str,
) -> list[tuple[str, list[str], str]]:
    """Faithful (label, argv, note) attempts in preference order.

    The first attempt uses the real build arguments plus target/sysroot fixups.
    The relaxed attempt only removes warning-control flags (no semantic flag is
    touched); it is skipped for strict callers.
    """

    real = _prepare_arguments(tu, options, source)
    attempts: list[tuple[str, list[str], str]] = [("real", real, "")]
    if not options.relax_on_errors:
        return attempts
    relaxed = _relax_arguments(real)
    if relaxed != real:
        attempts.append(("relaxed", relaxed, _relaxed_note(real, relaxed)))
    return attempts


def _compat_attempts(
    tu: CompilationUnit,
    options: ParseOptions,
    source: str,
) -> list[tuple[str, list[str], str]]:
    """Load-recovery attempts that drop frontend passthrough options only."""

    if not options.relax_on_errors:
        return []
    real = _prepare_arguments(tu, options, source)
    compat = _strip_frontend_passthrough_flags(real)
    if compat == real:
        return []
    removed = _removed_flags(real, compat)
    note = (
        "removed frontend passthrough options unsupported by the bundled "
        f"libclang: {_format_flags(removed)}"
    )
    attempts = [("compat", compat, note)]
    compat_relaxed = _relax_arguments(compat)
    if compat_relaxed != compat:
        attempts.append(("compat-relaxed", compat_relaxed, note))
    return attempts


def _host_fallback_attempts(
    tu: CompilationUnit,
    options: ParseOptions,
    source: str,
) -> list[tuple[str, list[str], str]]:
    """Last-resort attempts that strip cross-target flags (load recovery only)."""

    if not (options.relax_on_errors and options.host_fallback and options.infer_target):
        return []
    real = _prepare_arguments(tu, options, source)
    host = _prepare_arguments(tu, options, source, host_fallback=True)
    stripped = _strip_target_flags(_sanitize_arguments(tu.arguments, source, tu.directory))
    if stripped == _sanitize_arguments(tu.arguments, source, tu.directory):
        return []
    removed = _removed_flags(real, host)
    target = _explicit_target(real) or _inferred_target(tu, real)
    note = (
        f"cross target {target!r} could not be represented by this libclang; "
        f"parsed with host target (removed: {_format_flags(removed)})"
        if target
        else f"libclang could not load the TU with the real target; parsed with host target "
        f"(removed: {_format_flags(removed)})"
    )
    attempts = [("host", host, note)]
    host_relaxed = _relax_arguments(host)
    if host_relaxed != host:
        attempts.append(("host-relaxed", host_relaxed, note))
    return attempts


def _removed_flags(before: Sequence[str], after: Sequence[str]) -> list[str]:
    remaining = list(after)
    removed: list[str] = []
    for arg in before:
        if arg in remaining:
            remaining.remove(arg)
        else:
            removed.append(arg)
    return removed


# --------------------------------------------------------------------------- #
# libclang invocation and diagnostics
# --------------------------------------------------------------------------- #


def _load(
    source: str,
    arguments: Sequence[str],
    options: ParseOptions,
    unsaved_files: Sequence[tuple[str, str]] = (),
) -> ci.TranslationUnit:
    return cu.index().parse(
        source,
        args=list(arguments),
        unsaved_files=list(unsaved_files),
        options=options.clang_options,
    )


def _preflight_learn_unknown_options(
    arguments: Sequence[str],
    source: str,
    options: ParseOptions,
) -> list[str]:
    """Tiny probe parse that learns options this libclang build rejects.

    Runs once per process before the first real TU: compiling ``int ca_probe;``
    from an unsaved file with the TU's exact argv surfaces the cross compiler's
    vendor options in a few milliseconds, so the subsequent full parse succeeds
    in one attempt instead of a diagnostic-driven replay.
    """

    if not options.relax_on_errors or not options.preflight_unknown_options:
        return []
    suffix = ".cpp" if _unit_is_cpp_source(source) else ".c"
    probe_path = os.path.join(tempfile.gettempdir(), "ca_parse_preflight" + suffix)
    probe = None
    for probe_arguments in (
        arguments,
        _strip_frontend_passthrough_flags(arguments),
    ):
        try:
            probe = _load(
                probe_path,
                probe_arguments,
                options,
                unsaved_files=[(probe_path, "int ca_preflight_symbol;\n")],
            )
            break
        except ci.TranslationUnitLoadError:
            continue
    if probe is None:
        return []
    learned: list[str] = []
    for diagnostic in probe.diagnostics:
        try:
            severity_value = int(diagnostic.severity)
        except (TypeError, ValueError):
            continue
        if severity_value < 3:
            continue
        flags = _unknown_option_flags(
            [
                Diagnostic(
                    tu_id="",
                    severity="error",
                    message=diagnostic.spelling,
                )
            ]
        )
        for flag in flags:
            if flag not in _learned_unknown_options:
                _learned_unknown_options.add(flag)
                learned.append(flag)
    return learned


def _collect_diagnostics(
    clang_tu: ci.TranslationUnit,
    tu: CompilationUnit,
    mapper: cu.PathMapper,
) -> tuple[list[Diagnostic], list[Diagnostic]]:
    diagnostics: list[Diagnostic] = []
    errors: list[Diagnostic] = []
    for raw in clang_tu.diagnostics:
        try:
            severity_value = int(raw.severity)
        except (TypeError, ValueError):  # pragma: no cover - binding defensive
            severity_value = 3
        severity = _SEVERITY_NAMES.get(severity_value, "error")
        if severity == "ignored":
            continue
        # Driver's "argument unused during compilation" warning is escalated by
        # a build's -Werror; it says nothing about the parsed C code.
        if severity in ("error", "fatal") and "unused-command-line-argument" in raw.spelling:
            severity = "warning"
        record = Diagnostic(
            tu_id=tu.id,
            severity=severity,
            message=raw.spelling,
            sloc=mapper.sloc_for_location(raw.location),
        )
        diagnostics.append(record)
        if severity in ("error", "fatal"):
            errors.append(record)
    return diagnostics, errors


def _summarize_errors(errors: Iterable[Diagnostic]) -> str:
    messages: list[str] = []
    for record in errors:
        message = record.message.strip()
        if message and message not in messages:
            messages.append(message)
        if len(messages) >= 3:
            break
    count = len(list(errors))
    prefix = f"{count} error(s)" if count > 1 else "parse error"
    if not messages:
        return prefix
    return f"{prefix}: " + "; ".join(messages)


def _evaluate_attempts(
    attempts: Sequence[tuple[str, list[str], str]],
    source: str,
    options: ParseOptions,
    tu: CompilationUnit,
    mapper: cu.PathMapper,
) -> tuple[tuple[ci.TranslationUnit, list[Diagnostic], list[Diagnostic], str] | None, list[str]]:
    """Run attempts in order; prefer zero error diagnostics, else fewest errors."""

    chosen: tuple[ci.TranslationUnit, list[Diagnostic], list[Diagnostic], str, list[str]] | None = None
    load_errors: list[str] = []
    for label, arguments, note in attempts:
        try:
            candidate = _load(source, arguments, options)
        except ci.TranslationUnitLoadError as exc:
            load_errors.append(f"{label}: {exc}")
            continue
        diagnostics, errors = _collect_diagnostics(candidate, tu, mapper)
        if not errors:
            return (candidate, diagnostics, errors, note, list(arguments)), load_errors
        if chosen is None or len(errors) < len(chosen[2]):
            chosen = (candidate, diagnostics, errors, note, list(arguments))
        # -Werror escalation is the only thing a warning-relaxed replay fixes;
        # errors without a warning group (e.g. missing generated headers) would
        # fail identically, so stop instead of paying for another full TU.
        if not _tu_errors_relaxable(candidate):
            break
    return chosen, load_errors


def _tu_errors_relaxable(clang_tu: ci.TranslationUnit) -> bool:
    """Whether any error diagnostic is a warning escalated by -Werror etc."""

    for raw in clang_tu.diagnostics:
        try:
            if int(raw.severity) < 3:
                continue
        except (TypeError, ValueError):  # pragma: no cover - binding defensive
            continue
        try:
            if raw.option:
                return True
        except Exception:  # pragma: no cover - binding defensive
            pass
        if "unknown warning option" in raw.spelling:
            return True
    return False


def _unknown_option_flags(errors: Sequence[Diagnostic]) -> list[str]:
    """Compiler options rejected as unknown/unsupported by the bundled libclang."""

    flags: list[str] = []
    for record in errors:
        match = _UNKNOWN_ARGUMENT_RE.search(record.message)
        if match is None:
            match = _UNKNOWN_WARNING_OPTION_RE.search(record.message)
        if match is None:
            continue
        flag = match.group("flag")
        if flag and flag not in flags:
            flags.append(flag)
    return flags


def _strip_unknown_options(
    chosen: tuple[ci.TranslationUnit, list[Diagnostic], list[Diagnostic], str, list[str]],
    source: str,
    options: ParseOptions,
    tu: CompilationUnit,
    mapper: cu.PathMapper,
) -> tuple[ci.TranslationUnit, list[Diagnostic], list[Diagnostic], str, list[str]]:
    """Replay the winning attempt without compiler options this libclang rejects.

    Cross compiler vendors (e.g. OpenHarmony's OH clang 15) pass flags that the
    bundled libclang 18 does not know. libclang reports them as error diagnostics
    but still builds an AST. Each round removes only the exact argv tokens named
    by ``unknown argument``/``unknown warning option`` diagnostics (never a real
    parse error), retries, and records everything removed as a note diagnostic so
    the degradation is auditable. Semantic flags (target, sysroot, includes,
    -std, -D) are untouched.
    """

    current = chosen
    removed: list[str] = []
    for _ in range(_MAX_OPTION_REPLAYS):
        flags = _unknown_option_flags(current[2])
        sanitizer_error = any(
            "sanitizer" in record.message or "ignorelist" in record.message
            for record in current[2]
        )
        if not flags and not sanitizer_error:
            break
        removable = {flag for flag in flags if flag in current[4]}
        if sanitizer_error:
            removable.update(_codegen_only_flags(current[4]))
        if not removable:
            break
        arguments = [arg for arg in current[4] if arg not in removable]
        try:
            candidate = _load(source, arguments, options)
        except ci.TranslationUnitLoadError:
            break
        diagnostics, errors = _collect_diagnostics(candidate, tu, mapper)
        current = (candidate, diagnostics, errors, current[3], arguments)
        _learned_unknown_options.update(removable)
        removed.extend(sorted(removable))
        if not errors:
            break
    if removed:
        note = "removed compiler options unsupported by the bundled libclang: " + _format_flags(removed)
        current[1].append(Diagnostic(tu_id=tu.id, severity="note", message=note))
    return current


def _driver_executable(tu: CompilationUnit, source: str) -> str:
    """Absolute driver path; OH build databases store it relative to the build dir."""

    driver = tu.driver or ""
    if not driver:
        return ""
    candidates: list[str] = []
    if os.path.isabs(driver):
        candidates.append(driver)
    else:
        base = tu.directory or os.path.dirname(source)
        candidates.append(os.path.normpath(os.path.join(base, driver)))
        resolved = shutil.which(driver)
        if resolved:
            candidates.append(resolved)
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return ""


def _inclusion_file_name(file_value: Any) -> str:
    name = getattr(file_value, "name", "")
    return name if isinstance(name, str) else ""


def _excluded_include_path(path: str, excludes: tuple[str, ...]) -> bool:
    for prefix in excludes:
        if not prefix:
            continue
        normalized = prefix.rstrip("/")
        if path == normalized or path.startswith(normalized + "/"):
            return True
    return False


def _collect_include_facts(
    clang_tu: ci.TranslationUnit,
    source: str,
    mapper: cu.PathMapper,
    excludes: tuple[str, ...] = (),
    edge_limit: int = 0,
) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...], bool]:
    """Repo-relative include facts for the task-38 dependency contract.

    Returns ``(direct_includes_of_main_file, include_edges)``. Both are filtered
    to files under ``repo_root`` (system/generated-outside headers cannot be
    monitored incrementally), sorted and deduplicated. ``include_edges`` keeps
    header->header edges as ``(parent_file, included_file)``; the TU main file is
    never a target but may be a source.
    """

    try:
        # cindex exposes ``get_includes()`` (FileInclusion with source/include/
        # depth), not the C API name ``get_inclusions``.
        inclusions = list(clang_tu.get_includes())
    except Exception:  # pragma: no cover - binding defensive
        return (), (), False
    main_normalized = os.path.normpath(source)
    main_relative = mapper.relative(source) or ""
    direct: set[str] = set()
    edges: set[tuple[str, str]] = set()
    for inclusion in inclusions:
        target_file = getattr(inclusion, "include", None) or getattr(inclusion, "target", None)
        target_name = _inclusion_file_name(target_file)
        if not target_name:
            continue
        target_relative = mapper.relative(target_name)
        if not target_relative or target_relative == main_relative:
            continue
        if _excluded_include_path(target_relative, excludes):
            continue
        source_name = _inclusion_file_name(getattr(inclusion, "source", None))
        source_relative = mapper.relative(source_name) if source_name else None
        if source_relative and not _excluded_include_path(source_relative, excludes):
            edges.add((source_relative, target_relative))
        depth = getattr(inclusion, "depth", 0)
        try:
            is_direct = (source_name and os.path.normpath(source_name) == main_normalized) or int(depth) <= 1
        except (TypeError, ValueError):
            is_direct = bool(source_name and os.path.normpath(source_name) == main_normalized)
        if is_direct:
            direct.add(target_relative)
    ordered_edges = sorted(edges)
    truncated = False
    if edge_limit and len(ordered_edges) > edge_limit:
        ordered_edges = ordered_edges[:edge_limit]
        truncated = True
    return tuple(sorted(direct)), tuple(ordered_edges), truncated


def _toolchain_injection_note(tu: CompilationUnit, source: str) -> str:
    """Audit note when the parser supplies a cross toolchain's implicit headers."""

    driver_path = _driver_executable(tu, source)
    if not driver_path:
        return ""
    resource_root, resource_include, libcxx_include = _clang_toolchain_includes(driver_path)
    parts = []
    if resource_root:
        parts.append("resource-dir=" + resource_root)
    if libcxx_include and _unit_is_cpp(tu, source):
        parts.append("libc++=" + libcxx_include)
    if not parts:
        return ""
    return "using cross-toolchain headers not present in argv: " + " ".join(parts)


def _failed_unit(tu: CompilationUnit, started: float, error: str) -> ParsedTranslationUnit:
    return ParsedTranslationUnit(
        unit=tu,
        clang_tu=None,
        functions=(),
        types=(),
        fields=(),
        calls=(),
        diagnostics=(),
        definition_cursors={},
        parse_ok=False,
        elapsed_ms=1000.0 * (time.perf_counter() - started),
        error=error,
    )


# --------------------------------------------------------------------------- #
# AST extraction
# --------------------------------------------------------------------------- #


class _Extractor:
    """Single pre-order walk over the TU, collecting repo-local entities."""

    def __init__(
        self,
        unit: CompilationUnit,
        options: ParseOptions,
        mapper: cu.PathMapper,
        main_file: str = "",
    ) -> None:
        self.unit = unit
        self.options = options
        self.paths = mapper
        self.main_file = main_file
        self.functions: dict[str, FunctionRecord] = {}
        self.types: dict[str, TypeRecord] = {}
        self.fields: dict[str, FieldRecord] = {}
        self.calls: dict[str, CallRecord] = {}
        self.definition_cursors: dict[str, ci.Cursor] = {}
        self._field_index: dict[str, int] = {}
        self._field_keys: set[tuple[str, str, str, int, int]] = set()
        # Header-heavy C++ TUs call the same header entities thousands of times;
        # ids and qualified names are pure functions of USR/file, so cache them.
        self._function_id_cache: dict[tuple[str, str, str], str] = {}
        self._type_id_cache: dict[tuple[str, str, str], str] = {}
        self._callee_info_cache: dict[str, tuple[str, str, str]] = {}
        self.skipped_cursors = 0
        self.skipped_cursor_error = ""
        self.header_declaration_count = 0
        self.header_declarations_truncated = False

    # -- traversal ---------------------------------------------------------- #

    def extract(self, root: ci.Cursor) -> None:
        """Iterative pre-order walk that carries the enclosing function id.

        ``cursor.semantic_parent``/``lexical_parent`` are null for most
        expression cursors in libclang, so the enclosing function cannot be
        recovered from a ``CALL_EXPR`` after the fact; the walk tracks it.
        An explicit stack avoids Python recursion limits on large expressions.
        """

        stack: list[tuple[ci.Cursor, str]] = [(root, "")]
        while stack:
            cursor, current_function = stack.pop()
            try:
                kind = cursor.kind
            except Exception as exc:  # malformed/unreadable AST node
                self.skipped_cursors += 1
                if not self.skipped_cursor_error:
                    self.skipped_cursor_error = f"{type(exc).__name__}: {exc}"
                continue
            try:
                if kind in _FUNCTION_KINDS:
                    function_id = self._add_function(cursor)
                    context = function_id or current_function
                    if (
                        self.options.record_scope == "main_file"
                        and function_id
                        and function_id not in self.functions
                    ):
                        # Header inline/declaration bodies are not recorded in
                        # this scope; pruning them avoids walking the dominant
                        # part of large C++ TU ASTs.
                        continue
                    children = list(cursor.get_children())
                    for child in reversed(children):
                        stack.append((child, context))
                    continue
                if kind in _TYPE_KINDS:
                    self._add_type(cursor)
                elif kind == ci.CursorKind.FIELD_DECL:
                    self._add_field(cursor)
                elif kind == ci.CursorKind.CALL_EXPR and current_function:
                    self._add_call(cursor, current_function)
                children = list(cursor.get_children())
                for child in reversed(children):
                    stack.append((child, current_function))
            except Exception as exc:  # isolate one bad node from the whole TU
                self.skipped_cursors += 1
                if not self.skipped_cursor_error:
                    self.skipped_cursor_error = f"{type(exc).__name__}: {exc}"

    def finalize(
        self,
    ) -> tuple[list[FunctionRecord], list[TypeRecord], list[FieldRecord], list[CallRecord]]:
        def by_location(record: Any) -> tuple[str, int, int, str]:
            return (record.sloc.file, record.sloc.line, record.sloc.col, record.id)

        return (
            sorted(self.functions.values(), key=by_location),
            sorted(self.types.values(), key=by_location),
            sorted(self.fields.values(), key=by_location),
            sorted(self.calls.values(), key=by_location),
        )

    # -- identity helpers --------------------------------------------------- #

    def _function_identity(self, cursor: ci.Cursor) -> str:
        usr = cursor.get_usr() or ""
        linkage = cu.linkage_name(cursor)
        if usr:
            # External-linkage ids depend only on the USR; internal-linkage ids
            # add the repo-relative file (never the line/column).
            file_part = ""
            if linkage != "external":
                file_part = self.paths.relative_of_location(cursor.location) or ""
            cache_key = (usr, linkage, file_part)
            cached = self._function_id_cache.get(cache_key)
            if cached is None:
                cached = ids.function_id(
                    name=cursor.spelling,
                    usr=usr,
                    sloc=Sloc(file_part),
                    linkage=linkage,
                )
                self._function_id_cache[cache_key] = cached
            return cached
        return ids.function_id(
            name=cursor.spelling,
            usr="",
            sloc=self.paths.sloc_for_cursor(cursor),
            linkage=linkage,
        )

    def _type_identity(self, cursor: ci.Cursor) -> str:
        usr = cursor.get_usr() or ""
        kind = cu.type_kind_name(cursor)
        if usr:
            cache_key = (usr, kind, "")
            cached = self._type_id_cache.get(cache_key)
            if cached is None:
                cached = ids.type_id(name="", usr=usr, sloc=Sloc(""), kind=kind)
                self._type_id_cache[cache_key] = cached
            return cached
        name = "" if cursor.is_anonymous() else cursor.spelling
        return ids.type_id(
            name=name,
            usr="",
            sloc=self.paths.sloc_for_cursor(cursor),
            kind=kind,
        )

    @staticmethod
    def _enclosing_type(cursor: ci.Cursor) -> ci.Cursor | None:
        parent = cursor.semantic_parent
        if parent is not None and parent.kind in _TYPE_KINDS:
            return parent
        return None

    # -- functions ---------------------------------------------------------- #

    def _add_function(self, cursor: ci.Cursor) -> str:
        """Record a function declaration/definition; return its stable id."""

        if not self.paths.contains(cursor.location):
            return ""
        function_id = self._function_identity(cursor)
        is_definition = cursor.is_definition()
        if not is_definition and not self.options.include_declarations:
            return function_id
        header_declaration = False
        if self.options.record_scope == "main_file" and function_id:
            if self.paths.sloc_for_cursor(cursor).file != self.main_file:
                if is_definition or not self.options.emit_header_declarations:
                    return function_id
                header_declaration = True
        existing = self.functions.get(function_id)
        if existing is not None and not (is_definition and not existing.is_definition):
            return function_id
        is_virtual = False
        is_pure_virtual = False
        overrides: tuple[str, ...] = ()
        if cursor.kind == ci.CursorKind.CXX_METHOD:
            try:
                is_virtual = bool(cursor.is_virtual_method())
                is_pure_virtual = bool(cursor.is_pure_virtual_method())
            except Exception:  # pragma: no cover - binding defensive
                pass
            try:
                override_ids: list[str] = []
                for overridden in cu.overridden_cursors(cursor):
                    try:
                        override_id = self._function_identity(overridden)
                    except Exception:  # pragma: no cover - defensive
                        continue
                    if override_id and override_id != function_id and override_id not in override_ids:
                        override_ids.append(override_id)
                overrides = tuple(override_ids)
            except Exception:  # pragma: no cover - binding defensive
                overrides = ()
        if existing is not None:  # replacing a declaration with its definition
            is_virtual = is_virtual or existing.is_virtual
            is_pure_virtual = is_pure_virtual or existing.is_pure_virtual
            overrides = tuple(dict.fromkeys(existing.overrides + overrides))
        if header_declaration:
            limit = self.options.header_declaration_limit
            if limit and self.header_declaration_count >= limit:
                self.header_declarations_truncated = True
                return function_id
        sloc = self.paths.sloc_for_cursor(cursor)
        params = self._params(cursor)
        linkage = cu.linkage_name(cursor)
        kind = cu.function_kind_name(cursor)
        return_type = "" if kind in ("constructor", "destructor") else cursor.result_type.spelling
        storage_class = cu.storage_class_name(cursor)
        variadic = cu.is_variadic(cursor)
        is_const = cursor.is_const_method() if cursor.kind == ci.CursorKind.CXX_METHOD else False
        record = FunctionRecord(
            id=function_id,
            tu_id=self.unit.id,
            name=cursor.spelling,
            qualified_name=cu.qualified_name(cursor, cursor.spelling),
            usr=cursor.get_usr() or "",
            kind=kind,
            sloc=sloc,
            is_definition=is_definition,
            return_type=return_type,
            signature=_signature(cursor.spelling, return_type, params, variadic, is_const),
            display_name=cursor.displayname,
            storage_class=storage_class,
            linkage=linkage,
            is_static=storage_class == "static" or cu.is_static_method(cursor),
            is_inline=cu.is_function_inlined(cursor),
            is_variadic=variadic,
            is_virtual=is_virtual,
            is_pure_virtual=is_pure_virtual,
            overrides=overrides,
            parent_id=self._parent_function_id(cursor),
            params=params,
        )
        self.functions[function_id] = record
        self.definition_cursors[function_id] = cursor
        if header_declaration:
            self.header_declaration_count += 1
        return function_id

    def _parent_function_id(self, cursor: ci.Cursor) -> str:
        parent = self._enclosing_type(cursor)
        return self._type_identity(parent) if parent is not None else ""

    def _params(self, cursor: ci.Cursor) -> tuple[ParamRecord, ...]:
        params: list[ParamRecord] = []
        for child in cursor.get_children():
            if child.kind != ci.CursorKind.PARM_DECL:
                continue
            params.append(
                ParamRecord(
                    name=child.spelling,
                    type_spelling=child.type.spelling,
                    index=len(params),
                    sloc=self.paths.sloc_for_cursor(child),
                )
            )
        return tuple(params)

    # -- types and fields --------------------------------------------------- #

    def _base_type_ids(self, cursor: ci.Cursor) -> tuple[str, ...]:
        """Direct base class type ids of a C++ record cursor (declaration order)."""

        base_ids: list[str] = []
        for child in cursor.get_children():
            if child.kind != ci.CursorKind.CXX_BASE_SPECIFIER:
                continue
            try:
                declaration = child.type.get_declaration()
                if declaration is None or declaration.kind not in _TYPE_KINDS:
                    continue
                base_id = self._type_identity(declaration)
            except Exception:  # pragma: no cover - binding defensive
                continue
            if base_id and base_id not in base_ids:
                base_ids.append(base_id)
        return tuple(base_ids)

    def _add_type(self, cursor: ci.Cursor) -> None:
        if not self.paths.contains(cursor.location):
            return
        type_id_value = self._type_identity(cursor)
        if not self.options.emit_header_types:
            if self.paths.sloc_for_cursor(cursor).file != self.main_file:
                return
        is_definition = cursor.is_definition()
        is_anonymous = cursor.is_anonymous()
        name = "" if is_anonymous else cursor.spelling
        size_bytes, align_bytes = (-1, -1)
        if is_definition:
            size_bytes, align_bytes = cu.type_usize(cursor)
        parent = self._enclosing_type(cursor)
        record = TypeRecord(
            id=type_id_value,
            tu_id=self.unit.id,
            name=name,
            qualified_name=cu.qualified_name(cursor, name) if name else "",
            usr=cursor.get_usr() or "",
            kind=cu.type_kind_name(cursor),
            sloc=self.paths.sloc_for_cursor(cursor),
            is_definition=is_definition,
            size_bytes=size_bytes,
            align_bytes=align_bytes,
            underlying_type=cu.underlying_type_spelling(cursor),
            parent_id=self._type_identity(parent) if parent is not None else "",
            is_anonymous=is_anonymous,
            base_types=self._base_type_ids(cursor),
        )
        existing = self.types.get(type_id_value)
        if existing is None or _better_type(existing, record):
            self.types[type_id_value] = record

    def _add_field(self, cursor: ci.Cursor) -> None:
        if not self.paths.contains(cursor.location):
            return
        parent = self._enclosing_type(cursor)
        if parent is None or not self.paths.contains(parent.location):
            return
        if not self.options.emit_header_types:
            if self.paths.relative_of_location(cursor.location) != self.main_file:
                return
        type_id_value = self._type_identity(parent)
        sloc = self.paths.sloc_for_cursor(cursor)
        name = cursor.spelling
        dedupe_key = (type_id_value, name, sloc.file, sloc.line, sloc.col)
        if dedupe_key in self._field_keys:
            # libclang can hand out the same anonymous record/fields twice in
            # pre-order traversal; the first visit owns the index.
            return
        self._field_keys.add(dedupe_key)
        index = self._field_index.get(type_id_value, 0)
        self._field_index[type_id_value] = index + 1
        is_bitfield = bool(cursor.is_bitfield())
        bit_width = cu.bitfield_width(cursor) if is_bitfield else -1
        offset_bits = cursor.get_field_offsetof()
        field_id = ids.field_id(
            type_id_value=type_id_value,
            name=name,
            sloc=sloc,
            index=index,
        )
        self.fields[field_id] = FieldRecord(
            id=field_id,
            tu_id=self.unit.id,
            type_id=type_id_value,
            name=name,
            type_spelling=cursor.type.spelling,
            sloc=sloc,
            offset_bits=offset_bits if offset_bits >= 0 else -1,
            bit_width=bit_width if bit_width >= 0 else -1,
            is_static=cu.storage_class_name(cursor) == "static",
            is_bitfield=is_bitfield,
            index=index,
            access=cu.access_specifier_name(cursor),
        )

    # -- calls -------------------------------------------------------------- #

    @staticmethod
    def _is_this_member_call(cursor: ci.Cursor) -> bool:
        """True when a member call's receiver expression is the implicit ``this``.

        libclang represents both ``f()`` and ``this->f()`` as a CALL_EXPR whose
        callee MEMBER_REF_EXPR has a CXX_THIS_EXPR operand. A call through
        another expression (``p->f()``, ``obj.f()``) is not narrowed here.
        """

        children = list(cursor.get_children())
        if not children:
            return False
        callee = children[0]
        if callee.kind != ci.CursorKind.MEMBER_REF_EXPR:
            return False
        operands = list(callee.get_children())
        # Unqualified calls (``f()``) omit the implicit this operand entirely;
        # ``this->f()`` carries an explicit CXX_THIS_EXPR. Calls through another
        # expression (``p->f()``) carry that expression instead.
        return not operands or operands[0].kind == ci.CursorKind.CXX_THIS_EXPR

    def _add_call(self, cursor: ci.Cursor, caller_id: str) -> None:
        if not self.paths.contains(cursor.location):
            return
        if caller_id not in self.functions:
            # record_scope="main_file": header inline bodies are not recorded, so
            # their call sites have no owning FunctionRecord.
            return
        sloc = self.paths.sloc_for_cursor(cursor)
        reference = cursor.referenced
        callee_usr = ""
        callee_name = ""
        callee_qualified_name = ""
        callee_id = ""
        kind = "unknown"
        is_resolved = False
        is_virtual = False
        if reference is not None and reference.kind in _FUNCTION_KINDS:
            target = reference
            # USR-less functions fall back to name+location; point at the
            # definition when it is visible so call sites and records join.
            if not (target.get_usr() or ""):
                definition = target.get_definition()
                if definition is not None and definition.kind in _FUNCTION_KINDS:
                    target = definition
            callee_usr = target.get_usr() or ""
            cache_key = callee_usr or f"{target.spelling}@"
            cached_info = self._callee_info_cache.get(cache_key) if callee_usr else None
            if cached_info is None:
                cached_info = (
                    target.spelling,
                    cu.qualified_name(target, target.spelling),
                    self._function_identity(target),
                )
                if callee_usr:
                    self._callee_info_cache[cache_key] = cached_info
            callee_name, callee_qualified_name, callee_id = cached_info
            is_resolved = bool(callee_id)
            kind = "direct"
            try:
                is_virtual = bool(target.is_virtual_method())
            except Exception:  # pragma: no cover - C cursors have no virtual flag
                is_virtual = False
            if is_virtual:
                kind = "virtual_this" if self._is_this_member_call(cursor) else "virtual"
        elif reference is not None:
            callee_name = reference.spelling
            callee_qualified_name = cu.qualified_name(reference, reference.spelling)
            kind = "indirect"
        call_id = ids.call_id(
            caller_id=caller_id,
            callee_usr=callee_usr,
            callee_name=callee_name,
            sloc=sloc,
        )
        if call_id in self.calls:
            return
        is_macro_expansion = False
        if self.options.detect_macro_expansions:
            is_macro_expansion = cu.is_macro_expansion(cursor)
        self.calls[call_id] = CallRecord(
            id=call_id,
            tu_id=self.unit.id,
            caller_id=caller_id,
            sloc=sloc,
            callee_id=callee_id,
            callee_usr=callee_usr,
            callee_name=callee_name,
            callee_qualified_name=callee_qualified_name,
            kind=kind,
            is_resolved=is_resolved,
            is_virtual=is_virtual,
            is_macro_expansion=is_macro_expansion,
            arg_count=cu.call_argument_count(cursor),
        )


def _better_type(existing: TypeRecord, candidate: TypeRecord) -> bool:
    """Prefer a complete definition with a usable layout when ids collide."""

    if candidate.is_definition and not existing.is_definition:
        return True
    if existing.is_definition and not candidate.is_definition:
        return False
    return candidate.size_bytes > existing.size_bytes


def _signature(
    name: str,
    return_type: str,
    params: Sequence[ParamRecord],
    variadic: bool,
    is_const: bool = False,
) -> str:
    parts = [param.type_spelling for param in params]
    if variadic:
        parts.append("...")
    qualifier = " const" if is_const else ""
    if return_type:
        return f"{return_type} {name}({', '.join(parts)}){qualifier}"
    # Constructors/destructors have no return type in C++.
    return f"{name}({', '.join(parts)}){qualifier}"
