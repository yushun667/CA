"""CFG construction and data-flow analysis over the parsed AST.

Contract (see ``ca.core.interfaces``)::

    analyze_function(unit, function, options=None) -> AnalysisResult

Produces basic blocks/edges, branch conditions, assignments, return paths,
resource operations, and data-flow findings (leaks, double release). Analysis
must be deterministic: record order and IDs may not depend on hash iteration.

Precision is intraprocedural: cross-function ownership transfer, function
pointers, macro wrappers and exception paths are not modeled. Every resource
record keeps the annotation-table ``matcher`` that produced it, so false
positives can be audited against :mod:`ca.analyze.resources`.
"""

from ca.analyze.function_analyzer import AnalyzeOptions, analyze_function
from ca.analyze.resources import ResourceApi, ResourceConfig

__all__ = ["AnalyzeOptions", "ResourceApi", "ResourceConfig", "analyze_function"]
