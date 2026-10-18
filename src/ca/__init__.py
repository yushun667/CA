"""CA: operating-system source analyzer.

Pipeline: real compile args -> Clang AST -> CFG/data-flow -> queryable database.
The ``ca.core`` package owns the versioned data contract shared by every stage.
"""

__version__ = "0.1.0"
