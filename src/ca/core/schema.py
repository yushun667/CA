"""Versioned data contract exchanged by every CA stage.

Rules that all producers must follow:

- Paths are POSIX and relative to the analyzed repository root when possible.
  ``CompilationUnit`` keeps absolute paths because the parser needs them.
- Lines and columns are 1-based; ``0`` means unknown. Ranges use ``end_*``.
- Unknown scalar values use ``""`` or ``-1`` instead of ``None`` so records stay
  JSON friendly and database columns stay simple.
- ``id`` values are stable across runs and machines; compute them with
  :mod:`ca.core.ids`, never with ad-hoc formatting.
- Adding/renaming a field or changing an ID rule is a contract change: record a
  decision, update every module, and bump ``SCHEMA_VERSION``.

The analysis pipeline is: ``ca.build`` produces ``CompilationUnit`` objects,
``ca.parse`` produces ``ParseResult`` (functions/types/fields/calls), ``ca.analyze``
produces one ``AnalysisResult`` per function definition, and ``ca.store``
persists all records.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, ClassVar, Iterable, Iterator

SCHEMA_VERSION = "2"
TOOL_VERSION = "0.1.0"


# --------------------------------------------------------------------------- #
# Source locations and translation units
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Sloc:
    """A source range. ``file`` is repo-relative; line/col are 1-based."""

    file: str
    line: int = 0
    col: int = 0
    end_line: int = 0
    end_col: int = 0

    def is_valid(self) -> bool:
        return bool(self.file) and self.line > 0


@dataclass
class CompilationUnit:
    """One translation unit with the real compile arguments from the build.

    ``arguments`` is the exact argv (without the executable) captured from
    ``compile_commands.json`` or an intercepted build. ``source`` records where
    the command came from so synthetic flags are never mistaken for real ones.
    """

    KIND: ClassVar[str] = "compilation_unit"

    id: str
    file: str
    directory: str
    repo_root: str
    arguments: tuple[str, ...] = ()
    language: str = "c"
    source: str = "compile_commands"
    driver: str = ""


@dataclass
class Diagnostic:
    KIND: ClassVar[str] = "diagnostic"

    tu_id: str
    severity: str = "error"
    message: str = ""
    sloc: Sloc = field(default_factory=lambda: Sloc(""))


# --------------------------------------------------------------------------- #
# AST entities
# --------------------------------------------------------------------------- #


@dataclass
class ParamRecord:
    KIND: ClassVar[str] = "param"

    name: str
    type_spelling: str = ""
    index: int = 0
    sloc: Sloc = field(default_factory=lambda: Sloc(""))
    default_value: str = ""


@dataclass
class FunctionRecord:
    """A function definition or declaration.

    Identity: external-linkage functions use the Clang USR alone, internal
    linkage adds the file, and USR-less cursors fall back to name+location.
    Use :func:`ca.core.ids.function_id` to compute ``id``.
    """

    KIND: ClassVar[str] = "function"

    id: str
    tu_id: str
    name: str
    qualified_name: str
    usr: str
    kind: str  # function | method | constructor | destructor | conversion | unknown
    sloc: Sloc
    is_definition: bool = False
    return_type: str = ""
    signature: str = ""
    display_name: str = ""
    storage_class: str = ""
    linkage: str = "unknown"  # external | internal | none | unknown
    is_static: bool = False
    is_inline: bool = False
    is_variadic: bool = False
    is_virtual: bool = False
    is_pure_virtual: bool = False
    # Function ids (ca.core.ids.function_id) of the overridden declarations, in
    # declaration order. Stable across TUs because ids are USR based.
    overrides: tuple[str, ...] = ()
    parent_id: str = ""
    params: tuple[ParamRecord, ...] = ()


@dataclass
class TypeRecord:
    KIND: ClassVar[str] = "type"

    id: str
    tu_id: str
    name: str
    qualified_name: str
    usr: str
    kind: str  # struct | union | enum | class | typedef | unknown
    sloc: Sloc
    is_definition: bool = False
    size_bytes: int = -1
    align_bytes: int = -1
    underlying_type: str = ""
    parent_id: str = ""
    is_anonymous: bool = False
    # Direct base class/struct type ids (USR based); empty for C types. Multiple
    # inheritance is preserved in declaration order.
    base_types: tuple[str, ...] = ()


@dataclass
class FieldRecord:
    KIND: ClassVar[str] = "field"

    id: str
    tu_id: str
    type_id: str
    name: str
    type_spelling: str
    sloc: Sloc
    offset_bits: int = -1
    bit_width: int = -1
    is_static: bool = False
    is_bitfield: bool = False
    index: int = 0
    access: str = ""


@dataclass
class CallRecord:
    """A call site. ``callee_id`` may reference a placeholder declaration node."""

    KIND: ClassVar[str] = "call"

    id: str
    tu_id: str
    caller_id: str
    sloc: Sloc
    callee_id: str = ""
    callee_usr: str = ""
    callee_name: str = ""
    callee_qualified_name: str = ""
    kind: str = "direct"  # direct | indirect | virtual | macro | unknown
    is_resolved: bool = False
    is_virtual: bool = False
    is_macro_expansion: bool = False
    arg_count: int = 0


# --------------------------------------------------------------------------- #
# CFG and data-flow facts
# --------------------------------------------------------------------------- #


@dataclass
class CallTargetRecord:
    """A resolved or candidate target of a call site.

    Phase-3 callback/function-pointer/virtual resolution emits these edges when
    a call site cannot be resolved directly. ``kind`` distinguishes direct
    resolution from type/registration/override-based candidates. A record is a
    member of the *possible target set* for the call site, never a probability
    estimate: ``reason`` records the auditable evidence (registration site,
    override relationship, argument passing, ...) that produced the candidate.
    """

    KIND: ClassVar[str] = "call_target"

    id: str
    call_id: str
    caller_id: str
    callee_id: str
    callee_name: str
    kind: str = "indirect_candidate"  # direct | indirect_candidate | virtual_candidate
    reason: str = ""
    sloc: Sloc = field(default_factory=lambda: Sloc(""))


@dataclass
class CfgBlockRecord:
    KIND: ClassVar[str] = "cfg_block"

    id: str
    function_id: str
    index: int
    kind: str  # entry | exit | normal | branch | switch | loop_header | handler
    terminator: str = "none"  # none | unconditional | conditional | return | switch | goto | indirect
    sloc: Sloc = field(default_factory=lambda: Sloc(""))
    statement_count: int = 0


@dataclass
class CfgEdgeRecord:
    KIND: ClassVar[str] = "cfg_edge"

    id: str
    function_id: str
    src: str
    dst: str
    kind: str = "unconditional"  # unconditional | true | false | switch_case | switch_default
    label: str = ""


@dataclass
class BranchRecord:
    """A control-flow decision, including its source-level condition."""

    KIND: ClassVar[str] = "branch"

    id: str
    function_id: str
    block_id: str
    sloc: Sloc
    kind: str  # if | else_if | switch | ternary | logical_and | logical_or | loop | while | for | do_while | case
    condition: str = ""
    var_names: tuple[str, ...] = ()
    true_block: str = ""
    false_block: str = ""
    is_negated: bool = False
    case_value: str = ""


@dataclass
class AssignmentRecord:
    KIND: ClassVar[str] = "assignment"

    id: str
    function_id: str
    block_id: str
    sloc: Sloc
    lhs: str
    rhs: str
    var_names: tuple[str, ...] = ()
    is_decl_init: bool = False
    operator: str = "="
    type_spelling: str = ""


@dataclass
class ReturnRecord:
    KIND: ClassVar[str] = "return"

    id: str
    function_id: str
    block_id: str
    sloc: Sloc
    expr: str = ""
    var_names: tuple[str, ...] = ()
    is_implicit: bool = False


@dataclass
class ResourceOpRecord:
    """Acquire/release of OS resources (memory, locks, files, refcounts...)."""

    KIND: ClassVar[str] = "resource_op"

    id: str
    function_id: str
    block_id: str
    sloc: Sloc
    op: str  # acquire | release | copy | other
    resource_kind: str  # memory | file | lock | refcount | irq | socket | other
    api_name: str
    target: str = ""
    target_vars: tuple[str, ...] = ()
    call_id: str = ""
    matcher: str = ""  # table entry that matched, for auditability


@dataclass
class FindingRecord:
    """A data-flow result worth surfacing (e.g. a resource leak path)."""

    KIND: ClassVar[str] = "finding"

    id: str
    function_id: str
    kind: str  # leak | double_release | balanced | use_after_release | unreleased
    resource_kind: str
    message: str = ""
    severity: str = "warning"
    acquire_id: str = ""
    release_ids: tuple[str, ...] = ()
    path_blocks: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()


# --------------------------------------------------------------------------- #
# Stage results
# --------------------------------------------------------------------------- #


@dataclass
class ParseResult:
    KIND: ClassVar[str] = "parse_result"

    tu: CompilationUnit
    functions: list[FunctionRecord] = field(default_factory=list)
    types: list[TypeRecord] = field(default_factory=list)
    fields: list[FieldRecord] = field(default_factory=list)
    calls: list[CallRecord] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    # task-38 dependency contract (repo-relative POSIX paths, sorted, deduped):
    # ``include_files`` is the TU main file's direct includes (repo-local only,
    # main file excluded); ``include_edges`` are all repo-local include edges
    # observed in this TU, ``(parent_file, included_file)`` including
    # header->header edges. Both are TU-level facts independent of
    # ``ParseOptions.record_scope`` and are empty when collection is disabled or
    # the TU failed to load.
    include_files: tuple[str, ...] = ()
    include_edges: tuple[tuple[str, str], ...] = ()
    parse_ok: bool = True
    elapsed_ms: float = 0.0
    error: str = ""


@dataclass
class AnalysisResult:
    KIND: ClassVar[str] = "analysis_result"

    function_id: str
    cfg_blocks: list[CfgBlockRecord] = field(default_factory=list)
    cfg_edges: list[CfgEdgeRecord] = field(default_factory=list)
    branches: list[BranchRecord] = field(default_factory=list)
    assignments: list[AssignmentRecord] = field(default_factory=list)
    returns: list[ReturnRecord] = field(default_factory=list)
    resource_ops: list[ResourceOpRecord] = field(default_factory=list)
    findings: list[FindingRecord] = field(default_factory=list)
    call_targets: list[CallTargetRecord] = field(default_factory=list)
    elapsed_ms: float = 0.0


@dataclass
class TuWorkResult:
    """Worker output for one TU: parse records plus analyses of its functions."""

    KIND: ClassVar[str] = "tu_work_result"

    parse: ParseResult
    analyses: list[AnalysisResult] = field(default_factory=list)
    error: str = ""


@dataclass
class IndexSummary:
    """Observable result of one indexing run; used by CLI and benchmarks."""

    KIND: ClassVar[str] = "index_summary"

    repo_root: str
    db_path: str
    files: int = 0
    tus_total: int = 0
    tus_parsed: int = 0
    tus_failed: int = 0
    functions: int = 0
    types: int = 0
    calls: int = 0
    analyzed_functions: int = 0
    branches: int = 0
    assignments: int = 0
    returns: int = 0
    resource_ops: int = 0
    findings: int = 0
    parse_seconds: float = 0.0
    analyze_seconds: float = 0.0
    write_seconds: float = 0.0
    total_seconds: float = 0.0
    lines_of_code: int = 0
    errors: tuple[str, ...] = ()


# --------------------------------------------------------------------------- #
# JSON serialization helpers
# --------------------------------------------------------------------------- #


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


def record_to_dict(record: Any) -> dict[str, Any]:
    """Convert a record to a JSON-friendly dict with ``kind`` and ``schema``."""

    data = _jsonable(record)
    if not isinstance(data, dict) or not dataclasses.is_dataclass(record):
        raise TypeError(f"not a record dataclass: {type(record)!r}")
    data["kind"] = getattr(type(record), "KIND", type(record).__name__)
    data["schema"] = SCHEMA_VERSION
    return data


def iter_record_dicts(
    parse_result: ParseResult,
    analyses: Iterable[AnalysisResult] = (),
) -> Iterator[dict[str, Any]]:
    """Flatten a parse result plus analyses into a stream of record dicts."""

    yield record_to_dict(parse_result)
    for bucket in (
        parse_result.functions,
        parse_result.types,
        parse_result.fields,
        parse_result.calls,
        parse_result.diagnostics,
    ):
        for record in bucket:
            yield record_to_dict(record)
    for analysis in analyses:
        yield record_to_dict(analysis)
        for bucket in (
            analysis.cfg_blocks,
            analysis.cfg_edges,
            analysis.branches,
            analysis.assignments,
            analysis.returns,
            analysis.resource_ops,
            analysis.findings,
            analysis.call_targets,
        ):
            for record in bucket:
                yield record_to_dict(record)
