"""Personal notifications (the bell icon) for the QC console.

Who gets notified
-----------------
* A new issue is raised        -> every admin (.env QC_ADMINS / QC_TEAM and the Admin screen)
* The status of an issue moves -> the person who raised it, the person handling it, and everybody who commented
* A comment is added           -> the same people (plus the QC team when a non-QC person writes)
Nobody is ever notified about their OWN action.

Table (created automatically in the same PostgreSQL database as History):
    qc_notifications   one row per person per event

Announcements (kind "announcement", ref "ann:<id>") are added to everybody's bell by announcement_service.
An announcement that has ended no longer counts as unread. Opening a chat (Contact admin) marks its notifications as read.

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
from service.roles_service import all_admin_codes
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
            cur.execute("ALTER TABLE qc_notifications ADD COLUMN IF NOT EXISTS ref VARCHAR(64) NULL")   # e.g. the chat a notification belongs to
            cur.execute("CREATE INDEX IF NOT EXISTS ix_qc_notif_user ON qc_notifications (user_code, id)")
            conn.commit()
        _SCHEMA_READY = True


def team_codes() -> set[str]:
    """Everybody who is an admin (.env + Admin screen), lower-case."""
    return all_admin_codes()


def notify(cur, recipients, kind: str, issue_id: int | None, message: str, actor: dict, ref: str | None = None) -> None:
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
                "INSERT INTO qc_notifications (user_code, kind, issue_id, message, actor_code, actor_name, created_at, ref) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (code, kind, issue_id, message[:500], actor_code, actor_name, now, ref),
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

# An announcement notification only counts while its announcement is active (started, not ended, not removed).
_ANN_ACTIVE = (
    "EXISTS (SELECT 1 FROM qc_announcements a WHERE 'ann:' || a.id = n.ref "
    "AND (a.starts_at IS NULL OR a.starts_at <= %s) AND (a.expires_at IS NULL OR a.expires_at > %s))"
)


def _prepare(cur, me: str) -> None:
    """Make sure the announcement tables exist and this person has a notification for every active announcement."""
    from service import announcement_service
    announcement_service.ensure_schema()
    announcement_service.sync_notifications(cur, me)


def _unread(cur, me: str) -> int:
    now = _now()
    cur.execute(
        "SELECT COUNT(*) AS n FROM qc_notifications n WHERE n.user_code = %s AND n.read_at IS NULL "
        f"AND (n.kind <> 'announcement' OR {_ANN_ACTIVE})",
        (me, now, now),
    )
    return cur.fetchone()["n"]


def _json(row: dict) -> dict:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "issue_id": row["issue_id"],
        "ref": row.get("ref"),
        "message": row["message"],
        "actor_name": row["actor_name"],
        "created_at": _iso(row["created_at"]),
        "read": row["read_at"] is not None or bool(row.get("expired")),
        "expired": bool(row.get("expired")),
    }


@router.get("/count")
def unread_count(request: Request):
    user = _me(request)
    ensure_schema()
    me = user["userCode"].lower()
    with _connection() as conn:
        cur = conn.cursor()
        _prepare(cur, me)
        unread = _unread(cur, me)
        conn.commit()
        return {"unread": unread}


@router.get("")
def list_notifications(request: Request, limit: int = DEFAULT_LIMIT):
    user = _me(request)
    limit = max(1, min(int(limit), MAX_LIMIT))
    me = user["userCode"].lower()
    ensure_schema()
    with _connection() as conn:
        cur = conn.cursor()
        _prepare(cur, me)
        now = _now()
        cur.execute(
            f"SELECT n.*, (n.kind = 'announcement' AND NOT {_ANN_ACTIVE}) AS expired "
            "FROM qc_notifications n WHERE n.user_code = %s ORDER BY n.id DESC LIMIT %s",
            (now, now, me, limit),
        )
        rows = cur.fetchall()
        unread = _unread(cur, me)
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
    me, now = user["userCode"].lower(), _now()
    with _connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE qc_notifications SET read_at = %s WHERE user_code = %s AND read_at IS NULL",
            (now, me),
        )
        from service import announcement_service
        announcement_service.mark_seen_from_notifications(cur, me, now)
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
        from service import announcement_service
        announcement_service.mark_seen_from_notifications(cur, user["userCode"].lower())
        conn.commit()
    return {"ok": True}