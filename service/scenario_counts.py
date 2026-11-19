from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

logger = logging.getLogger(__name__)

TTL_SECONDS = 3600
WORKERS = 6
_SCENARIO_RE = re.compile(r"^\s*Scenario(?: Outline)?:", re.M)

_lock = threading.Lock()
_counts: dict[str, tuple[int | None, float]] = {}   # "module|screen" -> (count or None when unreadable, read at)
_job = {"running": False, "done": 0, "total": 0}


def key(module: str, screen: str) -> str:
    return f"{module}|{screen}"


def count_scenarios(text: str) -> int:
    return len(_SCENARIO_RE.findall(text or ""))


def _run(gl_factory: Callable, todo: list[tuple[str, str]]) -> None:
    local = threading.local()

    def work(item: tuple[str, str]) -> None:
        k, path = item
        n = None
        try:
            if not hasattr(local, "gl"):
                local.gl = gl_factory()
            text = local.gl.fetch_file(path)
            n = count_scenarios(text) if text is not None else None
        except Exception as e:                      # one unreadable file must not stop the rest
            logger.warning("could not count scenarios in %s: %s", path, e)
        with _lock:
            _counts[k] = (n, time.time())           # None is kept too, so a bad file is not retried on every poll
            _job["done"] += 1

    try:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            list(pool.map(work, todo))
    except Exception:
        logger.exception("scenario count job stopped unexpectedly")
    finally:
        with _lock:
            _job["running"] = False
        logger.info("scenario counts: %d screen(s) read", len(todo))


def snapshot(gl_factory: Callable, paths: dict[tuple[str, str], str], force: bool = False) -> dict:
    """{"counts": {"module|screen": n}, "status": "loading" | "ready", "done": n, "total": n}.
    paths: {(module, screen): feature file path} of the current QC catalog."""
    now = time.time()
    with _lock:
        valid = {key(m, s) for (m, s) in paths}
        for k in [k for k in _counts if k not in valid]:
            del _counts[k]                          # screen no longer in the repo
        if not _job["running"]:
            todo = [
                (key(m, s), p) for (m, s), p in paths.items()
                if force or key(m, s) not in _counts or now - _counts[key(m, s)][1] > TTL_SECONDS
            ]
            if todo:
                _job.update(running=True, done=0, total=len(todo))
                threading.Thread(target=_run, args=(gl_factory, todo), daemon=True, name="scenario-counts").start()
        return {
            "counts": {k: v[0] for k, v in _counts.items() if v[0] is not None},
            "status": "loading" if _job["running"] else "ready",
            "done": _job["done"],
            "total": _job["total"],
        }