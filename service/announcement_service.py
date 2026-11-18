"""Announcements: a message an admin publishes for everybody.

* Shown in the bell (one notification per user), in a popup after login, on the Contact admin page,
  and (warnings only) as a banner at the top.
* An admin can set when it starts ("show from") and the exact time it ends, extend it, end it early or remove it.
* Who has seen it is stored here on the server, so a person sees the popup once - on any computer.

Tables (created automatically): qc_announcements, qc_announcement_seen

Routes (login required):
    GET    /api/announcements                 everybody: announcements that are active now (+ whether I have seen them)
    POST   /api/announcements/seen            everybody: I have seen these  {"ids": [1, 2]}  (no ids = all active)
    GET    /api/admin/announcements           admin: recent announcements (scheduled, active, ended)
    POST   /api/admin/announcements           admin: {"message", "level", "startsAt", "endsAt"}  (ISO times, both optional)
    PATCH  /api/admin/announcements/{id}      admin: {"endsAt": "..."} | {"noEnd": true} | {"endNow": true}
    DELETE /api/admin/announcements/{id}      admin: remove one

Bell notifications are created lazily: the first time a person's bell is checked after an announcement became
active, one notification (kind "announcement", ref "ann:<id>") is added for that person - see sync_notifications().
"""
import logging
import threading
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from service import roles_service
from service.discussion_service import _iso, _me, _now
from service.notification_service import ensure_schema as _ensure_notifications
from service.store_db import connection as _connection

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

