"""Workspace/project/variant registry commands and routing helpers.

The registry tables (``workspaces``/``projects``/``variants``) live in an index
database. These commands expose CRUD for that metadata and resolve a variant's
``db_path`` for ``ca query``/``ca graph``. Registry rows contain metadata only;
switching a variant never writes index rows or the target database.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterable

from ca.cli import format as fmt


class RegistryError(RuntimeError):
    """A user-facing registry selection/validation error."""


def _db_path(args: Any) -> Path:
    raw = getattr(args, "db", None)
    if not raw or (isinstance(raw, list) and not raw):
        raise RegistryError("a registry database is required (--db)")
    value = raw[0] if isinstance(raw, list) else raw
    path = Path(str(value)).expanduser()
    if not path.is_file():
        raise RegistryError(f"database not found: {path}")
    return path.resolve()


def _read_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    connection.row_factory = sqlite3.Row
    return connection


def _require_tables(connection: sqlite3.Connection, path: Path) -> None:
    names = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    missing = {"workspaces", "projects", "variants"} - names
    if missing:
        raise RegistryError(
            f"{path} has no workspace registry (missing: {', '.join(sorted(missing))})"
        )


def _open_store(path: Path) -> Any:
    from ca.store import SqliteStore

    store = SqliteStore(str(path))
    store.open()
    return store


def _iter_variants(registry: Any) -> Iterable[tuple[dict[str, Any], dict[str, Any]]]:
    for workspace in registry.list_workspaces():
        for variant in registry.list_variants(workspace["id"]):
            yield workspace, variant


def resolve_variant(registry: Any, selector: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve a variant by exact ``var:`` id or (unique) name."""

    selector = str(selector or "").strip()
    if not selector:
        raise RegistryError("variant selector must not be empty")
    matches = [
        (workspace, variant)
        for workspace, variant in _iter_variants(registry)
        if variant["id"] == selector or variant["name"] == selector
    ]
    if not matches:
        raise RegistryError(f"variant not found: {selector}")
    if len(matches) > 1:
        ids = ", ".join(variant["id"] for _workspace, variant in matches)
        raise RegistryError(
            f"variant name {selector!r} is ambiguous; use one of: {ids}"
        )
    return matches[0]


def variant_db_path(registry_db: str | Path, selector: str) -> str:
    """Resolve a variant's index DB path from the registry DB (read-only)."""

    path = Path(str(registry_db)).expanduser()
    if not path.is_file():
        raise RegistryError(f"registry database not found: {path}")
    connection = _read_connection(path.resolve())
    try:
        _require_tables(connection, path.resolve())
        from ca.store.workspace import WorkspaceRegistry

        registry = WorkspaceRegistry(connection)
        _workspace, variant = resolve_variant(registry, selector)
        raw = str(variant.get("db_path") or "").strip()
        if not raw:
            raise RegistryError(f"variant {variant['id']} has no db_path")
        target = Path(raw).expanduser()
        if not target.is_absolute():
            target = path.resolve().parent / target
        target = target.resolve()
        if not target.is_file():
            raise RegistryError(
                f"variant database not found for {variant['id']}: {target}"
            )
        return str(target)
    finally:
        connection.close()


def _query_target(args: Any) -> str | None:
    """Resolve ``--variant`` for query/graph; ``None`` without the option."""

    selector = getattr(args, "variant", None)
    if not selector:
        return None
    db_values = getattr(args, "db", None)
    if getattr(args, "db_dir", None):
        raise RegistryError("--variant requires --db <registry-db>, not --db-dir")
    values = db_values if isinstance(db_values, list) else ([db_values] if db_values else [])
    if len(values) != 1:
        raise RegistryError("--variant requires exactly one --db <registry-db>")
    return variant_db_path(values[0], str(selector))


def _print_error(exc: Exception) -> int:
    print(f"error: {exc}", file=sys.stderr)
    return 1


def _json_or_table(rows: list[dict[str, Any]], args: Any, columns: list[str]) -> None:
    if getattr(args, "json_output", False):
        fmt.write_json({"items": rows, "count": len(rows)})
    elif rows:
        fmt.write_table(rows, columns=columns)


def _workspace_columns() -> list[str]:
    return ["id", "path", "name", "kind", "created_at"]


def _project_columns() -> list[str]:
    return ["id", "workspace_id", "path", "name", "repo", "product", "board", "version"]


def _variant_columns() -> list[str]:
    return ["id", "workspace_id", "name", "product", "board", "version", "db_path", "is_active"]


def _variant_summary(variant: dict[str, Any], registry_db: Path) -> dict[str, Any]:
    data = dict(variant)
    raw = str(data.get("db_path") or "").strip()
    target = Path(raw).expanduser() if raw else None
    if target is not None and not target.is_absolute():
        target = registry_db.parent / target
    data["db_exists"] = bool(target and target.is_file())
    return data


