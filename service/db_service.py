import os
import logging
from contextlib import contextmanager

import pymssql
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_MAX_SAMPLE_LIMIT = 50  # hard ceiling regardless of what a caller asks for


class DbServiceError(Exception):
    pass


def _config() -> dict:
    host     = os.getenv("TESTDB_HOST")
    database = os.getenv("TESTDB_NAME")
    user     = os.getenv("TESTDB_USER")
    password = os.getenv("TESTDB_PASSWORD")

    missing = [name for name, val in [
        ("TESTDB_HOST", host), ("TESTDB_NAME", database),
        ("TESTDB_USER", user), ("TESTDB_PASSWORD", password),
    ] if not val]
    if missing:
        raise DbServiceError(f"missing env var(s): {', '.join(missing)} — set these in .env")

    return {"host": host, "database": database, "user": user, "password": password}


@contextmanager
def _connection():
    cfg = _config()
    conn = pymssql.connect(
        server=cfg["host"], database=cfg["database"],
        user=cfg["user"], password=cfg["password"],
        as_dict=True, login_timeout=10, timeout=30,
    )
    try:
        yield conn
    finally:
        conn.close()


def _query(sql: str, params: tuple = ()) -> list[dict]:
    with _connection() as conn:
        cur = conn.cursor()
        cur.execute(sql, params)
        rows = list(cur.fetchall())
        logger.info("db_service query executed: %s | rows_returned=%d", sql, len(rows))
        return rows


def _table_exists(table_name: str) -> bool:
    rows = _query(
        "SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = %s",
        (table_name,),
    )
    return len(rows) > 0


def _column_exists(table_name: str, column_name: str) -> bool:
    rows = _query(
        "SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = %s AND COLUMN_NAME = %s",
        (table_name, column_name),
    )
    return len(rows) > 0


def list_tables(keyword: str | None = None, limit: int = 50) -> list[str]:
    """Lists real table names, optionally filtered by a substring keyword
    (case-insensitive). Use this FIRST when you don't have a confident table
    name yet — e.g. searching "instrument" for the InstrumentMaster screen.

    BUGFIX: the row-limit is now inlined via an f-string, not Python's %
    operator. The old code applied `% limit` (one value) to a string that
    also contained a literal `%s` meant for pymssql's OWN separate
    parameter-substitution mechanism — Python's % counts every placeholder
    in the string and demanded two values, raising "not enough arguments
    for format string" before any query ever reached the database. This
    is safe: `limit` is always our own clamped int, never externally
    supplied text, so there's no injection concern in inlining it directly."""
    limit = min(int(limit), 200)
    if keyword:
        rows = _query(
            f"SELECT TOP ({limit}) TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_TYPE = 'BASE TABLE' AND TABLE_NAME LIKE %s ORDER BY TABLE_NAME",
            (f"%{keyword}%",),
        )
    else:
        rows = _query(
            f"SELECT TOP ({limit}) TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_TYPE = 'BASE TABLE' ORDER BY TABLE_NAME"
        )
    return [r["TABLE_NAME"] for r in rows]


def search_tables_by_column(column_keyword: str, limit: int = 50) -> list[dict]:
    """Searches COLUMN names (not table names) for a substring — this is the
    stronger signal when the table name itself is opaque/legacy, since a
    screen's form field names (e.g. "InstrumentCode") tend to survive table
    renames better than the table name does. Returns [{table, column}, ...].

    Same %-operator bugfix as list_tables above — see that docstring."""
    limit = min(int(limit), 200)
    rows = _query(
        f"SELECT TOP ({limit}) TABLE_NAME, COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE COLUMN_NAME LIKE %s ORDER BY TABLE_NAME, COLUMN_NAME",
        (f"%{column_keyword}%",),
    )
    return [{"table": r["TABLE_NAME"], "column": r["COLUMN_NAME"]} for r in rows]


def describe_table(table_name: str) -> dict:
    """Given a table name you already have some confidence in (from
    list_tables/search_tables_by_column, or a source-code hint), returns its
    columns and foreign-key relationships (both directions) so an agent can
    understand the schema and follow FK chains without ever needing it
    explained up front. Raises DbServiceError if the table doesn't exist —
    that's a deliberate signal to fall back to search_tables_by_column
    instead of guessing further."""
    if not _table_exists(table_name):
        raise DbServiceError(f"no such table: {table_name!r} — try search_tables_by_column or list_tables instead")

    columns = _query(
        "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE "
        "FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = %s ORDER BY ORDINAL_POSITION",
        (table_name,),
    )

    # FKs where this table is the child (references another table)
    outgoing = _query(
        """
        SELECT
            fk.name AS constraint_name,
            c1.name AS column_name,
            OBJECT_NAME(fk.referenced_object_id) AS referenced_table,
            c2.name AS referenced_column
        FROM sys.foreign_keys fk
        JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id
        JOIN sys.columns c1 ON c1.object_id = fkc.parent_object_id AND c1.column_id = fkc.parent_column_id
        JOIN sys.columns c2 ON c2.object_id = fkc.referenced_object_id AND c2.column_id = fkc.referenced_column_id
        WHERE OBJECT_NAME(fk.parent_object_id) = %s
        """,
        (table_name,),
    )

    # FKs where this table is the parent (other tables reference it)
    incoming = _query(
        """
        SELECT
            fk.name AS constraint_name,
            OBJECT_NAME(fk.parent_object_id) AS referencing_table,
            c1.name AS referencing_column,
            c2.name AS column_name
        FROM sys.foreign_keys fk
        JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id
        JOIN sys.columns c1 ON c1.object_id = fkc.parent_object_id AND c1.column_id = fkc.parent_column_id
        JOIN sys.columns c2 ON c2.object_id = fkc.referenced_object_id AND c2.column_id = fkc.referenced_column_id
        WHERE OBJECT_NAME(fk.referenced_object_id) = %s
        """,
        (table_name,),
    )

    return {
        "table": table_name,
        "columns": [
            {"name": c["COLUMN_NAME"], "type": c["DATA_TYPE"], "nullable": c["IS_NULLABLE"] == "YES"}
            for c in columns
        ],
        "references": [  # this table -> other tables (follow these for parent/lookup data)
            {"column": r["column_name"], "references_table": r["referenced_table"], "references_column": r["referenced_column"]}
            for r in outgoing
        ],
        "referenced_by": [  # other tables -> this table (this table is shared/lookup data for these)
            {"table": r["referencing_table"], "column": r["referencing_column"], "via_column": r["column_name"]}
            for r in incoming
        ],
    }


def get_sample_values(table_name: str, column_name: str, limit: int = 10) -> list:
    """Returns real, currently-valid distinct values for a specific column —
    e.g. actual GL account codes that exist right now, not invented ones.
    Only ever called AFTER describe_table has confirmed the table/column are
    real (both are re-validated here too, defensively, before any query
    runs)."""
    limit = min(limit, _MAX_SAMPLE_LIMIT)

    if not _table_exists(table_name):
        raise DbServiceError(f"no such table: {table_name!r}")
    if not _column_exists(table_name, column_name):
        raise DbServiceError(f"no such column: {column_name!r} on table {table_name!r}")

    # table_name/column_name are validated above (real identifiers, no
    # arbitrary text can reach this point) — safe to reference directly in
    # the identifier position, which SQL doesn't allow parameterizing anyway.
    sql = (
        f"SELECT DISTINCT TOP ({limit}) [{column_name}] AS value"
        f" FROM [{table_name}] WHERE [{column_name}] IS NOT NULL"
    )
    rows = _query(sql)
    return [r["value"] for r in rows]
