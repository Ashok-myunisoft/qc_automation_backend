"""Issues: concerns raised to the QC team, tracked until they are fixed.

Who can do what
---------------
* Anyone logged in: raise an issue, read all issues, comment, edit the title/description of THEIR OWN issue.
* QC team: everything above on any issue, plus change the status (Open / Looking into it / Fixed) and the severity.
* Admins: all of the above, plus delete an issue.

The QC team is the list of user codes in .env (admins count as QC team too):
    QC_TEAM=E574,E101
    QC_ADMINS=E574

Tables (created automatically in the same PostgreSQL database as History):
    qc_issues          one row per issue
    qc_issue_comments  comments and the automatic "status changed" lines of an issue
"""
import logging
import os
import threading

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from service.discussion_service import _iso, _me, _now, is_admin
from service.notification_service import notify, team_codes
from service.store_db import connection as _connection

logger = logging.getLogger(__name__)

STATUSES = {"open": "Open", "looking": "Looking into it", "fixed": "Fixed"}
SEVERITIES = {"low": "Low", "medium": "Medium", "high": "High", "critical": "Critical"}
MAX_TITLE = 200
MAX_TEXT = 4000
MAX_COMMENT = 2000
MAX_LIST = 500

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
                CREATE TABLE IF NOT EXISTS qc_issues (
                    id BIGSERIAL PRIMARY KEY,
                    title VARCHAR(200) NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    module VARCHAR(255) NULL,
                    screen VARCHAR(255) NULL,
                    severity VARCHAR(16) NOT NULL DEFAULT 'medium',
                    status VARCHAR(16) NOT NULL DEFAULT 'open',
                    raised_by_code VARCHAR(64) NOT NULL,
                    raised_by_name VARCHAR(255) NOT NULL,
                    handled_by_code VARCHAR(64) NULL,
                    handled_by_name VARCHAR(255) NULL,
                    created_at TIMESTAMP NOT NULL,
                    updated_at TIMESTAMP NOT NULL
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS ix_qc_issues_status ON qc_issues (status, id)")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS qc_issue_comments (
                    id BIGSERIAL PRIMARY KEY,
                    issue_id BIGINT NOT NULL REFERENCES qc_issues(id) ON DELETE CASCADE,
                    kind VARCHAR(16) NOT NULL DEFAULT 'comment',
                    author_code VARCHAR(64) NOT NULL,
                    author_name VARCHAR(255) NOT NULL,
                    body TEXT NOT NULL,
                    created_at TIMESTAMP NOT NULL
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS ix_qc_issue_comments ON qc_issue_comments (issue_id, id)")
            conn.commit()
        _SCHEMA_READY = True


# ---------------------------------------------------------------- helpers

def is_team(user: dict) -> bool:
    team = {c.strip().lower() for c in (os.getenv("QC_TEAM") or "").split(",") if c.strip()}
    return is_admin(user) or str(user.get("userCode") or "").lower() in team


def _name(user: dict) -> str:
    return user.get("userName") or user["userCode"]


def _text(value, label: str, limit: int, required: bool) -> str:
    value = (value or "").strip()
    if required and not value:
        raise HTTPException(status_code=400, detail=f"{label} is required.")
    if len(value) > limit:
        raise HTTPException(status_code=400, detail=f"{label} can be at most {limit} characters.")
    return value


def _opt_key(value) -> str | None:
    value = (value or "").strip()
    if len(value) > 255:
        raise HTTPException(status_code=400, detail="Module / screen name is too long.")
    return value or None


def _issue_json(row: dict, user: dict, comment_count: int) -> dict:
    mine = row["raised_by_code"] == user["userCode"]
    team = is_team(user)
    return {
        "id": row["id"],
        "title": row["title"],
        "description": row["description"],
        "module": row["module"],
        "screen": row["screen"],
        "severity": row["severity"],
        "status": row["status"],
        "raised_by_code": row["raised_by_code"],
        "raised_by_name": row["raised_by_name"],
        "handled_by_code": row["handled_by_code"],
        "handled_by_name": row["handled_by_name"],
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
        "comment_count": comment_count,
        "can_edit": mine or team,
        "can_set_status": team,
        "can_delete": is_admin(user),
    }


def _comment_json(row: dict, user: dict) -> dict:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "author_code": row["author_code"],
        "author_name": row["author_name"],
        "body": row["body"],
        "created_at": _iso(row["created_at"]),
        "can_delete": row["kind"] == "comment" and (row["author_code"] == user["userCode"] or is_team(user)),
    }


