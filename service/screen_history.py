"""The overall history of ONE screen: who generated it, who replaced it, who ran it, and when.

Two sources are merged into one timeline (newest first):
* screen_events (this file's table)  - generate / replace / append / push to the repo
* run_history   (History page table) - every run, with the person and the environment

So a screen that one person generated on Monday and another person ran on Tuesday shows both,
each with the right name.

Route (login required):
    GET /api/screen-history?module=ADMIN&screen=AdvanceUserRights&limit=100
"""
import json
import logging
import threading
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

from service import history_store
from service.discussion_service import _iso, _me, _now
from service.store_db import connection as _connection

logger = logging.getLogger(__name__)

_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY = False
UNKNOWN = "Unknown (before user tracking)"
SOURCE_LABEL = {"fetch": "Fetch → Run", "generate": "Generate → Run", "auto_run": "Auto-run", "overnight_run": "Overnight run"}


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
                CREATE TABLE IF NOT EXISTS screen_events (
                    id BIGSERIAL PRIMARY KEY,
                    module VARCHAR(255) NOT NULL,
                    screen VARCHAR(255) NOT NULL,
                    module_key VARCHAR(255) NOT NULL,
                    screen_key VARCHAR(255) NOT NULL,
                    event_type VARCHAR(24) NOT NULL,
                    user_code VARCHAR(64) NULL,
                    user_name VARCHAR(255) NULL,
                    detail TEXT NULL,
                    created_at TIMESTAMP NOT NULL
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS ix_screen_events_key ON screen_events (module_key, screen_key, id)")
            conn.commit()
        _SCHEMA_READY = True


def log_event(module: str, screen: str, event_type: str, user: dict | None, detail: str = "") -> None:
    """Write one event. Never raises: the history must not break the real action."""
    try:
        if not module or not screen:
            return
        _ensure_schema()
        user = user or {}
        with _connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO screen_events (module, screen, module_key, screen_key, event_type, user_code, user_name, detail, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (module, screen, history_store.track_key(module), history_store.track_key(screen), event_type,
                 user.get("userCode"), user.get("userName") or user.get("userCode"), (detail or "")[:1000], _now()),
            )
            conn.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("could not log screen event: %s", e)


router = APIRouter(prefix="/api/screen-history")


@router.get("")
def screen_history(request: Request, module: str, screen: str, limit: int = 100):
    _me(request)
    module, screen = (module or "").strip(), (screen or "").strip()
    if not module or not screen:
        raise HTTPException(status_code=400, detail="Module and screen are required.")
    limit = max(1, min(int(limit), 300))
    mk, sk = history_store.track_key(module), history_store.track_key(screen)
    _ensure_schema()
    history_store._ensure_schema()

    events: list[dict] = []
    with _connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM screen_events WHERE module_key = %s AND screen_key = %s ORDER BY id DESC LIMIT %s", (mk, sk, limit))
        for r in cur.fetchall():
            events.append({
                "type": r["event_type"], "at": _iso(r["created_at"]),
                "user_code": r["user_code"], "user_name": r["user_name"] or UNKNOWN,
                "title": r["detail"] or "Tests pushed to the repo", "detail": "", "status": None, "run_id": None,
            })
        cur.execute(
            "SELECT id, created_at, source, module, screen, status, total, passed_count, screens_json, "
            "run_by_code, run_by_name, env_base_url, env_db FROM run_history ORDER BY id DESC LIMIT 1500"
        )
        runs = cur.fetchall()

    for r in runs:
        if history_store.track_key(r["module"]) != mk and mk not in {history_store.track_key(m) for m in str(r["module"] or "").split(",")}:
            continue
        if r["source"] == "overnight_run" and str(r["screen"] or "").startswith("All screens"):
            continue
        try:
            items = json.loads(r["screens_json"] or "[]")
        except (TypeError, ValueError):
            items = []
        mine = [i for i in items if history_store.track_key(i.get("slug")) == sk]
        if not mine and not (r["screen"] and "," not in str(r["screen"]) and history_store.track_key(r["screen"]) == sk):
            continue
        stats = (mine[0].get("stats") if mine else None) or {}
        if not mine:                       # old row without per-screen data: use the run's own numbers
            try:
                stats = (items[0].get("stats") or {}) if len(items) == 1 else {}
            except (IndexError, AttributeError):
                stats = {}
        ran_ok = (stats.get("tests") or 0) > 0   # completed = test cases were executed, whatever their result
        created = r["created_at"]
        if isinstance(created, datetime) and created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        where = " · ".join(x for x in (r["env_base_url"], f"DB {r['env_db']}" if r["env_db"] else None) if x)
        events.append({
            "type": "run", "at": created.isoformat() if isinstance(created, datetime) else None,
            "user_code": r["run_by_code"], "user_name": r["run_by_name"] or UNKNOWN,
            "title": "Ran the tests · " + SOURCE_LABEL.get(r["source"], r["source"] or "run"),
            "detail": where or "Environment not recorded", "status": "completed" if ran_ok else "error", "run_id": r["id"],
            "passed_count": stats.get("passes") or 0, "failed_count": stats.get("failures") or 0,
        })

    events.sort(key=lambda e: e["at"] or "", reverse=True)
    return {"events": events[:limit]}