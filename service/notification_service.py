"""Personal notifications (the bell icon) for the QC console.

Who gets notified
-----------------
* A new issue is raised        -> the QC team (QC_TEAM and QC_ADMINS in .env)
* The status of an issue moves -> the person who raised it, the person handling it, and everybody who commented
* A comment is added           -> the same people (plus the QC team when a non-QC person writes)
Nobody is ever notified about their OWN action.

Table (created automatically in the same PostgreSQL database as History):
    qc_notifications   one row per person per event

Routes (all need login):
    GET  /api/notifications?limit=30   newest first + number of unread
    GET  /api/notifications/count      only the unread number (cheap - the bell polls this)
    POST /api/notifications/{id}/read  mark one as read
    POST /api/notifications/read-all   mark all of mine as read
"""
import logging
import os
import threading

from fastapi import APIRouter, HTTPException, Request

from service.discussion_service import _iso, _me, _now
from service.store_db import connection as _connection

logger = logging.getLogger(__name__)

KEEP_DAYS = 60          # read notifications older than this are cleaned up
DEFAULT_LIMIT = 30
MAX_LIMIT = 100

_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY = False


def ensure_schema() -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    with _SCHEMA_LOCK:
        if _SCHEMA_READY:
            return
        with _connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS qc_notifications (
                    id BIGSERIAL PRIMARY KEY,
                    user_code VARCHAR(64) NOT NULL,
                    kind VARCHAR(24) NOT NULL,
                    issue_id BIGINT NULL,
                    message TEXT NOT NULL,
                    actor_code VARCHAR(64) NULL,
                    actor_name VARCHAR(255) NULL,
                    created_at TIMESTAMP NOT NULL,
                    read_at TIMESTAMP NULL
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS ix_qc_notif_user ON qc_notifications (user_code, id)")
            conn.commit()
        _SCHEMA_READY = True


def team_codes() -> set[str]:
    """QC team + admins from .env, lower-case."""
    codes: set[str] = set()
    for var in ("QC_TEAM", "QC_ADMINS"):
        codes |= {c.strip().lower() for c in (os.getenv(var) or "").split(",") if c.strip()}
    return codes


def notify(cur, recipients, kind: str, issue_id: int | None, message: str, actor: dict) -> None:
    """Add one notification per recipient, inside the caller's open transaction.
    The actor is never notified. A failure here must never break the real action, so it runs in a savepoint."""
    actor_code = str(actor.get("userCode") or "").strip()
    targets = {str(c).strip().lower() for c in (recipients or []) if c and str(c).strip()}
    targets.discard(actor_code.lower())
    targets.discard("anonymous")
    if not targets:
        return
    try:
        ensure_schema()
        cur.execute("SAVEPOINT qc_notify")
        now = _now()
        actor_name = actor.get("userName") or actor_code
        for code in sorted(targets):
            cur.execute(
                "INSERT INTO qc_notifications (user_code, kind, issue_id, message, actor_code, actor_name, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (code, kind, issue_id, message[:500], actor_code, actor_name, now),
            )
        cur.execute("RELEASE SAVEPOINT qc_notify")
    except Exception as e:  # noqa: BLE001
        logger.warning("could not create notifications: %s", e)
        try:
            cur.execute("ROLLBACK TO SAVEPOINT qc_notify")
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- routes

router = APIRouter(prefix="/api/notifications")


def _json(row: dict) -> dict:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "issue_id": row["issue_id"],
        "message": row["message"],
        "actor_name": row["actor_name"],
        "created_at": _iso(row["created_at"]),
        "read": row["read_at"] is not None,
    }


@router.get("/count")
def unread_count(request: Request):
    user = _me(request)
    ensure_schema()
    with _connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) AS n FROM qc_notifications WHERE user_code = %s AND read_at IS NULL",
            (user["userCode"].lower(),),
        )
        return {"unread": cur.fetchone()["n"]}


@router.get("")
def list_notifications(request: Request, limit: int = DEFAULT_LIMIT):
    user = _me(request)
    limit = max(1, min(int(limit), MAX_LIMIT))
    me = user["userCode"].lower()
    ensure_schema()
    with _connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM qc_notifications WHERE user_code = %s ORDER BY id DESC LIMIT %s",
            (me, limit),
        )
        rows = cur.fetchall()
        cur.execute("SELECT COUNT(*) AS n FROM qc_notifications WHERE user_code = %s AND read_at IS NULL", (me,))
        unread = cur.fetchone()["n"]
        # tidy up old, already-read notifications (cheap, keeps the table small)
        cur.execute(
            "DELETE FROM qc_notifications WHERE user_code = %s AND read_at IS NOT NULL "
            "AND read_at < NOW() - INTERVAL '%s days'" % ("%s", int(KEEP_DAYS)),
            (me,),
        )
        conn.commit()
    return {"notifications": [_json(r) for r in rows], "unread": unread}


@router.post("/read-all")
def read_all(request: Request):
    user = _me(request)
    ensure_schema()
    with _connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE qc_notifications SET read_at = %s WHERE user_code = %s AND read_at IS NULL",
            (_now(), user["userCode"].lower()),
        )
        conn.commit()
    return {"unread": 0}


@router.post("/{notification_id}/read")
def read_one(request: Request, notification_id: int):
    user = _me(request)
    ensure_schema()
    with _connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE qc_notifications SET read_at = COALESCE(read_at, %s) WHERE id = %s AND user_code = %s",
            (_now(), notification_id, user["userCode"].lower()),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Notification not found.")
        conn.commit()
    return {"ok": True}