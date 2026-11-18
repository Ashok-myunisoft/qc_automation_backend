"""Contact admin: a small private chat between ONE user and the admins.

* A user writes to "the admins": every admin gets a bell notification (kind "support").
* Any admin opens the Inbox on the Admins page and replies in the same chat; the user gets a bell notification.
* A user only ever sees their own conversation. Admins see all of them.
* People whose ERP account is an admin but who are not listed in .env / the Admin screen are not notified
  (the same limit the issue notifications have).

Opening a chat (the user's own, or an admin opening a user's conversation) also marks the matching bell
notifications as read, so the bell count goes down.

Tables (created automatically): support_threads (one per user), support_messages.

Routes (login required):
    GET  /api/support/admins            who the admins are (for the "Contact admin" list)
    GET  /api/support/thread            my conversation
    POST /api/support/thread            I write to the admins          {"body": "..."}
    GET  /api/support/inbox             admin: all conversations + how many are unread
    GET  /api/support/inbox/{key}       admin: one conversation (marks it as read)
    POST /api/support/inbox/{key}       admin: reply                   {"body": "..."}
"""
import logging
import threading

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from service import roles_service
from service.discussion_service import _iso, _me, _now
from service.notification_service import notify
from service.store_db import connection as _connection

logger = logging.getLogger(__name__)

