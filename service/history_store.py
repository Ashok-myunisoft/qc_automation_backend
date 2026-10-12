"""PostgreSQL storage for completed QC runs and their downloadable artifacts."""

import json
import threading

import psycopg2
from datetime import datetime, timezone

from service.store_db import connection as _connection


_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY = False

_LIST_COLUMNS = """
    id, created_at, source, module, screen, status, total, passed_count,
    failed_count, screens_json, report_filename, screenshots_filename,
    (report_xlsx IS NOT NULL) AS has_report,
    (screenshots_html IS NOT NULL) AS has_screenshots
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
                CREATE TABLE IF NOT EXISTS run_history (
                    id BIGSERIAL PRIMARY KEY,
                    created_at TIMESTAMP NOT NULL,
                    source VARCHAR(32) NOT NULL,
                    module VARCHAR(255) NULL,
                    screen TEXT NULL,
                    status VARCHAR(16) NOT NULL,
                    total INT NOT NULL DEFAULT 0,
                    passed_count INT NOT NULL DEFAULT 0,
                    failed_count INT NOT NULL DEFAULT 0,
                    screens_json TEXT NULL,
                    report_filename VARCHAR(255) NULL,
                    report_xlsx BYTEA NULL,
                    screenshots_filename VARCHAR(255) NULL,
                    screenshots_html TEXT NULL
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS ix_run_history_created_at
                ON run_history (created_at DESC, id DESC)
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
            INSERT INTO run_history (
                created_at, source, module, screen, status, total, passed_count, failed_count,
                screens_json, report_filename, report_xlsx, screenshots_filename, screenshots_html
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (
            datetime.now(timezone.utc).replace(tzinfo=None), source, module, screen, status,
            total, passed, failed, json.dumps(screens), report_filename,
            psycopg2.Binary(report_xlsx) if report_xlsx is not None else None,
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
            f"SELECT {_LIST_COLUMNS} FROM run_history ORDER BY id DESC LIMIT %s",
            (limit,),
        )
        rows = cursor.fetchall()
    return [_row_to_dict(row) for row in rows]


def get_run(run_id: int) -> dict | None:
    _ensure_schema()
    with _connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT {_LIST_COLUMNS} FROM run_history WHERE id = %s", (run_id,)
        )
        row = cursor.fetchone()
    return _row_to_dict(row) if row else None


def get_report(run_id: int) -> tuple[str, bytes] | None:
    _ensure_schema()
    with _connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT report_filename, report_xlsx FROM run_history WHERE id = %s", (run_id,)
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
            "SELECT screenshots_filename, screenshots_html FROM run_history WHERE id = %s", (run_id,)
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
        cursor.execute("DELETE FROM run_history WHERE id = %s", (run_id,))
        deleted = cursor.rowcount > 0
        conn.commit()
    return deleted