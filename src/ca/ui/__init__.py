"""Local read-only web UI for CA.

The package contains the stdlib HTTP JSON API server (:mod:`ca.ui.server`) and
the zero-dependency single-page frontend under ``static/``. The CLI entry point
is ``ca ui`` (see :func:`ca.ui.server.cmd_ui`); the server reuses the same
``ca.store`` query API as the CLI and MCP interface.
"""

__all__ = ["server"]