MAX_MESSAGE = 2000
MAX_MESSAGES = 300
_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY = False


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
                CREATE TABLE IF NOT EXISTS support_threads (
                    code_key VARCHAR(64) PRIMARY KEY,
                    user_code VARCHAR(64) NOT NULL,
                    user_name VARCHAR(255) NULL,
                    last_message_at TIMESTAMP NOT NULL,
                    last_preview VARCHAR(200) NOT NULL DEFAULT '',
                    last_from_admin BOOLEAN NOT NULL DEFAULT FALSE,
                    admin_seen_at TIMESTAMP NULL,
                    user_seen_at TIMESTAMP NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS support_messages (
                    id BIGSERIAL PRIMARY KEY,
                    thread_key VARCHAR(64) NOT NULL,
                    sender_code VARCHAR(64) NOT NULL,
                    sender_name VARCHAR(255) NULL,
                    from_admin BOOLEAN NOT NULL DEFAULT FALSE,
                    body TEXT NOT NULL,
                    created_at TIMESTAMP NOT NULL
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS ix_support_messages_thread ON support_messages (thread_key, id)")
            conn.commit()
        _SCHEMA_READY = True


def _name(user: dict) -> str:
    return user.get("userName") or user.get("userCode") or "?"


def _key(code: str) -> str:
    return str(code or "").strip().lower()


def _admin(request: Request) -> dict:
    me = _me(request)
    if not roles_service.user_is_admin(me):
        raise HTTPException(status_code=403, detail="Only an admin can open the inbox.")
    return me


class NewMessage(BaseModel):
    body: str


def _clean(body: str) -> str:
    text = (body or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Write a message first.")
    if len(text) > MAX_MESSAGE:
        raise HTTPException(status_code=400, detail=f"A message can have at most {MAX_MESSAGE} characters.")
    return text


def _json(row: dict) -> dict:
    return {"id": row["id"], "from_admin": bool(row["from_admin"]), "sender_name": row["sender_name"] or row["sender_code"],
            "sender_code": row["sender_code"], "body": row["body"], "created_at": _iso(row["created_at"])}


def _mark_chat_notifications_read(cur, user_code: str, key: str) -> None:
    """The person has opened this chat, so its bell notifications are seen (the bell count goes down). Never raises."""
    try:
        cur.execute("SAVEPOINT qc_chat_seen")
        cur.execute(
            "UPDATE qc_notifications SET read_at = %s WHERE user_code = %s AND kind = 'support' AND ref = %s AND read_at IS NULL",
            (_now(), _key(user_code), key),
        )
        cur.execute("RELEASE SAVEPOINT qc_chat_seen")
    except Exception:  # noqa: BLE001  (no notifications table yet = nothing to mark)
        try:
            cur.execute("ROLLBACK TO SAVEPOINT qc_chat_seen")
        except Exception:  # noqa: BLE001
            pass


def _preview(text: str, n: int = 80) -> str:
    one = " ".join(text.split())
    return one if len(one) <= n else one[: n - 1] + "…"


def _messages(cur, key: str) -> list[dict]:
    cur.execute("SELECT * FROM (SELECT * FROM support_messages WHERE thread_key = %s ORDER BY id DESC LIMIT %s) t ORDER BY id ASC",
                (key, MAX_MESSAGES))
    return [_json(r) for r in cur.fetchall()]


router = APIRouter(prefix="/api/support")


@router.get("/admins")
def admins(request: Request):
    me = _me(request)
    people = roles_service._listing(me)["admins"]
    out = [{"userCode": p["userCode"], "userName": p["userName"] or p["userCode"]} for p in people]
    out.sort(key=lambda p: p["userName"].lower())
    return {"admins": out}


@router.get("/thread")
def my_thread(request: Request):
    me = _me(request)
    _ensure_schema()
    key = _key(me["userCode"])
    with _connection() as conn:
        cur = conn.cursor()
        messages = _messages(cur, key)
        cur.execute("UPDATE support_threads SET user_seen_at = %s WHERE code_key = %s", (_now(), key))
        _mark_chat_notifications_read(cur, me["userCode"], key)
        conn.commit()
    return {"messages": messages}


@router.post("/thread")
def write_to_admins(request: Request, payload: NewMessage):
    me = _me(request)
    text = _clean(payload.body)
    _ensure_schema()
    key, now = _key(me["userCode"]), _now()
    with _connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO support_threads (code_key, user_code, user_name, last_message_at, last_preview, last_from_admin, user_seen_at) "
            "VALUES (%s, %s, %s, %s, %s, FALSE, %s) "
            "ON CONFLICT (code_key) DO UPDATE SET user_name = EXCLUDED.user_name, last_message_at = EXCLUDED.last_message_at, "
            "last_preview = EXCLUDED.last_preview, last_from_admin = FALSE, user_seen_at = EXCLUDED.user_seen_at",
            (key, me["userCode"], _name(me), now, _preview(text, 200), now),
        )
        cur.execute(
            "INSERT INTO support_messages (thread_key, sender_code, sender_name, from_admin, body, created_at) "
            "VALUES (%s, %s, %s, FALSE, %s, %s) RETURNING *",
            (key, me["userCode"], _name(me), text, now),
        )
        row = cur.fetchone()
        notify(cur, roles_service.all_admin_codes(), "support", None,
               f"{_name(me)} wrote to the admins: {_preview(text)}", me, ref=key)
        conn.commit()
    return _json(row)


@router.get("/inbox")
def inbox(request: Request):
    _admin(request)
    _ensure_schema()
    with _connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM support_threads ORDER BY last_message_at DESC LIMIT 200")
        rows = cur.fetchall()
    threads = []
    for r in rows:
        unread = (not r["last_from_admin"]) and (r["admin_seen_at"] is None or r["admin_seen_at"] < r["last_message_at"])
        threads.append({"key": r["code_key"], "userCode": r["user_code"], "userName": r["user_name"] or r["user_code"],
                        "preview": r["last_preview"], "lastAt": _iso(r["last_message_at"]),
                        "lastFromAdmin": bool(r["last_from_admin"]), "unread": unread})
    return {"threads": threads, "unread": sum(1 for t in threads if t["unread"])}


@router.get("/inbox/{key}")
def open_thread(request: Request, key: str):
    me = _admin(request)
    _ensure_schema()
    key = _key(key)
    with _connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM support_threads WHERE code_key = %s", (key,))
        t = cur.fetchone()
        if t is None:
            raise HTTPException(status_code=404, detail="This conversation does not exist.")
        messages = _messages(cur, key)
        cur.execute("UPDATE support_threads SET admin_seen_at = %s WHERE code_key = %s", (_now(), key))
        _mark_chat_notifications_read(cur, me["userCode"], key)
        conn.commit()
    return {"user": {"userCode": t["user_code"], "userName": t["user_name"] or t["user_code"]}, "messages": messages}


@router.post("/inbox/{key}")
def reply(request: Request, key: str, payload: NewMessage):
    me = _admin(request)
    text = _clean(payload.body)
    _ensure_schema()
    key, now = _key(key), _now()
    with _connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM support_threads WHERE code_key = %s", (key,))
        t = cur.fetchone()
        if t is None:
            raise HTTPException(status_code=404, detail="This conversation does not exist.")
        cur.execute(
            "INSERT INTO support_messages (thread_key, sender_code, sender_name, from_admin, body, created_at) "
            "VALUES (%s, %s, %s, TRUE, %s, %s) RETURNING *",
            (key, me["userCode"], _name(me), text, now),
        )
        row = cur.fetchone()
        cur.execute(
            "UPDATE support_threads SET last_message_at = %s, last_preview = %s, last_from_admin = TRUE, admin_seen_at = %s WHERE code_key = %s",
            (now, _preview(text, 200), now, key),
        )
        notify(cur, [t["user_code"]], "support", None, f"{_name(me)} replied to your message: {_preview(text)}", me, ref=key)
        conn.commit()
    return _json(row)