def _get_issue(cur, issue_id: int) -> dict:
    cur.execute("SELECT * FROM qc_issues WHERE id = %s", (issue_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Issue not found.")
    return row


def _comment_count(cur, issue_id: int) -> int:
    cur.execute("SELECT COUNT(*) AS n FROM qc_issue_comments WHERE issue_id = %s AND kind = 'comment'", (issue_id,))
    return cur.fetchone()["n"]


def _add_event(cur, issue_id: int, user: dict, kind: str, body: str) -> None:
    cur.execute(
        "INSERT INTO qc_issue_comments (issue_id, kind, author_code, author_name, body, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (issue_id, kind, user["userCode"], _name(user), body, _now()),
    )


def _me_info(user: dict) -> dict:
    return {"userCode": user["userCode"], "userName": _name(user), "isTeam": is_team(user), "isAdmin": is_admin(user)}


def _people_of_issue(cur, row: dict) -> set[str]:
    """Everybody who follows an issue: who raised it, who handles it, and everybody who commented."""
    people = {row["raised_by_code"]}
    if row.get("handled_by_code"):
        people.add(row["handled_by_code"])
    cur.execute("SELECT DISTINCT author_code FROM qc_issue_comments WHERE issue_id = %s AND kind = 'comment'", (row["id"],))
    people |= {r["author_code"] for r in cur.fetchall()}
    return people


# ---------------------------------------------------------------- routes

router = APIRouter(prefix="/api/issues")


class NewIssue(BaseModel):
    title: str
    description: str = ""
    module: str | None = None
    screen: str | None = None
    severity: str = "medium"


class IssuePatch(BaseModel):
    title: str | None = None
    description: str | None = None
    module: str | None = None
    screen: str | None = None
    severity: str | None = None
    status: str | None = None


class NewComment(BaseModel):
    body: str


@router.get("/summary")
def summary(request: Request):
    """Issue counts per status - used for the badge next to 'Issues' in the sidebar."""
    user = _me(request)
    _ensure_schema()
    with _connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT status, COUNT(*) AS n FROM qc_issues GROUP BY status")
        counts = {s: 0 for s in STATUSES}
        for r in cur.fetchall():
            counts[r["status"]] = r["n"]
    return {"counts": counts, "me": _me_info(user)}


@router.get("")
def list_issues(request: Request, status: str = "", mine: bool = False, module: str = "", q: str = ""):
    user = _me(request)
    _ensure_schema()
    where, params = [], []
    if status:
        if status not in STATUSES:
            raise HTTPException(status_code=400, detail="Unknown status.")
        where.append("i.status = %s")
        params.append(status)
    if mine:
        where.append("i.raised_by_code = %s")
        params.append(user["userCode"])
    if module.strip():
        where.append("i.module = %s")
        params.append(module.strip())
    if q.strip():
        like = f"%{q.strip()}%"
        where.append("(i.title ILIKE %s OR i.description ILIKE %s OR i.screen ILIKE %s)")
        params += [like, like, like]
    sql = (
        "SELECT i.*, (SELECT COUNT(*) FROM qc_issue_comments c WHERE c.issue_id = i.id AND c.kind = 'comment') AS n_comments "
        "FROM qc_issues i" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY i.id DESC LIMIT %s"
    )
    params.append(MAX_LIST)
    with _connection() as conn:
        cur = conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()
        cur.execute("SELECT status, COUNT(*) AS n FROM qc_issues GROUP BY status")
        counts = {s: 0 for s in STATUSES}
        for r in cur.fetchall():
            counts[r["status"]] = r["n"]
    return {
        "issues": [_issue_json(r, user, r["n_comments"]) for r in rows],
        "counts": counts,
        "me": _me_info(user),
        "statuses": STATUSES,
        "severities": SEVERITIES,
    }


@router.post("")
def create_issue(request: Request, payload: NewIssue):
    user = _me(request)
    title = _text(payload.title, "Title", MAX_TITLE, True)
    description = _text(payload.description, "Description", MAX_TEXT, False)
    if payload.severity not in SEVERITIES:
        raise HTTPException(status_code=400, detail="Unknown severity.")
    module, screen = _opt_key(payload.module), _opt_key(payload.screen)
    if screen and not module:
        raise HTTPException(status_code=400, detail="Pick the module of that screen.")
    _ensure_schema()
    now = _now()
    with _connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO qc_issues (title, description, module, screen, severity, status, raised_by_code, raised_by_name, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, 'open', %s, %s, %s, %s) RETURNING id",
            (title, description, module, screen, payload.severity, user["userCode"], _name(user), now, now),
        )
        new_id = cur.fetchone()["id"]
        notify(cur, team_codes(), "issue_new", new_id,
               f"{_name(user)} raised a {SEVERITIES[payload.severity]} issue: {title}", user)
        conn.commit()
        return _issue_json(_get_issue(cur, new_id), user, 0)


@router.get("/{issue_id}")
def get_issue(request: Request, issue_id: int):
    user = _me(request)
    _ensure_schema()
    with _connection() as conn:
        cur = conn.cursor()
        row = _get_issue(cur, issue_id)
        cur.execute("SELECT * FROM qc_issue_comments WHERE issue_id = %s ORDER BY id ASC", (issue_id,))
        comments = cur.fetchall()
    return {
        "issue": _issue_json(row, user, sum(1 for c in comments if c["kind"] == "comment")),
        "comments": [_comment_json(c, user) for c in comments],
        "me": _me_info(user),
    }


@router.patch("/{issue_id}")
def update_issue(request: Request, issue_id: int, payload: IssuePatch):
    user = _me(request)
    team = is_team(user)
    _ensure_schema()
    with _connection() as conn:
        cur = conn.cursor()
        row = _get_issue(cur, issue_id)
        mine = row["raised_by_code"] == user["userCode"]
        sets, params, events = [], [], []

        if any(v is not None for v in (payload.title, payload.description, payload.module, payload.screen)):
            if not (mine or team):
                raise HTTPException(status_code=403, detail="Only the person who raised this issue or the QC team can edit it.")
        if payload.title is not None:
            sets.append("title = %s"); params.append(_text(payload.title, "Title", MAX_TITLE, True))
        if payload.description is not None:
            sets.append("description = %s"); params.append(_text(payload.description, "Description", MAX_TEXT, False))
        if payload.module is not None or payload.screen is not None:
            module = _opt_key(payload.module) if payload.module is not None else row["module"]
            screen = _opt_key(payload.screen) if payload.screen is not None else row["screen"]
            if screen and not module:
                raise HTTPException(status_code=400, detail="Pick the module of that screen.")
            sets += ["module = %s", "screen = %s"]; params += [module, screen]

        if payload.severity is not None and payload.severity != row["severity"]:
            if not team:
                raise HTTPException(status_code=403, detail="Only the QC team can change the severity.")
            if payload.severity not in SEVERITIES:
                raise HTTPException(status_code=400, detail="Unknown severity.")
            sets.append("severity = %s"); params.append(payload.severity)
            events.append(("severity", f"changed the severity from {SEVERITIES[row['severity']]} to {SEVERITIES[payload.severity]}"))

        if payload.status is not None and payload.status != row["status"]:
            if not team:
                raise HTTPException(status_code=403, detail="Only the QC team can change the status.")
            if payload.status not in STATUSES:
                raise HTTPException(status_code=400, detail="Unknown status.")
            sets.append("status = %s"); params.append(payload.status)
            if payload.status == "open":
                sets += ["handled_by_code = NULL", "handled_by_name = NULL"]
            else:
                sets += ["handled_by_code = %s", "handled_by_name = %s"]; params += [user["userCode"], _name(user)]
            events.append(("status", f"changed the status from {STATUSES[row['status']]} to {STATUSES[payload.status]}"))

        if sets:
            sets.append("updated_at = %s"); params.append(_now())
            cur.execute(f"UPDATE qc_issues SET {', '.join(sets)} WHERE id = %s", params + [issue_id])
            for kind, text in events:
                _add_event(cur, issue_id, user, "status", text)
            if payload.status is not None and payload.status != row["status"]:
                notify(cur, _people_of_issue(cur, row), "issue_status", issue_id,
                       f"{_name(user)} set \"{row['title']}\" to {STATUSES[payload.status]}", user)
            conn.commit()
        return _issue_json(_get_issue(cur, issue_id), user, _comment_count(cur, issue_id))


@router.delete("/{issue_id}")
def delete_issue(request: Request, issue_id: int):
    user = _me(request)
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="Only an admin can delete an issue.")
    _ensure_schema()
    with _connection() as conn:
        cur = conn.cursor()
        _get_issue(cur, issue_id)
        cur.execute("DELETE FROM qc_issues WHERE id = %s", (issue_id,))
        conn.commit()
    logger.info("issue %s deleted by %s", issue_id, user["userCode"])
    return {"deleted": True, "id": issue_id}


