"""Shared contracts: records, stable IDs, and cross-module interfaces.

This package is Lead-owned. Changing a record field or an ID rule is a contract
change: update the decision record and notify every module owner.
"""

from ca.core.schema import (  # noqa: F401
    SCHEMA_VERSION,
    AssignmentRecord,
    AnalysisResult,
    BranchRecord,
    CallRecord,
    CallTargetRecord,
    CfgBlockRecord,
    CfgEdgeRecord,
    CompilationUnit,
    Diagnostic,
    FieldRecord,
    FindingRecord,
    FunctionRecord,
    IndexSummary,
    ParamRecord,
    ParseResult,
    ResourceOpRecord,
    ReturnRecord,
    Sloc,
    TuWorkResult,
    TypeRecord,
)
