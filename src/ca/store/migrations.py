"""Idempotent physical migrations for databases created by older CA versions.

Schema version 8 removes the ``confidence`` columns from ``call_targets`` and
``callback_registrations`` (candidate targets are a *set*, not probabilities).
New databases are created without those columns by ``schema.sql``; existing
databases are upgraded in place when :meth:`ca.store.SqliteStore.open` runs.

Migration is best effort and idempotent, with three outcomes per table:

1. ``dropped`` — ``ALTER TABLE ... DROP COLUMN confidence`` (SQLite >= 3.35).
2. ``rebuilt`` — the drop failed (e.g. a legacy index references the column);
   the table is safely copied into a new table without ``confidence``, verified
   by row count, swapped in, and its non-legacy indexes recreated.
3. ``kept`` — both paths failed: the legacy column is left in place and a
   warning is logged. The store keeps working because readers select explicit
   columns and writers do not bind the column (its ``DEFAULT`` fills it).

Read-only federated shards are never migrated: all store SQL selects explicit
columns, so a shard that still has the legacy column is served normally and the
API simply never returns ``confidence``. No automatic ``VACUUM`` is performed
(dropped-column space is reclaimed by a later explicit VACUUM).
"""

from __future__ import annotations

import logging
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

logger = logging.getLogger(__name__)

_SCHEMA_FILE = Path(__file__).with_name("schema.sql")

# table -> legacy column removed by schema v8.
LEGACY_COLUMNS: Mapping[str, str] = {
    "call_targets": "confidence",
    "callback_registrations": "confidence",
}

_QUOTED_TABLE = '"{}"'
_QUOTED_COLUMN = '"{}"'


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    try:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
            (table,),
        ).fetchone()
    except sqlite3.Error:  # pragma: no cover - defensive
        return False
    return row is not None


def table_columns(connection: sqlite3.Connection, table: str) -> list[str]:
    """Column names for ``table`` in definition order (``[]`` if missing)."""

    try:
        rows = connection.execute(f"PRAGMA table_info({_QUOTED_TABLE.format(table)})").fetchall()
    except sqlite3.Error:  # pragma: no cover - defensive
        return []
    return [str(row[1]) for row in rows]


def _canonical_ddl(table: str) -> str:
    """Extract the canonical (v8) ``CREATE TABLE`` statement from schema.sql."""

    text = _SCHEMA_FILE.read_text(encoding="utf-8")
    pattern = re.compile(
        r"CREATE TABLE IF NOT EXISTS\s+" + re.escape(table) + r"\s*\([^;]*\);",
        re.S,
    )
    match = pattern.search(text)
    if match is None:  # pragma: no cover - schema.sql and this module drifted
        raise RuntimeError(f"schema.sql has no definition for table {table!r}")
    return match.group(0)


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Iterator[None]:
    """Own transaction, or a savepoint when the caller already opened one."""

    if connection.in_transaction:
        connection.execute("SAVEPOINT ca_schema_migration")
        try:
            yield
        except BaseException:
            connection.execute("ROLLBACK TO ca_schema_migration")
            connection.execute("RELEASE ca_schema_migration")
            raise
        else:
            connection.execute("RELEASE ca_schema_migration")
        return
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    else:
        connection.execute("COMMIT")


def _drop_column(connection: sqlite3.Connection, table: str, column: str) -> None:
    with _transaction(connection):
        connection.execute(
            f"ALTER TABLE {_QUOTED_TABLE.format(table)} DROP COLUMN {_QUOTED_COLUMN.format(column)}"
        )


def _index_ddl(connection: sqlite3.Connection, table: str) -> list[str]:
    rows = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = ? "
        "AND sql IS NOT NULL ORDER BY name",
        (table,),
    ).fetchall()
    return [str(row[0]) for row in rows]


def _recreate_indexes(
    connection: sqlite3.Connection, table: str, statements: list[str]
) -> list[str]:
    skipped: list[str] = []
    for statement in statements:
        if "confidence" in statement.lower():
            # Legacy custom index on the removed column: cannot exist on v8.
            skipped.append(statement)
            continue
        try:
            connection.execute(statement)
        except sqlite3.Error as exc:  # pragma: no cover - defensive
            logger.warning("could not recreate index on %s: %s", table, exc)
            skipped.append(statement)
    return skipped