@router.post("/{issue_id}/comments")
def add_comment(request: Request, issue_id: int, payload: NewComment):
    user = _me(request)
    body = _text(payload.body, "Comment", MAX_COMMENT, True)
    _ensure_schema()
    with _connection() as conn:
        cur = conn.cursor()
        issue_row = _get_issue(cur, issue_id)
        followers = _people_of_issue(cur, issue_row)   # taken BEFORE this comment is added
        cur.execute(
            "INSERT INTO qc_issue_comments (issue_id, kind, author_code, author_name, body, created_at) "
            "VALUES (%s, 'comment', %s, %s, %s, %s) RETURNING *",
            (issue_id, user["userCode"], _name(user), body, _now()),
        )
        row = cur.fetchone()
        cur.execute("UPDATE qc_issues SET updated_at = %s WHERE id = %s", (_now(), issue_id))
        if not is_team(user):
            followers |= team_codes()   # a non-QC person wrote: the QC team should see it too
        notify(cur, followers, "issue_comment", issue_id,
               f"{_name(user)} commented on \"{issue_row['title']}\"", user)
        conn.commit()
        return _comment_json(row, user)


@router.delete("/{issue_id}/comments/{comment_id}")
def delete_comment(request: Request, issue_id: int, comment_id: int):
    user = _me(request)
    _ensure_schema()
    with _connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM qc_issue_comments WHERE id = %s AND issue_id = %s", (comment_id, issue_id))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Comment not found.")
        if row["kind"] != "comment":
            raise HTTPException(status_code=403, detail="Status history cannot be deleted.")
        if row["author_code"] != user["userCode"] and not is_team(user):
            raise HTTPException(status_code=403, detail="You can only delete your own comments.")
        cur.execute("DELETE FROM qc_issue_comments WHERE id = %s", (comment_id,))
        conn.commit()
    return {"deleted": True, "id": comment_id}