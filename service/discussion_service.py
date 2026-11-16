"""Per-screen discussion (chat) for the QC console.

Every screen (module + screen name, exactly as they appear in the module list) has its own chat.
Anyone logged in can read and write. You can edit and delete your own messages; an admin can delete
any message. Anyone can react to any message with an emoji (click again to take the reaction back).

Tables (created automatically in the same PostgreSQL database as History - HISTORY_DATABASE_URL):
    discussion_messages   one row per message (a deleted message stays as a row for the record, but it is never shown or sent out again)
    discussion_reactions  one row per (message, person, emoji)

Admins = people whose ERP account is an admin, plus the user codes listed in .env:
    QC_ADMINS=E574,E101
"""
import logging
import os
import threading
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from service.auth_service import request_user
from service.store_db import connection as _connection

logger = logging.getLogger(__name__)

MAX_BODY = 2000
MAX_LIST = 300  # newest messages returned for one screen
EMOJIS = ["👍", "👎", "❤️", "😂", "🎉", "👀", "✅", "🙏"]

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
                CREATE TABLE IF NOT EXISTS discussion_messages (
                    id BIGSERIAL PRIMARY KEY,
                    module VARCHAR(255) NOT NULL,
                    screen VARCHAR(255) NOT NULL,
                    author_code VARCHAR(64) NOT NULL,
                    author_name VARCHAR(255) NOT NULL,
                    body TEXT NOT NULL,
                    created_at TIMESTAMP NOT NULL,
                    edited_at TIMESTAMP NULL,
                    deleted_at TIMESTAMP NULL,
                    deleted_by VARCHAR(64) NULL
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS ix_discussion_screen
                ON discussion_messages (module, screen, id)
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS discussion_reactions (
                    message_id BIGINT NOT NULL REFERENCES discussion_messages(id) ON DELETE CASCADE,
                    user_code VARCHAR(64) NOT NULL,
                    user_name VARCHAR(255) NOT NULL,
                    emoji VARCHAR(16) NOT NULL,
                    created_at TIMESTAMP NOT NULL,
                    PRIMARY KEY (message_id, user_code, emoji)
                )
            """)
            conn.commit()
        _SCHEMA_READY = True


# ---------------------------------------------------------------- helpers

def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)  # stored as naive UTC, like History


def _iso(value) -> str | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def is_admin(user: dict) -> bool:
    admins = {c.strip().lower() for c in (os.getenv("QC_ADMINS") or "").split(",") if c.strip()}
    return bool(user.get("isAdmin")) or str(user.get("userCode") or "").lower() in admins


def _clean_key(value: str, label: str) -> str:
    value = (value or "").strip()
    if not value or len(value) > 255:
        raise HTTPException(status_code=400, detail=f"{label} is required.")
    return value


def _clean_body(value: str) -> str:
    value = (value or "").strip()
    if not value:
        raise HTTPException(status_code=400, detail="Write a message first.")
    if len(value) > MAX_BODY:
        raise HTTPException(status_code=400, detail=f"Messages can be at most {MAX_BODY} characters.")
    return value


def _reactions_for(cur, message_ids: list[int], me: str) -> dict[int, list[dict]]:
    if not message_ids:
        return {}
    cur.execute(
        "SELECT message_id, emoji, user_code, user_name FROM discussion_reactions "
        "WHERE message_id = ANY(%s) ORDER BY created_at, user_name",
        (message_ids,),
    )
    grouped: dict[int, dict[str, dict]] = {}
    for row in cur.fetchall():
        per_msg = grouped.setdefault(row["message_id"], {})
        item = per_msg.setdefault(row["emoji"], {"emoji": row["emoji"], "count": 0, "users": [], "mine": False})
        item["count"] += 1
        item["users"].append(row["user_name"])
        if row["user_code"] == me:
            item["mine"] = True
    return {mid: list(per.values()) for mid, per in grouped.items()}


def _to_message(row: dict, reactions: list[dict], user: dict) -> dict:
    mine = row["author_code"] == user["userCode"]
    return {
        "id": row["id"],
        "module": row["module"],
        "screen": row["screen"],
        "author_code": row["author_code"],
        "author_name": row["author_name"],
        "body": row["body"],
        "created_at": _iso(row["created_at"]),
        "edited_at": _iso(row["edited_at"]),
        "can_edit": mine,
        "can_delete": mine or is_admin(user),
        "reactions": reactions,
    }


def _load_one(cur, message_id: int, user: dict) -> dict:
    cur.execute("SELECT * FROM discussion_messages WHERE id = %s", (message_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Message not found.")
    return _to_message(row, _reactions_for(cur, [message_id], user["userCode"]).get(message_id, []), user)


def _me(request: Request) -> dict:
    user = request_user(request)
    if not user or not user.get("userCode"):
        raise HTTPException(status_code=401, detail="Please log in.")
    return user


# ---------------------------------------------------------------- routes

router = APIRouter(prefix="/api/discussion")


class NewMessage(BaseModel):
    module: str
    screen: str
    body: str


class EditMessage(BaseModel):
    body: str


class ReactBody(BaseModel):
    emoji: str


@router.get("/messages")
def list_messages(request: Request, module: str, screen: str):
    user = _me(request)
    module, screen = _clean_key(module, "Module"), _clean_key(screen, "Screen")
    _ensure_schema()
    with _connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM ("
            "  SELECT * FROM discussion_messages WHERE module = %s AND screen = %s AND deleted_at IS NULL ORDER BY id DESC LIMIT %s"
            ") t ORDER BY id ASC",
            (module, screen, MAX_LIST),
        )
        rows = cur.fetchall()
        reactions = _reactions_for(cur, [r["id"] for r in rows], user["userCode"])
    return {
        "messages": [_to_message(r, reactions.get(r["id"], []), user) for r in rows],
        "emojis": EMOJIS,
        "me": {"userCode": user["userCode"], "userName": user.get("userName") or user["userCode"], "isAdmin": is_admin(user)},
        "max_length": MAX_BODY,
    }


@router.get("/summary")
def summary(request: Request):
    """How many messages each screen has: {"counts": {"Module|Screen": {"count": n, "last_at": iso}}}."""
    _me(request)
    _ensure_schema()
    with _connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT module, screen, COUNT(*) AS n, MAX(created_at) AS last_at "
            "FROM discussion_messages WHERE deleted_at IS NULL GROUP BY module, screen"
        )
        rows = cur.fetchall()
    return {"counts": {f"{r['module']}|{r['screen']}": {"count": r["n"], "last_at": _iso(r["last_at"])} for r in rows}}


@router.post("/messages")
def post_message(request: Request, payload: NewMessage):
    user = _me(request)
    module, screen = _clean_key(payload.module, "Module"), _clean_key(payload.screen, "Screen")
    body = _clean_body(payload.body)
    _ensure_schema()
    with _connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO discussion_messages (module, screen, author_code, author_name, body, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (module, screen, user["userCode"], user.get("userName") or user["userCode"], body, _now()),
        )
        new_id = cur.fetchone()["id"]
        conn.commit()
        return _load_one(cur, new_id, user)


@router.patch("/messages/{message_id}")
def edit_message(request: Request, message_id: int, payload: EditMessage):
    user = _me(request)
    body = _clean_body(payload.body)
    _ensure_schema()
    with _connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT author_code, deleted_at FROM discussion_messages WHERE id = %s", (message_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Message not found.")
        if row["deleted_at"] is not None:
            raise HTTPException(status_code=409, detail="This message was deleted.")
        if row["author_code"] != user["userCode"]:
            raise HTTPException(status_code=403, detail="You can only edit your own messages.")
        cur.execute("UPDATE discussion_messages SET body = %s, edited_at = %s WHERE id = %s", (body, _now(), message_id))
        conn.commit()
        return _load_one(cur, message_id, user)


@router.delete("/messages/{message_id}")
def delete_message(request: Request, message_id: int):
    user = _me(request)
    _ensure_schema()
    with _connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT author_code, deleted_at FROM discussion_messages WHERE id = %s", (message_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Message not found.")
        if row["deleted_at"] is not None:
            return {"deleted": True, "id": message_id}
        if row["author_code"] != user["userCode"] and not is_admin(user):
            raise HTTPException(status_code=403, detail="You can only delete your own messages.")
        cur.execute(
            "UPDATE discussion_messages SET deleted_at = %s, deleted_by = %s WHERE id = %s",
            (_now(), user["userCode"], message_id),
        )
        conn.commit()
    logger.info("discussion message %s deleted by %s", message_id, user["userCode"])
    return {"deleted": True, "id": message_id}


@router.post("/messages/{message_id}/reactions")
def toggle_reaction(request: Request, message_id: int, payload: ReactBody):
    """React with an emoji; sending the same emoji again takes your reaction back."""
    user = _me(request)
    if payload.emoji not in EMOJIS:
        raise HTTPException(status_code=400, detail="That reaction is not available.")
    _ensure_schema()
    with _connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT deleted_at FROM discussion_messages WHERE id = %s", (message_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Message not found.")
        if row["deleted_at"] is not None:
            raise HTTPException(status_code=409, detail="This message was deleted.")
        cur.execute(
            "INSERT INTO discussion_reactions (message_id, user_code, user_name, emoji, created_at) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (message_id, user["userCode"], user.get("userName") or user["userCode"], payload.emoji, _now()),
        )
        if cur.rowcount == 0:  # already reacted -> take it back
            cur.execute(
                "DELETE FROM discussion_reactions WHERE message_id = %s AND user_code = %s AND emoji = %s",
                (message_id, user["userCode"], payload.emoji),
            )
        conn.commit()
        return _load_one(cur, message_id, user)