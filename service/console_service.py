"""Admin tools and personal pages of the Admins page.

    GET    /api/admin/live                    admin: runs that are running or waiting + the Cypress process count
    POST   /api/admin/live/{key}/cancel       admin: stop one run
    POST   /api/admin/cleanup                 admin: kill leftover Cypress / browser processes (only while no run is active)

    GET    /api/announcements                 everybody: messages that are currently active (shown as a banner)
    GET    /api/admin/announcements           admin: recent messages, also expired ones
    POST   /api/admin/announcements           admin: {"message": "...", "level": "info|warning", "hours": 24 or null}
    DELETE /api/admin/announcements/{id}      admin: remove one

    GET    /api/me/work                       my recent runs and my issues
"""
import logging
import os
import signal
import subprocess
import threading
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from service import history_store, job_manager, roles_service
from service.discussion_service import _iso, _me, _now
from service.store_db import connection as _connection

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


def _admin(request: Request) -> dict:
    me = _me(request)
    if not roles_service.user_is_admin(me):
        raise HTTPException(status_code=403, detail="Only an admin can do this.")
    return me


# ------------------------------------------------------------------ live runs + processes

def _scan_proc() -> list[dict] | None:
    """Linux (also inside Docker, where `ps` may not be installed): read /proc directly."""
    if not os.path.isdir("/proc/self"):
        return None
    rows = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", "rb") as f:
                stat = f.read().decode("utf-8", "replace")
            after = stat[stat.rindex(")") + 2:].split()        # state, ppid, ...
            with open(f"/proc/{name}/cmdline", "rb") as f:
                args = f.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
            rows.append({"pid": int(name), "ppid": int(after[1]), "stat": after[0], "args": args})
        except Exception:  # noqa: BLE001  (the process ended while we looked)
            continue
    return rows


def _scan_processes() -> list[dict] | None:
    """Every process on this machine (container): pid, ppid, state, command. None when it cannot be listed."""
    rows = _scan_proc()
    if rows is not None:
        return rows
    try:
        out = subprocess.run(["ps", "-eo", "pid=,ppid=,stat=,args="], capture_output=True, text=True, timeout=10).stdout
    except Exception:  # noqa: BLE001
        return None
    rows = []
    for line in out.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) < 3:
            continue
        try:
            rows.append({"pid": int(parts[0]), "ppid": int(parts[1]), "stat": parts[2], "args": parts[3] if len(parts) > 3 else ""})
        except ValueError:
            continue
    return rows


def _is_cypress(args: str) -> bool:
    a = args or ""
    return "/Cypress/" in a or "cypress-workspace" in a or "cypress run" in a.lower()


def _process_report() -> dict:
    rows = _scan_processes()
    if rows is None:
        return {"available": False}
    mine = os.getpid()
    cy = [r for r in rows if _is_cypress(r["args"]) and r["pid"] != mine]
    zombies = [r for r in rows if r["stat"].startswith("Z")]
    return {"available": True, "total": len(rows), "cypress": len([r for r in cy if not r["stat"].startswith("Z")]), "zombies": len(zombies)}


def _live_jobs() -> list[dict]:
    jobs = []
    for job in list(job_manager._JOBS.values()):
        if not job.active:
            continue
        user = job.session.get("user") or {}
        jobs.append({
            "key": job.key, "source": job.source, "module": job.module, "screen": job.screen,
            "status": job.status, "progress": job.progress, "lastLog": (job.last_log or "")[:160],
            "userCode": user.get("userCode") or "", "userName": user.get("userName") or user.get("userCode") or "unknown",
        })
    jobs.sort(key=lambda j: (j["status"] != "running", j["key"]))
    return jobs


@router.get("/admin/live")
def live(request: Request):
    _admin(request)
    return {"jobs": _live_jobs(), "processes": _process_report(), "maxConcurrent": job_manager.MAX_CONCURRENT_RUNS}


@router.post("/admin/live/{key}/cancel")
def cancel_run(request: Request, key: int):
    me = _admin(request)
    for job in list(job_manager._JOBS.values()):
        if job.key == key and job.active:
            logger.info("run %s cancelled by admin %s", key, me.get("userCode"))
            job_manager._kill_process(job)
            job.task.cancel()
            return {"ok": True}
    raise HTTPException(status_code=404, detail="That run has already finished.")


@router.post("/admin/cleanup")
def cleanup(request: Request):
    me = _admin(request)
    if job_manager.any_active():
        raise HTTPException(status_code=409, detail="A run is active. Cancel it or wait until it finishes, then clean up.")
    rows = _scan_processes()
    if rows is None:
        raise HTTPException(status_code=501, detail="This server cannot list its processes.")
    mine, killed = os.getpid(), 0
    for r in rows:
        if r["pid"] == mine or r["stat"].startswith("Z") or not _is_cypress(r["args"]):
            continue
        try:
            os.kill(r["pid"], signal.SIGKILL)
            killed += 1
        except (ProcessLookupError, PermissionError):
            continue
        except Exception:  # noqa: BLE001
            logger.warning("could not stop process %s", r["pid"], exc_info=True)
    logger.info("cleanup by %s: %s leftover process(es) stopped", me.get("userCode"), killed)
    return {"killed": killed, "processes": _process_report()}


