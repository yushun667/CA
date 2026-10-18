"""Clang AST extraction: functions, types, fields, calls, source locations.

Contract (see ``ca.core.interfaces``)::

    parse_compilation_unit(tu, options=None) -> ParsedUnit

``ParsedUnit`` keeps the live ``clang.cindex.TranslationUnit`` so the analyzer
can walk the same AST; it never crosses a process boundary. Only the detached
``ParseResult`` is pickled.
"""

from __future__ import annotations

from ca.parse.libclang_parser import (
    ParseOptions,
    ParsedTranslationUnit,
    parse_compilation_unit,
)

__all__ = [
    "ParseOptions",
    "ParsedTranslationUnit",
    "parse_compilation_unit",
]
