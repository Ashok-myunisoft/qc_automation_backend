from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from fastapi import APIRouter, HTTPException, Request

from service import history_store
from service.discussion_service import _iso, _me, _now
from service.store_db import connection as _connection

logger = logging.getLogger(__name__)

PENDING_TTL = 12 * 3600       # a generated-but-never-pushed fingerprint is forgotten after this
STATE_TTL = 30 * 60           # the Tracking page re-checks a screen after this
WORKERS = 6
MAX_FILES = 80                # fingerprint at most this many files of one screen

_gl_factory: Callable | None = None


def configure(gl_factory: Callable) -> None:
    """app.py gives the function that creates a source-repo GitLab client."""
    global _gl_factory
    _gl_factory = gl_factory


def _new_gl():
    if _gl_factory is None:
        raise RuntimeError("source repository is not configured")
    return _gl_factory()


def _skey(name) -> str:
    """Loose screen key: 'Admin_Module' == 'admin', and a leading order number ('2_Activity') is ignored."""
    return re.sub(r"^\d+\s+", "", history_store.track_key(name))


def _hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", "replace")).hexdigest()


# ------------------------------------------------------------------ table

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
                CREATE TABLE IF NOT EXISTS screen_sources (
                    id BIGSERIAL PRIMARY KEY,
                    module_key VARCHAR(255) NOT NULL,
                    screen_key VARCHAR(255) NOT NULL,
                    module VARCHAR(255) NOT NULL,
                    screen VARCHAR(255) NOT NULL,
                    files_json TEXT NOT NULL,
                    recorded_by_code VARCHAR(64) NULL,
                    recorded_by_name VARCHAR(255) NULL,
                    recorded_at TIMESTAMP NOT NULL,
                    UNIQUE (module_key, screen_key)
                )
            """)
            conn.commit()
        _SCHEMA_READY = True


# ------------------------------------------------------------------ remember at generation, save at push

_pending: dict[tuple[str, str], dict] = {}
_pending_lock = threading.Lock()


def remember_pending(module: str, screen: str, source_files: list[dict]) -> None:
    """Called while generating: fingerprint of the source files that were read. Never raises."""
    try:
        files = {}
        for f in (source_files or [])[:MAX_FILES]:
            path = f.get("path")
            if path:
                files[path] = _hash(f.get("content") or "")
        if not files:
            return
        now = time.time()
        with _pending_lock:
            for k in [k for k, v in _pending.items() if now - v["at"] > PENDING_TTL]:
                del _pending[k]
            _pending[(history_store.track_key(module), _skey(screen))] = {"files": files, "at": now}
    except Exception as e:  # noqa: BLE001
        logger.warning("could not fingerprint the source of %s/%s: %s", module, screen, e)


def commit_pending(pairs: list[tuple[str, str]], user: dict | None) -> bool:
    """Called when the tests are pushed: save the pending fingerprint of the first (module, screen) that has one."""
    try:
        for module, screen in pairs:
            if not module or not screen:
                continue
            key = (history_store.track_key(module), _skey(screen))
            with _pending_lock:
                item = _pending.pop(key, None)
            if not item:
                continue
            _ensure_schema()
            user = user or {}
            with _connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO screen_sources (module_key, screen_key, module, screen, files_json, recorded_by_code, recorded_by_name, recorded_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (module_key, screen_key) DO UPDATE SET module = EXCLUDED.module, screen = EXCLUDED.screen, "
                    "files_json = EXCLUDED.files_json, recorded_by_code = EXCLUDED.recorded_by_code, "
                    "recorded_by_name = EXCLUDED.recorded_by_name, recorded_at = EXCLUDED.recorded_at",
                    (key[0], key[1], module, screen, json.dumps(item["files"]), user.get("userCode"),
                     user.get("userName") or user.get("userCode"), _now()),
                )
                conn.commit()
            with _lock:
                _states.pop(key, None)        # the next Tracking refresh checks it again (it is "unchanged" now)
            return True
    except Exception as e:  # noqa: BLE001
        logger.warning("could not save the source fingerprint: %s", e)
    return False


# ------------------------------------------------------------------ the check

def _load_record(mk: str, sk: str) -> dict | None:
    _ensure_schema()
    with _connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM screen_sources WHERE module_key = %s AND screen_key = %s", (mk, sk))
        return cur.fetchone()


def _compare(gl, record: dict) -> dict:
    saved: dict = json.loads(record["files_json"] or "{}")
    changed: list[str] = []
    for path, old_hash in saved.items():
        text = gl.fetch_file(path)                # None = the file no longer exists
        if text is None or _hash(text) != old_hash:
            changed.append(path)
    base = {
        "recorded_at": _iso(record["recorded_at"]),
        "recorded_by": record["recorded_by_name"] or record["recorded_by_code"] or "",
        "checked": len(saved),
        "changed_count": len(changed),
        "changed_files": changed[:10],
    }
    return {"state": "changed" if changed else "unchanged", **base}


def check(module: str, screen: str, gl=None) -> dict:
    """Exact check of one screen. Never raises: a problem comes back as state 'unknown' with a reason."""
    try:
        record = _load_record(history_store.track_key(module), _skey(screen))
        if record is None:
            return {"state": "unknown", "reason": "not tracked yet - tracking starts with the next generation of this screen"}
        return _compare(gl or _new_gl(), record)
    except Exception as e:  # noqa: BLE001
        logger.warning("source check failed for %s/%s: %s", module, screen, e)
        return {"state": "unknown", "reason": "the source repository could not be reached"}


# ------------------------------------------------------------------ every tracked screen, in the background

_lock = threading.Lock()
_states: dict[tuple[str, str], tuple[dict, float]] = {}
_job = {"running": False, "done": 0, "total": 0}


def _run(todo: list[dict]) -> None:
    local = threading.local()

    def work(record: dict) -> None:
        key = (record["module_key"], record["screen_key"])
        try:
            if not hasattr(local, "gl"):
                local.gl = _new_gl()
            state = _compare(local.gl, record)
        except Exception as e:  # noqa: BLE001
            logger.warning("could not check the source of %s/%s: %s", record["module"], record["screen"], e)
            state = {"state": "unknown", "reason": "the source repository could not be reached"}
        with _lock:
            _states[key] = (state, time.time())
            _job["done"] += 1

    try:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            list(pool.map(work, todo))
    except Exception:
        logger.exception("source status job stopped unexpectedly")
    finally:
        with _lock:
            _job["running"] = False


def snapshot(force: bool = False) -> dict:
    _ensure_schema()
    with _connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM screen_sources")
        records = cur.fetchall()
    now = time.time()
    with _lock:
        if not _job["running"]:
            todo = [r for r in records
                    if force or (r["module_key"], r["screen_key"]) not in _states
                    or now - _states[(r["module_key"], r["screen_key"])][1] > STATE_TTL]
            if todo:
                _job.update(running=True, done=0, total=len(todo))
                threading.Thread(target=_run, args=(todo,), daemon=True, name="source-status").start()
        items = {}
        for r in records:
            key = (r["module_key"], r["screen_key"])
            cached = _states.get(key)
            items[f"{key[0]}|{key[1]}"] = cached[0] if cached else {
                "state": "checking", "recorded_at": _iso(r["recorded_at"]), "recorded_by": r["recorded_by_name"] or "",
            }
        return {"items": items, "status": "loading" if _job["running"] else "ready", "done": _job["done"], "total": _job["total"]}


# ------------------------------------------------------------------ routes

router = APIRouter(prefix="/api")


@router.get("/source-check")
def api_source_check(request: Request, module: str, screen: str):
    _me(request)
    if not module.strip() or not screen.strip():
        raise HTTPException(status_code=400, detail="Module and screen are required.")
    return check(module.strip(), screen.strip())


@router.get("/source-status")
def api_source_status(request: Request, refresh: bool = False):
    _me(request)
    try:
        return snapshot(force=refresh)
    except Exception as e:  # noqa: BLE001
        logger.exception("could not read the source status")
        raise HTTPException(status_code=502, detail=f"could not read the source status: {e}")