def _rebuild_table(
    connection: sqlite3.Connection, table: str, column: str, columns: list[str]
) -> list[str]:
    """Copy the table without ``column`` and swap it in; returns skipped indexes."""

    keep = [name for name in columns if name != column]
    if not keep:  # pragma: no cover - every CA table has more columns
        raise sqlite3.DatabaseError(f"{table} has no columns besides {column}")
    temp = f"__ca_migrate_{table}"
    ddl = _canonical_ddl(table)
    temp_ddl = ddl.replace(
        f"CREATE TABLE IF NOT EXISTS {table}", f"CREATE TABLE {temp}", 1
    )
    if temp_ddl == ddl:  # pragma: no cover - defensive
        raise sqlite3.DatabaseError(f"cannot derive rebuild DDL for {table}")
    indexes = _index_ddl(connection, table)
    quoted_keep = ", ".join(_QUOTED_COLUMN.format(name) for name in keep)
    with _transaction(connection):
        connection.execute(f"DROP TABLE IF EXISTS {_QUOTED_TABLE.format(temp)}")
        before = int(
            connection.execute(f"SELECT COUNT(*) FROM {_QUOTED_TABLE.format(table)}").fetchone()[0]
        )
        connection.execute(temp_ddl)
        connection.execute(
            f"INSERT INTO {_QUOTED_TABLE.format(temp)} ({quoted_keep}) "
            f"SELECT {quoted_keep} FROM {_QUOTED_TABLE.format(table)}"
        )
        after = int(
            connection.execute(f"SELECT COUNT(*) FROM {_QUOTED_TABLE.format(temp)}").fetchone()[0]
        )
        if after != before:
            raise sqlite3.DatabaseError(
                f"{table} rebuild copied {after} of {before} rows"
            )
        connection.execute(f"DROP TABLE {_QUOTED_TABLE.format(table)}")
        connection.execute(
            f"ALTER TABLE {_QUOTED_TABLE.format(temp)} RENAME TO {_QUOTED_TABLE.format(table)}"
        )
    return _recreate_indexes(connection, table, indexes)


def _migrate_table(connection: sqlite3.Connection, table: str, column: str) -> str:
    if not _table_exists(connection, table):
        return "missing"
    columns = table_columns(connection, table)
    if column not in columns:
        return "clean"
    try:
        _drop_column(connection, table, column)
        logger.info("schema v8: dropped legacy %s.%s", table, column)
        return "dropped"
    except sqlite3.OperationalError as exc:
        logger.info(
            "schema v8: DROP COLUMN failed on %s.%s (%s); rebuilding table",
            table,
            column,
            exc,
        )
    except sqlite3.Error:  # pragma: no cover - non-Operational drop failures
        logger.warning("schema v8: unexpected DROP COLUMN failure on %s", table, exc_info=True)
    try:
        skipped = _rebuild_table(connection, table, column, columns)
        if skipped:
            logger.info("schema v8: rebuilt %s; skipped %d legacy indexes", table, len(skipped))
        else:
            logger.info("schema v8: rebuilt %s without %s", table, column)
        return "rebuilt"
    except sqlite3.Error as exc:
        logger.warning(
            "schema v8: could not migrate %s; keeping legacy column %s: %s",
            table,
            column,
            exc,
        )
        return "kept"


def migrate_confidence_columns(connection: sqlite3.Connection) -> dict[str, str]:
    """Remove legacy ``confidence`` columns; returns ``{table: outcome}``.

    Outcomes: ``dropped``, ``rebuilt``, ``kept``, ``clean`` (older tables had no
    column), ``missing`` (table not present yet). Never raises for migration
    failures: a ``kept`` table stays usable because readers/writers no longer
    touch the column.
    """

    report: dict[str, str] = {}
    for table, column in LEGACY_COLUMNS.items():
        try:
            report[table] = _migrate_table(connection, table, column)
        except sqlite3.Error:  # pragma: no cover - defensive last resort
            logger.warning(
                "schema v8: migration for %s failed unexpectedly; keeping legacy layout",
                table,
                exc_info=True,
            )
            report[table] = "kept"
    return report


__all__ = ["LEGACY_COLUMNS", "migrate_confidence_columns", "table_columns"]
