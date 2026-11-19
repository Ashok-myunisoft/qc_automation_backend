import logging
import os
import re
import threading
import time
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from service.auth_service import request_user
from service.store_db import connection as _connection

logger = logging.getLogger(__name__)

_CACHE_SECONDS = 10      # how long the list from the database is reused
_RETRY_SECONDS = 30      # if the database cannot be read, wait this long before trying again
_CODE_RE = re.compile(r"^[A-Za-z0-9._@-]{1,64}$")

_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY = False

_CACHE_LOCK = threading.Lock()
_cache_codes: frozenset = frozenset()
_cache_until = 0.0


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
                CREATE TABLE IF NOT EXISTS qc_roles (
                    code_key VARCHAR(64) PRIMARY KEY,
                    user_code VARCHAR(64) NOT NULL,
                    user_name VARCHAR(255) NULL,
                    added_by VARCHAR(64) NULL,
                    added_by_name VARCHAR(255) NULL,
                    added_at TIMESTAMP NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS qc_users (
                    code_key VARCHAR(64) PRIMARY KEY,
                    user_code VARCHAR(64) NOT NULL,
                    user_name VARCHAR(255) NULL,
                    last_login TIMESTAMP NOT NULL
                )
            """)
            conn.commit()
        _SCHEMA_READY = True


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)   # naive UTC, like the other QC tables


def _iso(value) -> str | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def remember_user(user: dict | None) -> None:
    """Remember who a user code belongs to (called at login and when an admin opens the Admin screen), so the
    list can show names - also for people fixed in .env. Never raises."""
    try:
        code = str((user or {}).get("userCode") or "").strip()
        if not code or code.lower() == "anonymous":
            return
        name = str((user or {}).get("userName") or "").strip()[:255] or None
        _ensure_schema()
        with _connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO qc_users (code_key, user_code, user_name, last_login) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (code_key) DO UPDATE SET user_code = EXCLUDED.user_code, "
                "user_name = COALESCE(EXCLUDED.user_name, qc_users.user_name), last_login = EXCLUDED.last_login",
                (code.lower(), code, name, _now()),
            )
            conn.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("could not remember the user name: %s", e)


# ---------------------------------------------------------------- who is an admin

def _env_entries() -> dict[str, tuple[str, str]]:
    """.env QC_ADMINS / QC_TEAM: lower-case code -> (code as written, name or ""). Format: E486 or E486:Ragul"""
    entries: dict[str, tuple[str, str]] = {}
    for var in ("QC_ADMINS", "QC_TEAM"):
        for item in (os.getenv(var) or "").split(","):
            code, _, name = item.partition(":")
            code, name = code.strip(), name.strip()
            if not code:
                continue
            key = code.lower()
            if key not in entries or (name and not entries[key][1]):
                entries[key] = (code, name)
    return entries


def _env_display() -> dict[str, str]:
    """User codes fixed in .env: lower-case code -> the code as it was written."""
    return {k: v[0] for k, v in _env_entries().items()}


def env_codes() -> set[str]:
    """User codes fixed in .env (QC_ADMINS and QC_TEAM), lower-case."""
    return set(_env_display())


def _db_codes() -> frozenset:
    """Codes added on the Admin screen. Cached; on any database problem the last known list is kept."""
    global _cache_codes, _cache_until
    now = time.monotonic()
    if now < _cache_until:
        return _cache_codes
    with _CACHE_LOCK:
        if time.monotonic() < _cache_until:
            return _cache_codes
        try:
            _ensure_schema()
            with _connection() as conn:
                cur = conn.cursor()
                cur.execute("SELECT code_key FROM qc_roles")
                _cache_codes = frozenset(str(r["code_key"]).lower() for r in cur.fetchall())
            _cache_until = time.monotonic() + _CACHE_SECONDS
        except Exception as e:  # noqa: BLE001
            logger.warning("could not read the admin list from the database (%s) - using the last known list", e)
            _cache_until = time.monotonic() + _RETRY_SECONDS
        return _cache_codes


def _forget_cache() -> None:
    global _cache_until
    _cache_until = 0.0


def all_admin_codes() -> set[str]:
    """Everybody listed as admin (.env + Admin screen), lower-case. The ERP's own admins are not in here."""
    return env_codes() | set(_db_codes())


def user_is_admin(user: dict | None) -> bool:
    if not user:
        return False
    code = str(user.get("userCode") or "").strip().lower()
    if code and code != "anonymous" and code in all_admin_codes():
        return True
    return bool(user.get("isAdmin"))     # an admin account in the ERP itself


# ---------------------------------------------------------------- routes

router = APIRouter(prefix="/api/admin/roles")


def _admin_user(request: Request) -> dict:
    user = request_user(request)
    if not user or not user.get("userCode"):
        raise HTTPException(status_code=401, detail="Please log in.")
    if not user_is_admin(user):
        raise HTTPException(status_code=403, detail="Only an admin can manage admins.")
    return user


class NewAdmin(BaseModel):
    userCode: str
    userName: str = ""


def _known_names(cur) -> dict[str, tuple[str, str]]:
    """lower-case code -> (code as written, name) for people who logged in, or wrote in a discussion, before."""
    known: dict[str, tuple[str, str]] = {}
    try:   # older source of names: the discussion messages (a name seen there is better than none)
        cur.execute("SELECT author_code, author_name FROM discussion_messages ORDER BY id DESC LIMIT 3000")
        for row in cur.fetchall():
            k = str(row["author_code"] or "").strip().lower()
            if k and k not in known and row["author_name"]:
                known[k] = (str(row["author_code"]).strip(), str(row["author_name"]).strip())
    except Exception:  # noqa: BLE001
        try:
            cur.connection.rollback()
        except Exception:  # noqa: BLE001
            pass
    cur.execute("SELECT code_key, user_code, user_name FROM qc_users")
    for row in cur.fetchall():           # a real login always wins
        if row["user_name"]:
            known[str(row["code_key"]).lower()] = (row["user_code"], row["user_name"])
    return known


def _listing(me: dict) -> dict:
    _ensure_schema()
    with _connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT user_code, user_name, added_by, added_by_name, added_at FROM qc_roles ORDER BY added_at, user_code")
        rows = cur.fetchall()
        known = _known_names(cur)
    entries = _env_entries()
    fixed = {k: v[0] for k, v in entries.items()}
    people = []
    for key in sorted(fixed):
        # a name written in .env wins; otherwise the name seen at login / in discussions
        name = entries[key][1] or known.get(key, (fixed[key], ""))[1]
        people.append({"userCode": fixed[key], "userName": name, "source": "env", "removable": False,
                       "addedBy": "", "addedAt": None})
    for r in rows:
        key = str(r["user_code"]).lower()
        if key in fixed:
            continue   # also fixed in .env: shown once, as fixed
        people.append({"userCode": r["user_code"], "userName": r["user_name"] or known.get(key, ("", ""))[1],
                       "source": "screen", "removable": True, "addedBy": r["added_by_name"] or r["added_by"] or "",
                       "addedAt": _iso(r["added_at"])})
    return {"admins": people,
            "me": {"userCode": me.get("userCode"), "userName": me.get("userName") or me.get("userCode")}}


@router.get("")
def list_admins(request: Request):
    me = _admin_user(request)
    remember_user(me)      # so the person looking at the list is shown with their name
    return _listing(me)


@router.post("")
def add_admin(request: Request, payload: NewAdmin):
    me = _admin_user(request)
    code = (payload.userCode or "").strip()
    if not _CODE_RE.match(code) or code.lower() == "anonymous":
        raise HTTPException(status_code=400, detail="Enter a valid user code (letters, numbers, . _ - @ only).")
    key = code.lower()
    if key in all_admin_codes():
        raise HTTPException(status_code=409, detail=f"{code} is already an admin.")
    name = (payload.userName or "").strip()[:255] or None
    _ensure_schema()
    with _connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO qc_roles (code_key, user_code, user_name, added_by, added_by_name, added_at) "
            "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (code_key) DO NOTHING",
            (key, code, name, me.get("userCode"), me.get("userName") or me.get("userCode"), _now()),
        )
        conn.commit()
    _forget_cache()
    logger.info("admin added: %s (by %s)", code, me.get("userCode"))
    return _listing(me)


@router.delete("/{user_code}")
def remove_admin(request: Request, user_code: str):
    me = _admin_user(request)
    key = (user_code or "").strip().lower()
    if key in env_codes():
        raise HTTPException(status_code=400, detail=f"{user_code} is set in the server's .env file. Remove it there.")
    _ensure_schema()
    with _connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM qc_roles")
        total = cur.fetchone()["n"]
        cur.execute("SELECT 1 FROM qc_roles WHERE code_key = %s", (key,))
        if cur.fetchone() is None:
            raise HTTPException(status_code=404, detail=f"{user_code} is not in the admin list.")
        if total <= 1 and not env_codes():
            raise HTTPException(status_code=400, detail="This is the last admin on the list, so it cannot be removed.")
        cur.execute("DELETE FROM qc_roles WHERE code_key = %s", (key,))
        conn.commit()
    _forget_cache()
    logger.info("admin removed: %s (by %s)", user_code, me.get("userCode"))
    return _listing(me)