MAX_ANNOUNCEMENT = 300
MAX_DAYS_AHEAD = 365
NOTIFY_WINDOW_DAYS = 30        # an announcement older than this no longer creates new bell notifications

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
            cur.execute("ALTER TABLE qc_announcements ADD COLUMN IF NOT EXISTS starts_at TIMESTAMP NULL")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS qc_announcement_seen (
                    ann_id BIGINT NOT NULL,
                    user_code VARCHAR(64) NOT NULL,
                    seen_at TIMESTAMP NOT NULL,
                    PRIMARY KEY (ann_id, user_code)
                )
            """)
            conn.commit()
        _SCHEMA_READY = True


# ---------------------------------------------------------------- helpers

def _admin(request: Request) -> dict:
    me = _me(request)
    if not roles_service.user_is_admin(me):
        raise HTTPException(status_code=403, detail="Only an admin can do this.")
    return me


def _parse_time(value: str | None, label: str) -> datetime | None:
    """ISO time from the browser (with a zone, usually 'Z') -> naive UTC, like every other QC table."""
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip()
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{label} is not a valid date and time.")
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.replace(second=0, microsecond=0)


def _state(r: dict, now: datetime) -> str:
    if r.get("starts_at") and r["starts_at"] > now:
        return "scheduled"
    if r.get("expires_at") and r["expires_at"] <= now:
        return "ended"
    return "active"


def _ann_json(r: dict, now: datetime, seen: bool | None = None) -> dict:
    out = {
        "id": r["id"], "message": r["message"], "level": r["level"],
        "by": r["created_by_name"] or r["created_by_code"] or "",
        "createdAt": _iso(r["created_at"]),
        "startsAt": _iso(r.get("starts_at")), "endsAt": _iso(r["expires_at"]),
        "state": _state(r, now),
    }
    out["active"] = out["state"] == "active"
    if seen is not None:
        out["seen"] = seen
    return out


ACTIVE_SQL = "(a.starts_at IS NULL OR a.starts_at <= %s) AND (a.expires_at IS NULL OR a.expires_at > %s)"


def sync_notifications(cur, user_code: str) -> None:
    """Make sure this person has a bell notification for every announcement that is active now.
    Cheap (the announcements table is tiny) and safe to call often. Never raises."""
    try:
        ensure_schema()
        _ensure_notifications()
        code = str(user_code or "").strip().lower()
        if not code:
            return
        now = _now()
        cur.execute("SAVEPOINT qc_ann_sync")
        cur.execute(
            "INSERT INTO qc_notifications (user_code, kind, issue_id, message, actor_code, actor_name, created_at, ref) "
            "SELECT %s, 'announcement', NULL, LEFT(a.message, 480), a.created_by_code, a.created_by_name, "
            "       COALESCE(a.starts_at, a.created_at), 'ann:' || a.id "
            "FROM qc_announcements a "
            f"WHERE {ACTIVE_SQL} "
            "  AND COALESCE(a.starts_at, a.created_at) > %s "
            "  AND LOWER(COALESCE(a.created_by_code, '')) <> %s "
            "  AND NOT EXISTS (SELECT 1 FROM qc_notifications n "
            "                  WHERE n.user_code = %s AND n.kind = 'announcement' AND n.ref = 'ann:' || a.id) "
            "  AND NOT EXISTS (SELECT 1 FROM qc_announcement_seen s WHERE s.ann_id = a.id AND s.user_code = %s)",
            (code, now, now, now - timedelta(days=NOTIFY_WINDOW_DAYS), code, code, code),
        )
        cur.execute("RELEASE SAVEPOINT qc_ann_sync")
    except Exception as e:  # noqa: BLE001
        logger.warning("could not sync announcement notifications: %s", e)
        try:
            cur.execute("ROLLBACK TO SAVEPOINT qc_ann_sync")
        except Exception:  # noqa: BLE001
            pass


def mark_seen_from_notifications(cur, user_code: str, only_read_at: datetime | None = None) -> None:
    """Called when bell notifications are marked as read: the matching announcements count as seen."""
    try:
        ensure_schema()
        code = str(user_code or "").strip().lower()
        sql = ("INSERT INTO qc_announcement_seen (ann_id, user_code, seen_at) "
               "SELECT CAST(SUBSTRING(ref FROM 5) AS BIGINT), user_code, read_at FROM qc_notifications "
               "WHERE user_code = %s AND kind = 'announcement' AND ref LIKE 'ann:%%' AND read_at IS NOT NULL ")
        args: list = [code]
        if only_read_at is not None:
            sql += "AND read_at = %s "
            args.append(only_read_at)
        sql += "ON CONFLICT DO NOTHING"
        cur.execute("SAVEPOINT qc_ann_seen")
        cur.execute(sql, args)
        cur.execute("RELEASE SAVEPOINT qc_ann_seen")
    except Exception as e:  # noqa: BLE001
        logger.warning("could not store seen announcements: %s", e)
        try:
            cur.execute("ROLLBACK TO SAVEPOINT qc_ann_seen")
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- everybody

@router.get("/announcements")
def active_announcements(request: Request):
    me = _me(request)
    ensure_schema()
    code, now = str(me["userCode"]).strip().lower(), _now()
    with _connection() as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT a.*, EXISTS (SELECT 1 FROM qc_announcement_seen s WHERE s.ann_id = a.id AND s.user_code = %s) AS seen "
            f"FROM qc_announcements a WHERE {ACTIVE_SQL} ORDER BY a.id DESC LIMIT 10",
            (code, now, now),
        )
        rows = cur.fetchall()
        sync_notifications(cur, code)
        conn.commit()
    return {"announcements": [_ann_json(r, now, bool(r["seen"])) for r in rows]}


class SeenPayload(BaseModel):
    ids: list[int] | None = None


@router.post("/announcements/seen")
def mark_seen(request: Request, payload: SeenPayload):
    me = _me(request)
    ensure_schema()
    _ensure_notifications()
    code, now = str(me["userCode"]).strip().lower(), _now()
    with _connection() as conn:
        cur = conn.cursor()
        if payload.ids:
            ids = [int(i) for i in payload.ids][:50]
            cur.execute(
                "INSERT INTO qc_announcement_seen (ann_id, user_code, seen_at) "
                "SELECT a.id, %s, %s FROM qc_announcements a WHERE a.id = ANY(%s) ON CONFLICT DO NOTHING",
                (code, now, ids),
            )
            cur.execute(
                "UPDATE qc_notifications SET read_at = COALESCE(read_at, %s) "
                "WHERE user_code = %s AND kind = 'announcement' AND ref = ANY(%s)",
                (now, code, [f"ann:{i}" for i in ids]),
            )
        else:
            cur.execute(
                f"INSERT INTO qc_announcement_seen (ann_id, user_code, seen_at) "
                f"SELECT a.id, %s, %s FROM qc_announcements a WHERE {ACTIVE_SQL} ON CONFLICT DO NOTHING",
                (code, now, now, now),
            )
            cur.execute(
                "UPDATE qc_notifications SET read_at = COALESCE(read_at, %s) WHERE user_code = %s AND kind = 'announcement'",
                (now, code),
            )
        conn.commit()
    return {"ok": True}


# ---------------------------------------------------------------- admin

@router.get("/admin/announcements")
def all_announcements(request: Request):
    _admin(request)
    ensure_schema()
    now = _now()
    with _connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT a.*, (SELECT COUNT(*) FROM qc_announcement_seen s WHERE s.ann_id = a.id) AS seen_count "
            "FROM qc_announcements a ORDER BY a.id DESC LIMIT 30"
        )
        rows = cur.fetchall()
    items = []
    for r in rows:
        j = _ann_json(r, now)
        j["seenCount"] = int(r["seen_count"] or 0)
        items.append(j)
    return {"announcements": items}


class NewAnnouncement(BaseModel):
    message: str
    level: str = "info"
    startsAt: str | None = None     # None = right now
    endsAt: str | None = None       # None = until an admin ends it


def _check_window(starts: datetime | None, ends: datetime | None, now: datetime) -> None:
    if ends is not None:
        if ends <= now:
            raise HTTPException(status_code=400, detail="The end time is already in the past.")
        if starts is not None and ends <= starts:
            raise HTTPException(status_code=400, detail="The end time must be after the start time.")
        if ends > now + timedelta(days=MAX_DAYS_AHEAD):
            raise HTTPException(status_code=400, detail="Choose an end time within the next year, or leave it empty.")
    if starts is not None and starts > now + timedelta(days=MAX_DAYS_AHEAD):
        raise HTTPException(status_code=400, detail="Choose a start time within the next year.")


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
    starts, ends = _parse_time(payload.startsAt, "The start time"), _parse_time(payload.endsAt, "The end time")
    if starts is not None and starts <= now:
        starts = None                      # "from now" - nothing to schedule
    _check_window(starts, ends, now)
    ensure_schema()
    with _connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO qc_announcements (message, level, created_by_code, created_by_name, created_at, starts_at, expires_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *",
            (text, level, me.get("userCode"), me.get("userName") or me.get("userCode"), now, starts, ends),
        )
        row = cur.fetchone()
        conn.commit()
    return _ann_json(row, now)


class ChangeAnnouncement(BaseModel):
    endsAt: str | None = None
    noEnd: bool = False
    endNow: bool = False


@router.patch("/admin/announcements/{ann_id}")
def change_announcement(request: Request, ann_id: int, payload: ChangeAnnouncement):
    _admin(request)
    ensure_schema()
    now = _now()
    with _connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM qc_announcements WHERE id = %s", (ann_id,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="That announcement is already gone.")
        if payload.endNow:
            new_end = now
        elif payload.noEnd:
            new_end = None
        else:
            new_end = _parse_time(payload.endsAt, "The end time")
            if new_end is None:
                raise HTTPException(status_code=400, detail="Choose the new end time.")
            _check_window(row.get("starts_at"), new_end, now)
        cur.execute("UPDATE qc_announcements SET expires_at = %s WHERE id = %s RETURNING *", (new_end, ann_id))
        row = cur.fetchone()
        conn.commit()
    return _ann_json(row, now)


@router.delete("/admin/announcements/{ann_id}")
def remove_announcement(request: Request, ann_id: int):
    _admin(request)
    ensure_schema()
    _ensure_notifications()
    with _connection() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM qc_announcements WHERE id = %s", (ann_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="That announcement is already gone.")
        cur.execute("DELETE FROM qc_announcement_seen WHERE ann_id = %s", (ann_id,))
        cur.execute("DELETE FROM qc_notifications WHERE kind = 'announcement' AND ref = %s", (f"ann:{ann_id}",))
        conn.commit()
    return {"ok": True}