# ------------------------------------------------------------------ announcements

_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY = False
MAX_ANNOUNCEMENT = 300


def _ensure_schema() -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    with _SCHEMA_LOCK:
        if _SCHEMA_READY:
            return
        with _connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS qc_announcements (
                    id BIGSERIAL PRIMARY KEY,
                    message VARCHAR(400) NOT NULL,
                    level VARCHAR(12) NOT NULL DEFAULT 'info',
                    created_by_code VARCHAR(64) NULL,
                    created_by_name VARCHAR(255) NULL,
                    created_at TIMESTAMP NOT NULL,
                    expires_at TIMESTAMP NULL
                )
            """)
            conn.commit()
        _SCHEMA_READY = True


def _ann_json(r: dict) -> dict:
    return {"id": r["id"], "message": r["message"], "level": r["level"], "by": r["created_by_name"] or r["created_by_code"] or "",
            "createdAt": _iso(r["created_at"]), "expiresAt": _iso(r["expires_at"]),
            "active": r["expires_at"] is None or r["expires_at"] > _now()}


@router.get("/announcements")
def active_announcements(request: Request):
    _me(request)
    _ensure_schema()
    with _connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM qc_announcements WHERE expires_at IS NULL OR expires_at > %s ORDER BY id DESC LIMIT 5", (_now(),))
        return {"announcements": [_ann_json(r) for r in cur.fetchall()]}


@router.get("/admin/announcements")
def all_announcements(request: Request):
    _admin(request)
    _ensure_schema()
    with _connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM qc_announcements ORDER BY id DESC LIMIT 30")
        return {"announcements": [_ann_json(r) for r in cur.fetchall()]}


class NewAnnouncement(BaseModel):
    message: str
    level: str = "info"
    hours: int | None = None       # None = until an admin removes it


@router.post("/admin/announcements")
def add_announcement(request: Request, payload: NewAnnouncement):
    me = _admin(request)
    text = " ".join((payload.message or "").split())
    if not text:
        raise HTTPException(status_code=400, detail="Write the message first.")
    if len(text) > MAX_ANNOUNCEMENT:
        raise HTTPException(status_code=400, detail=f"Keep it under {MAX_ANNOUNCEMENT} characters.")
    level = payload.level if payload.level in ("info", "warning") else "info"
    now = _now()
    expires = now + timedelta(hours=max(1, min(int(payload.hours), 24 * 90))) if payload.hours else None
    _ensure_schema()
    with _connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO qc_announcements (message, level, created_by_code, created_by_name, created_at, expires_at) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING *",
            (text, level, me.get("userCode"), me.get("userName") or me.get("userCode"), now, expires),
        )
        row = cur.fetchone()
        conn.commit()
    return _ann_json(row)


@router.delete("/admin/announcements/{ann_id}")
def remove_announcement(request: Request, ann_id: int):
    _admin(request)
    _ensure_schema()
    with _connection() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM qc_announcements WHERE id = %s", (ann_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="That announcement is already gone.")
        conn.commit()
    return {"ok": True}


# ------------------------------------------------------------------ my work

@router.get("/me/work")
def my_work(request: Request):
    me = _me(request)
    key = str(me["userCode"]).strip().lower()
    history_store._ensure_schema()
    with _connection() as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT {history_store._LIST_COLUMNS} FROM run_history WHERE LOWER(run_by_code) = %s ORDER BY id DESC LIMIT 10", (key,))
        runs = [history_store._row_to_dict(r) for r in cur.fetchall()]
        issues = []
        try:
            cur.execute(
                "SELECT id, title, status, severity, module, screen, updated_at, raised_by_code, handled_by_code FROM qc_issues "
                "WHERE LOWER(raised_by_code) = %s OR LOWER(COALESCE(handled_by_code, '')) = %s ORDER BY updated_at DESC LIMIT 15",
                (key, key),
            )
            for r in cur.fetchall():
                issues.append({"id": r["id"], "title": r["title"], "status": r["status"], "severity": r["severity"],
                               "module": r["module"], "screen": r["screen"], "updatedAt": _iso(r["updated_at"]),
                               "role": "raised" if str(r["raised_by_code"]).lower() == key else "assigned"})
        except Exception:  # noqa: BLE001  (no issues table yet = nothing raised yet)
            conn.rollback()
    return {
        "runs": [{"id": r["id"], "at": r["created_at"].isoformat() if isinstance(r["created_at"], datetime) else None,
                  "source": r["source"], "module": r["module"], "screen": r["screen"], "outcome": r["outcome"],
                  "passed": r["scen_passed"], "failed": r["scen_failed"], "screens": r["total"]} for r in runs],
        "issues": issues,
    }