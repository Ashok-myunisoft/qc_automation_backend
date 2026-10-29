import json
import re
import threading

import psycopg2
from datetime import datetime, timedelta, timezone

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


def track_key(text) -> str:
    """Loose name used to match History names with repo names: 'Skill_Management_Module' == 'skill management'.

    The frontend (tracking.js) uses the same rule, so keep the two in step.
    """
    name = re.sub(r"[_\-\s]+", " ", str(text or "")).strip().lower()
    return re.sub(r"\s+module$", "", name).strip()


def get_last_runs() -> dict:
    """When was each module / screen last run, and how often lately? Read straight from History (no blobs).

    Returns {
      "modules": {module_key: iso}, "screens": {"module_key|screen_key": iso},       # last run, UTC ISO
      "runs30":  {"modules": {module_key: n}, "screens": {"module_key|screen_key": n}},  # runs in the last 30 days
      "weekly":  [{"week": "2026-10-26", "screens": 12, "runs": 15}, ...]             # last 8 weeks, oldest first
    }
    The whole-run row an overnight run adds ("All screens (N)") is skipped - every screen of that run
    already has its own row, and the combined row has a comma-joined module name.
    Weeks start on Monday (UTC). "screens" = different screens run that week, "runs" = runs saved that week.
    """
    _ensure_schema()
    with _connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT created_at, source, module, screen, screens_json FROM run_history")
        rows = cursor.fetchall()

    now = datetime.now(timezone.utc)
    cutoff30 = now - timedelta(days=30)
    this_monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    weeks = [this_monday - timedelta(weeks=i) for i in range(7, -1, -1)]          # oldest -> newest
    week_screens: dict[datetime, set] = {w: set() for w in weeks}
    week_runs: dict[datetime, int] = {w: 0 for w in weeks}

    modules: dict[str, datetime] = {}
    screens: dict[str, datetime] = {}
    module_runs30: dict[str, int] = {}
    screen_runs30: dict[str, int] = {}
    for row in rows:
        created = row.get("created_at")
        if not isinstance(created, datetime):
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if row.get("source") == "overnight_run" and str(row.get("screen") or "").startswith("All screens"):
            continue
        module_key = track_key(row.get("module"))
        if not module_key:
            continue
        if module_key not in modules or created > modules[module_key]:
            modules[module_key] = created

        try:
            slugs = [str(item.get("slug")) for item in json.loads(row.get("screens_json") or "[]") if item.get("slug")]
        except (TypeError, ValueError, AttributeError):
            slugs = []
        if not slugs and row.get("screen") and "," not in str(row["screen"]):
            slugs = [str(row["screen"])]

        recent = created >= cutoff30
        if recent:
            module_runs30[module_key] = module_runs30.get(module_key, 0) + 1
        week = next((w for w in reversed(weeks) if created >= w), None)
        if week is not None:
            week_runs[week] += 1

        for slug in dict.fromkeys(slugs):                  # a screen counts once per run
            key = f"{module_key}|{track_key(slug)}"
            if key not in screens or created > screens[key]:
                screens[key] = created
            if recent:
                screen_runs30[key] = screen_runs30.get(key, 0) + 1
            if week is not None:
                week_screens[week].add(key)

    return {
        "modules": {k: v.isoformat(timespec="seconds") for k, v in modules.items()},
        "screens": {k: v.isoformat(timespec="seconds") for k, v in screens.items()},
        "runs30": {"modules": module_runs30, "screens": screen_runs30},
        "weekly": [
            {"week": w.date().isoformat(), "screens": len(week_screens[w]), "runs": week_runs[w]}
            for w in weeks
        ],
    }


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