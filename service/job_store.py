"""SQL Server bookkeeping for background runs that are queued or in progress.

A row here is the "order slip" for one run. It exists only while the run is
queued or running (plus a short-lived "failed" row so a broken run is visible
in History). When a run finishes normally its result lives in
dbo.run_history and the slip is deleted. When a run is cancelled, or the
server dies mid-run, the slip is deleted too - nothing half-finished is kept.

The table is in the shared SQL Server (same connection as history_store), so
every backend copy sees the same jobs.
"""

import threading

from service.db_service import _connection


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
                IF OBJECT_ID(N'dbo.run_jobs', N'U') IS NULL
                BEGIN
                    CREATE TABLE dbo.run_jobs (
                        id BIGINT IDENTITY(1,1) NOT NULL PRIMARY KEY,
                        source NVARCHAR(32) NOT NULL,
                        module NVARCHAR(255) NULL,
                        screen NVARCHAR(MAX) NULL,
                        status NVARCHAR(16) NOT NULL,
                        progress NVARCHAR(255) NULL,
                        last_log NVARCHAR(1000) NULL,
                        started_at DATETIME2(7) NOT NULL CONSTRAINT DF_run_jobs_started DEFAULT SYSUTCDATETIME(),
                        updated_at DATETIME2(7) NOT NULL CONSTRAINT DF_run_jobs_updated DEFAULT SYSUTCDATETIME()
                    );
                END
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
            INSERT INTO dbo.run_jobs (source, module, screen, status)
            OUTPUT INSERTED.id
            VALUES (%s, %s, %s, %s)
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
    sets = ["updated_at = SYSUTCDATETIME()"]
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
        cursor.execute(f"UPDATE dbo.run_jobs SET {', '.join(sets)} WHERE id = %s", tuple(params))
        conn.commit()


def mark_failed(job_id: int, message: str) -> None:
    update_job(job_id, status="failed", last_log=message or "run ended without results")


def delete_job(job_id: int) -> None:
    _ensure_schema()
    with _connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM dbo.run_jobs WHERE id = %s", (job_id,))
        conn.commit()


def sweep() -> None:
    """Remove slips of runs that died with their server, and old failed slips."""
    _ensure_schema()
    with _connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            DELETE FROM dbo.run_jobs
            WHERE (status IN (N'queued', N'running')
                   AND DATEDIFF(SECOND, updated_at, SYSUTCDATETIME()) > %s)
               OR (status = N'failed'
                   AND DATEDIFF(HOUR, updated_at, SYSUTCDATETIME()) > %s)
            """,
            (STALE_SECONDS, FAILED_KEEP_HOURS),
        )
        conn.commit()


def list_jobs() -> list[dict]:
    """Queued / running / failed slips, oldest first. Timestamps are UTC with a Z."""
    sweep()
    with _connection() as conn:
        cursor = conn.cursor()
        cursor.execute(f"SELECT {_COLUMNS} FROM dbo.run_jobs ORDER BY id ASC")
        rows = cursor.fetchall()
    result = []
    for row in rows:
        item = dict(row)
        for key in ("started_at", "updated_at"):
            if item.get(key) is not None:
                item[key] = item[key].isoformat() + "Z"
        result.append(item)
    return result