def _cmd_workspace(args: Any) -> int:
    action = getattr(args, "registry_action", "")
    path = _db_path(args)
    if action == "add":
        store = _open_store(path)
        try:
            store.begin()
            identifier = store.upsert_workspace(
                args.path,
                getattr(args, "name", None) or Path(args.path).expanduser().name,
                getattr(args, "kind", None) or "local",
            )
            store.commit()
        finally:
            store.close()
        if getattr(args, "json_output", False):
            fmt.write_json({"workspace": {"id": identifier}})
        else:
            print(identifier)
        return 0
    if action == "list":
        store = _open_store(path)
        try:
            rows = store.list_workspaces(limit=getattr(args, "limit", 0) or 0)
        finally:
            store.close()
        _json_or_table(rows, args, _workspace_columns())
        return 0
    if action == "show":
        store = _open_store(path)
        try:
            summary = store.workspace_summary(args.target)
        finally:
            store.close()
        if summary.get("workspace") is None:
            return _print_error(RegistryError(f"workspace not found: {args.target}"))
        if getattr(args, "json_output", False):
            fmt.write_json(summary)
        else:
            fmt.write_key_values(
                {
                    "workspace": summary["workspace"],
                    "projects": summary["projects"],
                    "variants": summary["variants"],
                    "products": summary["products"],
                }
            )
        return 0
    if action == "remove":
        store = _open_store(path)
        try:
            store.begin()
            counts = store.remove_workspace(args.target)
            store.commit()
        finally:
            store.close()
        if counts is None:
            return _print_error(RegistryError(f"workspace not found: {args.target}"))
        if getattr(args, "json_output", False):
            fmt.write_json({"removed": counts})
        else:
            print(
                f"removed workspace {args.target}: projects={counts['projects']}"
                f" variants={counts['variants']}"
            )
        return 0
    return _print_error(RegistryError(f"unknown workspace action: {action}"))


def _cmd_project(args: Any) -> int:
    action = getattr(args, "registry_action", "")
    path = _db_path(args)
    store = _open_store(path)
    try:
        if action == "add":
            workspace = store.get_workspace(args.workspace)
            if workspace is None:
                return _print_error(RegistryError(f"workspace not found: {args.workspace}"))
            project_path = str(args.path)
            store.begin()
            store.register_projects(
                workspace["id"],
                [
                    {
                        "path": project_path,
                        "name": getattr(args, "name", None)
                        or Path(project_path).name,
                        "repo": getattr(args, "repo", None) or "",
                        "product": getattr(args, "product", None) or "",
                        "board": getattr(args, "board", None) or "",
                        "version": getattr(args, "version", None) or "",
                    }
                ],
            )
            store.commit()
            from ca.store.workspace import project_id

            identifier = project_id(workspace["id"], project_path)
            if getattr(args, "json_output", False):
                fmt.write_json({"project": {"id": identifier, "workspace_id": workspace["id"]}})
            else:
                print(identifier)
            return 0
        if action == "list":
            workspaces = (
                [store.get_workspace(args.workspace)]
                if getattr(args, "workspace", None)
                else store.list_workspaces()
            )
            if any(workspace is None for workspace in workspaces):
                return _print_error(RegistryError(f"workspace not found: {args.workspace}"))
            rows: list[dict[str, Any]] = []
            for workspace in workspaces:
                assert workspace is not None
                rows.extend(store.list_projects(workspace["id"]))
            _json_or_table(rows, args, _project_columns())
            return 0
        if action == "remove":
            store.begin()
            removed = store.remove_project(args.target)
            store.commit()
            if not removed:
                return _print_error(RegistryError(f"project not found: {args.target}"))
            if getattr(args, "json_output", False):
                fmt.write_json({"removed": {"project": args.target}})
            else:
                print(f"removed project {args.target}")
            return 0
        return _print_error(RegistryError(f"unknown project action: {action}"))
    finally:
        store.close()


def _cmd_variant(args: Any) -> int:
    action = getattr(args, "registry_action", "")
    path = _db_path(args)
    store = _open_store(path)
    try:
        if action == "add":
            workspace = store.get_workspace(args.workspace)
            if workspace is None:
                return _print_error(RegistryError(f"workspace not found: {args.workspace}"))
            store.begin()
            identifier = store.register_variant(
                workspace["id"],
                args.name,
                product=getattr(args, "product", None) or "",
                board=getattr(args, "board", None) or "",
                version=getattr(args, "version", None) or "",
                db_path=getattr(args, "db_path", None) or "",
            )
            store.commit()
            if getattr(args, "json_output", False):
                fmt.write_json({"variant": store.get_variant(identifier)})
            else:
                print(identifier)
            return 0
        if action == "list":
            workspaces = (
                [store.get_workspace(args.workspace)]
                if getattr(args, "workspace", None)
                else store.list_workspaces()
            )
            if any(workspace is None for workspace in workspaces):
                return _print_error(RegistryError(f"workspace not found: {args.workspace}"))
            rows: list[dict[str, Any]] = []
            for workspace in workspaces:
                assert workspace is not None
                for variant in store.list_variants(workspace["id"]):
                    rows.append(_variant_summary(variant, path))
            _json_or_table(rows, args, _variant_columns() + ["db_exists"])
            return 0
        if action in ("show", "use"):
            workspace, variant = resolve_variant(store, args.target)
            if action == "use":
                store.begin()
                active = store.set_active_variant(workspace["id"], variant["id"])
                store.commit()
                if active is None:
                    return _print_error(
                        RegistryError(f"variant not found: {args.target}")
                    )
                variant = dict(variant)
                variant["is_active"] = True
            data = _variant_summary(variant, path)
            if getattr(args, "json_output", False):
                fmt.write_json({"variant": data, "workspace": workspace["id"]})
            else:
                fmt.write_key_values(
                    {"workspace": workspace["id"], "variant": data}
                )
            return 0
        if action == "remove":
            _workspace, variant = resolve_variant(store, args.target)
            store.begin()
            removed = store.remove_variant(variant["id"])
            store.commit()
            if not removed:
                return _print_error(RegistryError(f"variant not found: {args.target}"))
            if getattr(args, "json_output", False):
                fmt.write_json({"removed": {"variant": variant["id"]}})
            else:
                print(f"removed variant {variant['id']}")
            return 0
        return _print_error(RegistryError(f"unknown variant action: {action}"))
    finally:
        store.close()


