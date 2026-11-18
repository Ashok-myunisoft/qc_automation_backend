"""Admin tools and personal pages of the Admins page.

    GET    /api/admin/live                    admin: who is running / waiting, who was active in the last 24 hours, and one plain server status
    POST   /api/admin/live/{key}/cancel       admin: stop one run
    POST   /api/admin/cleanup                 admin: kill leftover Cypress / browser processes (only while no run is active)

    GET    /api/me/work                       my recent runs and my issues

Announcements live in announcement_service.py.
"""
import logging
import os
import signal
import subprocess
import time
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Request

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
    cy = [r for r in rows if _is_cypress(r["args"]) and r["pid"] != mine and not r["stat"].startswith("Z")]
    return {"available": True, "leftover": len(cy)}


def _live_jobs() -> list[dict]:
    now = time.time()
    jobs = []
    for job in list(job_manager._JOBS.values()):
        if not job.active:
            continue
        user = job.session.get("user") or {}
        since = getattr(job, "started_at", None) if job.status == "running" else getattr(job, "created_at", None)
        jobs.append({
            "key": job.key, "source": job.source, "module": job.module, "screen": job.screen,
            "status": job.status, "progress": job.progress,
            "minutes": max(0, int((now - since) // 60)) if since else None,
            "userCode": user.get("userCode") or "", "userName": user.get("userName") or user.get("userCode") or "unknown",
        })
    jobs.sort(key=lambda j: (j["status"] != "running", j["key"]))
    return jobs


def _active_people() -> list[dict]:
    """People who logged in or ran something in the last 24 hours (most recent first)."""
    since = _now() - timedelta(hours=24)
    people: dict[str, dict] = {}
    try:
        roles_service._ensure_schema()
        history_store._ensure_schema()
        with _connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT user_code, user_name, last_login FROM qc_users WHERE last_login >= %s ORDER BY last_login DESC LIMIT 100", (since,))
            for r in cur.fetchall():
                people[r["user_code"].lower()] = {"userCode": r["user_code"], "userName": r["user_name"] or r["user_code"],
                                                  "lastLogin": _iso(r["last_login"]), "runs": 0}
            cur.execute(
                "SELECT LOWER(run_by_code) AS code, MAX(run_by_name) AS name, MAX(run_by_code) AS raw, COUNT(*) AS n FROM run_history "
                "WHERE created_at >= %s AND run_by_code IS NOT NULL AND run_by_code <> '' GROUP BY LOWER(run_by_code)",
                (since,),
            )
            for r in cur.fetchall():
                p = people.setdefault(r["code"], {"userCode": r["raw"], "userName": r["name"] or r["raw"], "lastLogin": None, "runs": 0})
                p["runs"] = int(r["n"] or 0)
    except Exception:  # noqa: BLE001  (a missing table must not break the page)
        logger.warning("could not list today's activity", exc_info=True)
    out = list(people.values())
    out.sort(key=lambda p: p["lastLogin"] or "", reverse=True)
    return out


def _server_status(has_jobs: bool) -> dict:
    """One plain line - no process numbers. state: clean | leftover | busy | unknown"""
    report = _process_report()
    if not report["available"]:
        return {"state": "unknown", "canCleanup": False}
    if has_jobs:
        return {"state": "busy", "canCleanup": False}
    if report["leftover"] > 0:
        return {"state": "leftover", "canCleanup": True}
    return {"state": "clean", "canCleanup": False}


@router.get("/admin/live")
def live(request: Request):
    _admin(request)
    jobs = _live_jobs()
    return {"jobs": jobs, "people": _active_people(), "server": _server_status(bool(jobs))}


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
    return {"cleaned": killed > 0, "server": _server_status(False)}


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