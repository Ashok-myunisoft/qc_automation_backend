"""PostgreSQL bookkeeping for background runs that are queued or in progress.

A row here is the "order slip" for one run. It exists only while the run is
queued or running (plus a short-lived "failed" row so a broken run is visible
in History). When a run finishes normally its result lives in
run_history and the slip is deleted. When a run is cancelled, or the
server dies mid-run, the slip is deleted too - nothing half-finished is kept.

The table is in the shared PostgreSQL database (same connection as history_store), so
every backend copy sees the same jobs.
"""

import threading

from service.store_db import connection as _connection


_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY = False

# A queued/running job whose backend stopped updating it for this long is
# treated as dead (server restarted / crashed) and silently removed.
STALE_SECONDS = 150
# A "failed" slip stays visible for a day, then is cleaned up.
FAILED_KEEP_HOURS = 24

_COLUMNS = "id, source, module, screen, status, progress, last_log, started_at, updated_at"


def _ensure_schema() -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    with _SCHEMA_LOCK:
        if _SCHEMA_READY:
            return
        with _connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS run_jobs (
                    id BIGSERIAL PRIMARY KEY,
                    source VARCHAR(32) NOT NULL,
                    module VARCHAR(255) NULL,
                    screen TEXT NULL,
                    status VARCHAR(16) NOT NULL,
                    progress VARCHAR(255) NULL,
                    last_log VARCHAR(1000) NULL,
                    started_at TIMESTAMP NOT NULL DEFAULT (NOW() AT TIME ZONE 'utc'),
                    updated_at TIMESTAMP NOT NULL DEFAULT (NOW() AT TIME ZONE 'utc')
                )
            """)
            conn.commit()
        _SCHEMA_READY = True


def _clip(value, limit: int):
    if value is None:
        return None
    value = str(value)
    return value if len(value) <= limit else value[: limit - 1] + "…"


def create_job(source: str, module: str, screen: str, status: str = "queued") -> int:
    _ensure_schema()
    with _connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO run_jobs (source, module, screen, status)
            VALUES (%s, %s, %s, %s)
            RETURNING id
            """,
            (source, _clip(module, 255), screen or "", status),
        )
        row = cursor.fetchone()
        conn.commit()
    return int(row["id"])


def update_job(job_id: int, *, status: str | None = None,
               progress: str | None = None, last_log: str | None = None) -> None:
    """Update whichever fields are given and always refresh the heartbeat."""
    _ensure_schema()
    sets = ["updated_at = (NOW() AT TIME ZONE 'utc')"]
    params: list = []
    if status is not None:
        sets.append("status = %s")
        params.append(status)
    if progress is not None:
        sets.append("progress = %s")
        params.append(_clip(progress, 255))
    if last_log is not None:
        sets.append("last_log = %s")
        params.append(_clip(last_log, 1000))
    params.append(job_id)
    with _connection() as conn:
        cursor = conn.cursor()
        cursor.execute(f"UPDATE run_jobs SET {', '.join(sets)} WHERE id = %s", tuple(params))
        conn.commit()


def mark_failed(job_id: int, message: str) -> None:
    update_job(job_id, status="failed", last_log=message or "run ended without results")


def delete_job(job_id: int) -> None:
    _ensure_schema()
    with _connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM run_jobs WHERE id = %s", (job_id,))
        conn.commit()


def sweep() -> None:
    """Remove slips of runs that died with their server, and old failed slips."""
    _ensure_schema()
    with _connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            DELETE FROM run_jobs
            WHERE (status IN ('queued', 'running')
                   AND EXTRACT(EPOCH FROM ((NOW() AT TIME ZONE 'utc') - updated_at)) > %s)
               OR (status = 'failed'
                   AND EXTRACT(EPOCH FROM ((NOW() AT TIME ZONE 'utc') - updated_at)) > %s)
            """,
            (STALE_SECONDS, FAILED_KEEP_HOURS * 3600),
        )
        conn.commit()


def list_jobs() -> list[dict]:
    """Queued / running / failed slips, oldest first. Timestamps are UTC with a Z."""
    sweep()
    with _connection() as conn:
        cursor = conn.cursor()
        cursor.execute(f"SELECT {_COLUMNS} FROM run_jobs ORDER BY id ASC")
        rows = cursor.fetchall()
    result = []
    for row in rows:
        item = dict(row)
        for key in ("started_at", "updated_at"):
            if item.get(key) is not None:
                item[key] = item[key].isoformat() + "Z"
        result.append(item)
    return result