"""Multi-repo workspace / project / variant metadata.

A *workspace* is one checked-out multi-repo tree (OpenHarmony example:
``/home/yu/CA_OH/openharmony`` with ~500 ``.repo`` projects). A *variant* is a
buildable configuration of that workspace (``product=opi5plus``,
``board=rk3588``, ``version=<current release>``) and may point at its own index
database (``db_path``). Records are metadata only: no code records live here.

IDs are store-local and deterministic (``ws:`` / ``prj:`` / ``var:`` prefixes
over :func:`ca.core.ids.digest`), so re-registering the same workspace/project is
idempotent. The core schema contract is untouched.

Cross-database queries are provided by :class:`ca.store.federated.FederatedStore`
(read-only union of shard DBs). ``attach_database()`` remains the low-level seam
for ad-hoc union SQL under a validated alias; per-variant index DBs follow the
convention ``<workspace>/.ca/index-<variant>.db`` and are recorded in
``variants.db_path``.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from ca.core.ids import digest

_ALIAS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

WORKSPACE_COLUMNS = ("id", "path", "name", "kind", "metadata_json", "created_at")
PROJECT_COLUMNS = (
    "id",
    "workspace_id",
    "path",
    "name",
    "repo",
    "product",
    "board",
    "version",
    "kind",
    "metadata_json",
)
VARIANT_COLUMNS = (
    "id",
    "workspace_id",
    "name",
    "product",
    "board",
    "version",
    "db_path",
    "is_active",
    "metadata_json",
)


def workspace_id(path: str) -> str:
    """Stable id for a workspace path."""

    return "ws:" + digest(_normalize_path(path))


def project_id(workspace_id_value: str, path: str) -> str:
    return "prj:" + digest(workspace_id_value, _normalize_path(path))


def variant_id(
    workspace_id_value: str,
    name: str,
    product: str = "",
    board: str = "",
    version: str = "",
) -> str:
    return "var:" + digest(workspace_id_value, name, product, board, version)


def _normalize_path(path: str) -> str:
    return str(path).rstrip("/") or "/"


def _metadata(metadata: Mapping[str, Any] | None) -> str:
    return json.dumps(dict(metadata or {}), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decode_metadata(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class WorkspaceRegistry:
    """Workspace/project/variant CRUD over an open store connection."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection

    # ------------------------------------------------------------------ #
    # Writes
    # ------------------------------------------------------------------ #
    def upsert_workspace(
        self,
        path: str,
        name: str = "",
        kind: str = "local",
        metadata: Mapping[str, Any] | None = None,
    ) -> str:
        """Insert or update one workspace; returns its stable ``ws:`` id."""

        if not str(path).strip():
            raise ValueError("workspace path must not be empty")
        identifier = workspace_id(str(path))
        self._conn.execute(
            "INSERT INTO workspaces (id, path, name, kind, metadata_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET path = excluded.path, name = excluded.name, "
            "kind = excluded.kind, metadata_json = excluded.metadata_json",
            (
                identifier,
                _normalize_path(str(path)),
                name,
                kind,
                _metadata(metadata),
                _now(),
            ),
        )
        return identifier

    def register_projects(
        self,
        workspace_id_value: str,
        projects: Iterable[Mapping[str, Any]],
        *,
        replace: bool = False,
    ) -> int:
        """Insert/update projects for a workspace; returns the number written.

        Each project mapping may contain ``path`` (required), ``name``, ``repo``,
        ``product``, ``board``, ``version``, ``kind`` and ``metadata``.
        ``replace=True`` deletes previously registered projects of the workspace
        first, which is the natural mode for a full ``.repo`` manifest refresh.
        """

        if self.get_workspace(workspace_id_value) is None:
            raise ValueError(f"unknown workspace: {workspace_id_value!r}")
        rows: list[tuple[Any, ...]] = []
        for project in projects:
            path = str(project.get("path", "")).strip()
            if not path:
                raise ValueError("project path must not be empty")
            rows.append(
                (
                    project_id(workspace_id_value, path),
                    workspace_id_value,
                    _normalize_path(path),
                    str(project.get("name", "")),
                    str(project.get("repo", "")),
                    str(project.get("product", "")),
                    str(project.get("board", "")),
                    str(project.get("version", "")),
                    str(project.get("kind", "repo")),
                    _metadata(project.get("metadata")),  # type: ignore[arg-type]
                )
            )
        if replace:
            self._conn.execute(
                "DELETE FROM projects WHERE workspace_id = ?", (workspace_id_value,)
            )
        self._conn.executemany(
            f"INSERT INTO projects ({', '.join(PROJECT_COLUMNS)}) "
            f"VALUES ({', '.join('?' for _ in PROJECT_COLUMNS)}) "
            "ON CONFLICT(id) DO UPDATE SET path = excluded.path, name = excluded.name, "
            "repo = excluded.repo, product = excluded.product, board = excluded.board, "
            "version = excluded.version, kind = excluded.kind, "
            "metadata_json = excluded.metadata_json",
            rows,
        )
        return len(rows)

    def register_variant(
        self,
        workspace_id_value: str,
        name: str,
        product: str = "",
        board: str = "",
        version: str = "",
        db_path: str = "",
        metadata: Mapping[str, Any] | None = None,
        is_active: bool = False,
    ) -> str:
        """Insert or update one build variant; returns its ``var:`` id."""

        if self.get_workspace(workspace_id_value) is None:
            raise ValueError(f"unknown workspace: {workspace_id_value!r}")
        if not str(name).strip():
            raise ValueError("variant name must not be empty")
        identifier = variant_id(workspace_id_value, name, product, board, version)
        self._conn.execute(
            "INSERT INTO variants (id, workspace_id, name, product, board, version, db_path, "
            "is_active, metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET workspace_id = excluded.workspace_id, "
            "name = excluded.name, product = excluded.product, board = excluded.board, "
            "version = excluded.version, db_path = excluded.db_path, "
            "is_active = excluded.is_active, metadata_json = excluded.metadata_json",
            (
                identifier,
                workspace_id_value,
                name,
                product,
                board,
                version,
                db_path,
                int(bool(is_active)),
                _metadata(metadata),
            ),
        )
        return identifier

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #
    def get_workspace(self, identifier: str) -> dict[str, Any] | None:
        """Look a workspace up by ``ws:`` id or by path."""

        row = self._conn.execute(
            "SELECT * FROM workspaces WHERE id = ? OR path = ? LIMIT 1",
            (identifier, _normalize_path(str(identifier))),
        ).fetchone()
        return _workspace_row(row) if row is not None else None

    def list_workspaces(self, limit: int = 0) -> list[dict[str, Any]]:
        sql = "SELECT * FROM workspaces ORDER BY path"
        if limit > 0:
            sql += " LIMIT ?"
            return [_workspace_row(row) for row in self._conn.execute(sql, (limit,))]
        return [_workspace_row(row) for row in self._conn.execute(sql)]

    def list_projects(self, workspace_id_value: str, limit: int = 0) -> list[dict[str, Any]]:
        sql = "SELECT * FROM projects WHERE workspace_id = ? ORDER BY path"
        params: list[Any] = [workspace_id_value]
        if limit > 0:
            sql += " LIMIT ?"
            params.append(limit)
        return [_project_row(row) for row in self._conn.execute(sql, params)]

    def list_variants(self, workspace_id_value: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM variants WHERE workspace_id = ? ORDER BY name, product, board, version",
            (workspace_id_value,),
        )
        return [_variant_row(row) for row in rows]

    def get_variant(self, variant_id_value: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM variants WHERE id = ?", (variant_id_value,)
        ).fetchone()
        return _variant_row(row) if row is not None else None

    def workspace_summary(self, identifier: str) -> dict[str, Any]:
        """Workspace dict plus project/variant counts and distinct products."""

        workspace = self.get_workspace(identifier)
        if workspace is None:
            return {"workspace": None, "projects": 0, "variants": 0, "products": []}
        identifier = workspace["id"]
        projects = self._conn.execute(
            "SELECT COUNT(*) FROM projects WHERE workspace_id = ?", (identifier,)
        ).fetchone()[0]
        variants = self.list_variants(identifier)
        products = sorted({variant["product"] for variant in variants if variant["product"]})
        return {
            "workspace": workspace,
            "projects": int(projects),
            "variants": len(variants),
            "products": products,
        }

    # ------------------------------------------------------------------ #
    # Federation skeleton (interface reservation only)
    # ------------------------------------------------------------------ #
    def attach_database(self, db_path: str, alias: str) -> None:
        """Attach another index database for ad-hoc union queries.

        This is the reserved seam for per-variant / per-workspace databases:
        ``ATTACH DATABASE ? AS <alias>``; alias is validated as a plain SQL
        identifier (values never interpolated). No orchestration is performed.
        """

        if not _ALIAS_RE.match(alias):
            raise ValueError(f"invalid database alias: {alias!r}")
        self._conn.execute("ATTACH DATABASE ? AS " + alias, (str(db_path),))

    def detach_database(self, alias: str) -> None:
        if not _ALIAS_RE.match(alias):
            raise ValueError(f"invalid database alias: {alias!r}")
        self._conn.execute("DETACH DATABASE " + alias)

    def attached_databases(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("PRAGMA database_list").fetchall()
        return [{"name": row[1], "path": row[2]} for row in rows]


# --------------------------------------------------------------------------- #
# Row encoders
# --------------------------------------------------------------------------- #


def _workspace_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["metadata"] = _decode_metadata(data.pop("metadata_json", None))
    return data


def _project_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["metadata"] = _decode_metadata(data.pop("metadata_json", None))
    return data


def _variant_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["metadata"] = _decode_metadata(data.pop("metadata_json", None))
    data["is_active"] = bool(data.get("is_active", 0))
    return data
