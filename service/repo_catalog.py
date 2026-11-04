"""Live module -> screens list, read from the QC repo tree (the same tree Fetch / Overnight Run use).

The tree is read once and the result is cached for about 5 minutes (force=True re-reads it).
get_catalog() is blocking (GitLab) - call it through asyncio.to_thread, or use get_catalog_async().
A module is a folder directly under Regression-Testing/ (for example Skill_Management_Module is shown as
"Skill Management"); its screens are every folder below it that has both a .feature and a script - the same
matching rule resolve_module_screens_exact() applies, so a module picked from this list always fetches.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from service.architecture_resolver import (
    LEGACY_REGRESSION_TESTING_PREFIX, REGRESSION_TESTING_PREFIX,
    _group_screen_folders, _normalize_module_text, _searchable_tree,
)

logger = logging.getLogger(__name__)

CACHE_SECONDS = 300
_lock = threading.Lock()
_async_lock: asyncio.Lock | None = None
_cache: dict = {"at": 0.0, "data": None, "paths": {}}   # "paths": {(module, screen): feature file path}


def module_label(folder: str) -> str:
    """'Skill_Management_Module' -> 'Skill Management'."""
    name = re.sub(r"[_\-]+", " ", folder).strip()
    name = re.sub(r"\s+Module$", "", name, flags=re.I).strip()
    return name or folder


def build_catalog(tree: list[str], paths_out: dict | None = None) -> list[dict]:
    """[{"module": "Skill Management", "screens": ["Skill Domain", ...]}] sorted by name.
    paths_out, when given, is filled with {(module, screen): feature file path} (used for scenario counts)."""
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
                if paths_out is not None:
                    paths_out.setdefault((label, parts[-1]), folders[directory]["feature"])
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
        paths: dict = {}
        data = {
            "modules": build_catalog(tree, paths),
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        _cache["at"], _cache["data"], _cache["paths"] = now, data, paths
        logger.info("repo catalog: %d module(s), %d screen(s)", len(data["modules"]),
                    sum(len(m["screens"]) for m in data["modules"]))
        return {**data, "cached": False}


def feature_paths() -> dict:
    """{(module, screen): feature file path} of the last QC catalog that was read."""
    return dict(_cache.get("paths") or {})


async def get_catalog_async(gl_factory: Callable, force: bool = False) -> dict:
    global _async_lock
    if _async_lock is None:
        _async_lock = asyncio.Lock()
    async with _async_lock:        # two browsers opening at once share one GitLab read
        return await asyncio.to_thread(get_catalog, gl_factory, force)


# ----------------------------------------------------------------------
# SOURCE catalog - the module -> screens list read from the SOURCE (Angular) repo.
# Used by Generate / Auto generate: a screen can be generated before it has any test in the QC repo,
# so the QC list above (only screens that already have a feature + script) would hide exactly the
# screens that need generating.
#
# A source screen is a folder that directly holds a <name>.component.html AND a <name>.component.ts
# (the same rule prompts/architecture_prompt.txt gives the architecture agent), sitting under
# projects/<project>/.  The project folder is the module.  Supporting folders (service, model,
# shared, dialogs ...) are skipped.  The five Material-Management modules have no per-screen
# folder (they share one component), so their screens come from config/mm_screen_mapping.json,
# exactly as the generate flow treats them.
# ----------------------------------------------------------------------
SOURCE_PROJECT_RE = re.compile(r"(?:^|/)projects/([^/]+)/")
# projects that are the app shell / host, not a module with screens of its own (edit if needed)
SOURCE_EXCLUDED_PROJECTS = {"gbhost"}
# folder names that hold supporting code, never a screen of their own
SOURCE_SKIP_SEGMENTS = {
    "shared", "common", "service", "services", "model", "models", "directive", "directives",
    "pipe", "pipes", "dialog", "dialogs", "popup", "popups", "assets", "public", "node_modules",
    "environments", "guards", "interceptors",
}
_MM_MAPPING_PATH = Path(__file__).resolve().parent.parent / "config" / "mm_screen_mapping.json"

_source_cache: dict = {"at": 0.0, "data": None}
_source_lock = threading.Lock()
_source_async_lock: asyncio.Lock | None = None


def _humanize(folder: str) -> str:
    """'target-master' -> 'Target Master'."""
    words = [w for w in re.split(r"[\s_\-]+", folder.strip()) if w]
    return " ".join(w[:1].upper() + w[1:] for w in words) or folder


def _mm_modules() -> dict[str, list[str]]:
    """{'Sales': ['Proforma Invoice', ...], ...} from config/mm_screen_mapping.json."""
    try:
        with open(_MM_MAPPING_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning("could not read the MM screen mapping: %s", e)
        return {}
    out: dict[str, list[str]] = {}
    for key, screens in data.items():
        if key.startswith("_") or not isinstance(screens, dict):
            continue
        out[key.strip().capitalize()] = sorted({_humanize(n) for n in screens}, key=str.lower)
    return out


def build_source_catalog(tree: list[str], qc_modules: list[dict] | None = None) -> list[dict]:
    """[{"module": "Inventory", "screens": ["Item Master", ...]}] sorted by name.

    qc_modules (the QC catalog, when already cached) is only used to reuse its module / screen
    spelling for names that match, so a generated screen lands next to the existing tests."""
    qc_module_label: dict[str, str] = {}
    qc_screen_label: dict[tuple[str, str], str] = {}
    for m in qc_modules or []:
        mk = _normalize_module_text(m["module"])
        qc_module_label[mk] = m["module"]
        for sc in m.get("screens") or []:
            qc_screen_label[(mk, re.sub(r"[^a-z0-9]", "", sc.lower()))] = sc

    files_by_dir: dict[str, set[str]] = {}
    for path in tree:
        if "/" not in path:
            continue
        directory, filename = path.rsplit("/", 1)
        if filename.endswith(".component.html") or filename.endswith(".component.ts"):
            files_by_dir.setdefault(directory, set()).add(filename.rsplit(".", 1)[-1])

    mm = _mm_modules()
    mm_keys = {_normalize_module_text(k) for k in mm}

    modules: dict[str, dict[str, str]] = {}        # normalised module -> {normalised screen: label}
    labels: dict[str, str] = {}
    for directory, exts in files_by_dir.items():
        if not {"html", "ts"} <= exts:             # needs BOTH the markup and the component class
            continue
        m = SOURCE_PROJECT_RE.search(directory + "/")
        if not m or m.group(1).lower() in SOURCE_EXCLUDED_PROJECTS:
            continue
        rest = (directory + "/")[m.end():].strip("/").split("/")   # folders below projects/<project>/
        rest = [r for r in rest if r]
        if not rest or any(p.lower() in SOURCE_SKIP_SEGMENTS for p in rest):
            continue
        mkey = _normalize_module_text(m.group(1))
        if not mkey or mkey in mm_keys:             # MM modules are taken from the mapping instead
            continue
        labels.setdefault(mkey, qc_module_label.get(mkey) or _humanize(module_label(m.group(1))))
        skey = re.sub(r"[^a-z0-9]", "", rest[-1].lower())
        modules.setdefault(mkey, {}).setdefault(skey, qc_screen_label.get((mkey, skey)) or _humanize(rest[-1]))

    result = [{"module": labels[k], "screens": sorted(v.values(), key=str.lower)} for k, v in modules.items() if v]
    for label, screens in mm.items():
        mkey = _normalize_module_text(label)
        result.append({"module": qc_module_label.get(mkey, label), "screens": screens})
    result.sort(key=lambda r: r["module"].lower())
    return result


def get_source_catalog(gl_factory: Callable, force: bool = False) -> dict:
    """Same shape as get_catalog(), read from the source repo. Raises on a GitLab failure."""
    with _source_lock:
        now = time.time()
        if not force and _source_cache["data"] is not None and now - _source_cache["at"] < CACHE_SECONDS:
            return {**_source_cache["data"], "cached": True}
        gl = gl_factory()
        tree = gl.get_repo_tree(force_refresh=force)   # the shared source instance keeps its tree for an hour
        qc_data = _cache["data"]                   # only reuse the QC names if that list is already cached
        data = {
            "modules": build_source_catalog(tree, qc_data["modules"] if qc_data else None),
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        _source_cache["at"], _source_cache["data"] = now, data
        logger.info("source catalog: %d module(s), %d screen(s)", len(data["modules"]),
                    sum(len(m["screens"]) for m in data["modules"]))
        return {**data, "cached": False}


async def get_source_catalog_async(gl_factory: Callable, force: bool = False) -> dict:
    global _source_async_lock
    if _source_async_lock is None:
        _source_async_lock = asyncio.Lock()
    async with _source_async_lock:
        return await asyncio.to_thread(get_source_catalog, gl_factory, force)