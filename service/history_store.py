"""SQL Server storage for completed QC runs and their downloadable artifacts."""

import json
import threading
from datetime import datetime, timezone

from service.db_service import _connection


_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY = False

_LIST_COLUMNS = """
    id, created_at, source, module, screen, status, total, passed_count,
    failed_count, screens_json, report_filename, screenshots_filename,
    CAST(CASE WHEN report_xlsx IS NULL THEN 0 ELSE 1 END AS bit) AS has_report,
    CAST(CASE WHEN screenshots_html IS NULL THEN 0 ELSE 1 END AS bit) AS has_screenshots
"""


def _ensure_schema() -> None:
    """Create the shared history table once when the application first uses it."""
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return

    with _SCHEMA_LOCK:
        if _SCHEMA_READY:
            return
        with _connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                IF OBJECT_ID(N'dbo.run_history', N'U') IS NULL
                BEGIN
                    CREATE TABLE dbo.run_history (
                        id BIGINT IDENTITY(1,1) NOT NULL PRIMARY KEY,
                        created_at DATETIME2(7) NOT NULL,
                        source NVARCHAR(32) NOT NULL,
                        module NVARCHAR(255) NULL,
                        screen NVARCHAR(MAX) NULL,
                        status NVARCHAR(16) NOT NULL,
                        total INT NOT NULL CONSTRAINT DF_run_history_total DEFAULT 0,
                        passed_count INT NOT NULL CONSTRAINT DF_run_history_passed DEFAULT 0,
                        failed_count INT NOT NULL CONSTRAINT DF_run_history_failed DEFAULT 0,
                        screens_json NVARCHAR(MAX) NULL,
                        report_filename NVARCHAR(255) NULL,
                        report_xlsx VARBINARY(MAX) NULL,
                        screenshots_filename NVARCHAR(255) NULL,
                        screenshots_html NVARCHAR(MAX) NULL
                    );
                END
            """)
            cursor.execute("""
                IF NOT EXISTS (
                    SELECT 1 FROM sys.indexes
                    WHERE name = N'IX_run_history_created_at'
                      AND object_id = OBJECT_ID(N'dbo.run_history')
                )
                CREATE INDEX IX_run_history_created_at
                ON dbo.run_history (created_at DESC, id DESC);
            """)
            conn.commit()
        _SCHEMA_READY = True


def _row_to_dict(row: dict) -> dict:
    result = dict(row)
    # created_at is stored as naive UTC. Mark it as UTC so the browser converts
    # it to the viewer's local time instead of showing the raw UTC clock time.
    created = result.get("created_at")
    if isinstance(created, datetime) and created.tzinfo is None:
        result["created_at"] = created.replace(tzinfo=timezone.utc)
    result["has_report"] = bool(result.get("has_report"))
    result["has_screenshots"] = bool(result.get("has_screenshots"))
    try:
        result["screens"] = json.loads(result.pop("screens_json") or "[]")
    except (TypeError, ValueError):
        result["screens"] = []
    return result


def save_run(*, source: str, module: str, screen: str, run_results: list[dict],
             report_filename: str, report_xlsx: bytes | None,
             screenshots_filename: str, screenshots_html: str | None) -> int:
    _ensure_schema()
    total = len(run_results)
    passed = sum(1 for result in run_results if result.get("passed"))
    failed = total - passed
    status = "passed" if total and failed == 0 else ("failed" if passed == 0 else "partial")
    screens = [
        {"slug": result.get("slug"), "passed": bool(result.get("passed")), "stats": result.get("stats")}
        for result in run_results
    ]

    with _connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO dbo.run_history (
                created_at, source, module, screen, status, total, passed_count, failed_count,
                screens_json, report_filename, report_xlsx, screenshots_filename, screenshots_html
            )
            OUTPUT INSERTED.id
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            datetime.now(timezone.utc).replace(tzinfo=None), source, module, screen, status,
            total, passed, failed, json.dumps(screens), report_filename, report_xlsx,
            screenshots_filename, screenshots_html,
        ))
        row = cursor.fetchone()
        conn.commit()
    return int(row["id"])


def list_runs(limit: int = 200) -> list[dict]:
    _ensure_schema()
    with _connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT TOP (%s) {_LIST_COLUMNS} FROM dbo.run_history ORDER BY id DESC",
            (limit,),
        )
        rows = cursor.fetchall()
    return [_row_to_dict(row) for row in rows]


def get_run(run_id: int) -> dict | None:
    _ensure_schema()
    with _connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT {_LIST_COLUMNS} FROM dbo.run_history WHERE id = %s", (run_id,)
        )
        row = cursor.fetchone()
    return _row_to_dict(row) if row else None


def get_report(run_id: int) -> tuple[str, bytes] | None:
    _ensure_schema()
    with _connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT report_filename, report_xlsx FROM dbo.run_history WHERE id = %s", (run_id,)
        )
        row = cursor.fetchone()
    if not row or row["report_xlsx"] is None:
        return None
    return row["report_filename"] or f"qc-report-{run_id}.xlsx", bytes(row["report_xlsx"])


def get_screenshots(run_id: int) -> tuple[str, str] | None:
    _ensure_schema()
    with _connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT screenshots_filename, screenshots_html FROM dbo.run_history WHERE id = %s", (run_id,)
        )
        row = cursor.fetchone()
    if not row or row["screenshots_html"] is None:
        return None
    return row["screenshots_filename"] or f"qc-screenshots-{run_id}.html", row["screenshots_html"]


def delete_run(run_id: int) -> bool:
    """Delete one history row (report and screenshots included). True if a row was removed."""
    _ensure_schema()
    with _connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM dbo.run_history WHERE id = %s", (run_id,))
        deleted = cursor.rowcount > 0
        conn.commit()
    return deleted