"""Live module -> screens list, read from the QC repo tree (the same tree Fetch / Overnight Run use).

The tree is read once and the result is cached for about 5 minutes (force=True re-reads it).
get_catalog() is blocking (GitLab) - call it through asyncio.to_thread, or use get_catalog_async().
A module is a folder directly under Regression-Testing/ (for example Skill_Management_Module is shown as
"Skill Management"); its screens are every folder below it that has both a .feature and a script - the same
matching rule resolve_module_screens_exact() applies, so a module picked from this list always fetches.
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Callable

from service.architecture_resolver import (
    LEGACY_REGRESSION_TESTING_PREFIX, REGRESSION_TESTING_PREFIX,
    _group_screen_folders, _normalize_module_text, _searchable_tree,
)

logger = logging.getLogger(__name__)

CACHE_SECONDS = 300
_lock = threading.Lock()
_async_lock: asyncio.Lock | None = None
_cache: dict = {"at": 0.0, "data": None}


def module_label(folder: str) -> str:
    """'Skill_Management_Module' -> 'Skill Management'."""
    name = re.sub(r"[_\-]+", " ", folder).strip()
    name = re.sub(r"\s+Module$", "", name, flags=re.I).strip()
    return name or folder


def build_catalog(tree: list[str]) -> list[dict]:
    """[{"module": "Skill Management", "screens": ["Skill Domain", ...]}] sorted by name."""
    prefix = ""
    scoped = _searchable_tree(tree)
    if scoped and scoped[0].startswith(REGRESSION_TESTING_PREFIX):
        prefix = REGRESSION_TESTING_PREFIX
    elif scoped and scoped[0].startswith(LEGACY_REGRESSION_TESTING_PREFIX):
        prefix = LEGACY_REGRESSION_TESTING_PREFIX
    folders = _group_screen_folders(scoped)

    first: dict[str, str] = {}                     # module label -> the module folder name
    for directory in folders:
        rest = directory[len(prefix):] if prefix and directory.startswith(prefix) else directory
        parts = [p for p in rest.split("/") if p]
        if len(parts) < 2:
            continue
        first.setdefault(module_label(parts[0]), parts[0])

    result = []
    for label in sorted(first, key=str.lower):
        norm = _normalize_module_text(label)
        names: dict[str, None] = {}
        for directory in sorted(folders):
            parts = directory.split("/")
            if any(_normalize_module_text(p) == norm for p in parts[:-1]):
                names.setdefault(parts[-1], None)
        if names:
            result.append({"module": label, "screens": sorted(names, key=str.lower)})
    return result


def get_catalog(gl_factory: Callable, force: bool = False) -> dict:
    """{"modules": [...], "generated_at": iso, "cached": bool}. Raises on a GitLab failure (nothing cached then)."""
    with _lock:
        now = time.time()
        if not force and _cache["data"] is not None and now - _cache["at"] < CACHE_SECONDS:
            return {**_cache["data"], "cached": True}
        gl = gl_factory()
        tree = gl.get_repo_tree(force_refresh=True)
        data = {
            "modules": build_catalog(tree),
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        _cache["at"], _cache["data"] = now, data
        logger.info("repo catalog: %d module(s), %d screen(s)", len(data["modules"]),
                    sum(len(m["screens"]) for m in data["modules"]))
        return {**data, "cached": False}


async def get_catalog_async(gl_factory: Callable, force: bool = False) -> dict:
    global _async_lock
    if _async_lock is None:
        _async_lock = asyncio.Lock()
    async with _async_lock:        # two browsers opening at once share one GitLab read
        return await asyncio.to_thread(get_catalog, gl_factory, force)