def cmd_registry(args: Any) -> int:
    """Dispatch one ``ca workspace|project|variant ...`` command."""

    command = getattr(args, "command", "")
    try:
        if command == "workspace":
            return _cmd_workspace(args)
        if command == "project":
            return _cmd_project(args)
        if command == "variant":
            return _cmd_variant(args)
        return _print_error(RegistryError(f"unknown registry command: {command}"))
    except RegistryError as exc:
        return _print_error(exc)
    except Exception as exc:  # keep CLI exit codes stable
        return _print_error(RegistryError(f"registry error: {type(exc).__name__}: {exc}"))


# --------------------------------------------------------------------------- #
# Index-time registration
# --------------------------------------------------------------------------- #


def _registry_options_present(args: Any) -> bool:
    names = (
        "workspace_path",
        "workspace_name",
        "project_path",
        "project_name",
        "project_repo",
        "project_product",
        "project_board",
        "project_version",
        "variant_name",
        "variant_product",
        "variant_board",
        "variant_version",
        "variant_db",
    )
    return any(getattr(args, name, None) for name in names)


def validate_index_options(args: Any) -> None:
    """Validate registry flags before the expensive indexing stage."""

    if not _registry_options_present(args):
        return
    if not getattr(args, "workspace_path", None):
        raise RegistryError(
            "registry options require --workspace-path (projects/variants belong to a workspace)"
        )
    project_options = any(
        getattr(args, name, None)
        for name in (
            "project_path",
            "project_name",
            "project_repo",
            "project_product",
            "project_board",
            "project_version",
        )
    )
    variant_options = any(
        getattr(args, name, None)
        for name in (
            "variant_name",
            "variant_product",
            "variant_board",
            "variant_version",
            "variant_db",
        )
    )
    if variant_options and not getattr(args, "variant_name", None):
        raise RegistryError("variant options require --variant-name")
    if project_options and not (
        getattr(args, "project_path", None) or getattr(args, "project_name", None)
    ):
        raise RegistryError("project options require --project-path or --project-name")


def register_index(db_path: str | Path, repo: str, args: Any) -> dict[str, Any]:
    """Write workspace/project/variant rows into the freshly indexed DB."""

    if not _registry_options_present(args):
        return {}
    workspace_path = str(getattr(args, "workspace_path", "") or "").strip()
    if not workspace_path:
        raise RegistryError("--workspace-path is required for registry registration")
    workspace_name = getattr(args, "workspace_name", None) or Path(
        workspace_path
    ).expanduser().name
    project_path = getattr(args, "project_path", None)
    project_name = getattr(args, "project_name", None)
    variant_name = getattr(args, "variant_name", None)
    store = _open_store(Path(str(db_path)))
    try:
        store.begin()
        workspace_identifier = store.upsert_workspace(workspace_path, workspace_name)
        project_identifier = ""
        if project_path or project_name:
            project_path = project_path or repo
            project_identifier = store.register_projects(
                workspace_identifier,
                [
                    {
                        "path": str(project_path),
                        "name": project_name or Path(str(project_path)).name,
                        "repo": getattr(args, "project_repo", None) or repo,
                        "product": getattr(args, "project_product", None) or "",
                        "board": getattr(args, "project_board", None) or "",
                        "version": getattr(args, "project_version", None) or "",
                    }
                ],
            )
            from ca.store.workspace import project_id

            project_identifier = project_id(workspace_identifier, str(project_path))
        variant_identifier = ""
        if variant_name:
            variant_identifier = store.register_variant(
                workspace_identifier,
                variant_name,
                product=getattr(args, "variant_product", None) or "",
                board=getattr(args, "variant_board", None) or "",
                version=getattr(args, "variant_version", None) or "",
                db_path=getattr(args, "variant_db", None) or str(Path(str(db_path)).resolve()),
            )
        store.commit()
    finally:
        store.close()
    return {
        "workspace_id": workspace_identifier,
        "project_id": project_identifier,
        "variant_id": variant_identifier,
    }


def query_target(args: Any) -> str | None:
    return _query_target(args)
