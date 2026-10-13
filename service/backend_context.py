"""Backend source context fetcher.

Fetches C# backend files (BL/DL/Service) for a given screen from the
backend GitLab repo and appends them to the project_context source_files
so ProjectAnalysisAgent can use them for business logic coverage.
"""
import json
import logging
import os
import re
from pathlib import Path

from service.gitlab_service import GitLabService
from service.mm_html_filter import prune_html_for_class_id, squeeze_whitespace

logger = logging.getLogger(__name__)

# config/mm_screen_mapping.json, confirmed against dbo.MBIZTRANSACTIONCLASS
# on 2026-10-07. See that file's "_meta" block for provenance and the one
# unresolved case (Amendment has no single ClassId).
_MM_MAPPING_PATH = Path(__file__).resolve().parent.parent / "config" / "mm_screen_mapping.json"
_mm_mapping_cache: dict | None = None


def _load_mm_mapping() -> dict:
    global _mm_mapping_cache
    if _mm_mapping_cache is None:
        with open(_MM_MAPPING_PATH, "r", encoding="utf-8") as f:
            _mm_mapping_cache = json.load(f)
    return _mm_mapping_cache


def get_biz_transaction_class_id(module: str, screen_name: str) -> int | None:
    """Look up the confirmed BizTransactionClassId for a module + screen name.

    Returns None (not a raise) when the module or screen isn't in the
    mapping yet, so callers can fall back to name-based inference instead
    of hard-failing generation for a screen we just haven't confirmed yet.

    Matching is case-insensitive and whitespace-trimmed, since the mapping
    keys are lowercase BIZTRANSACTIONCLASSNAME values from the DB but
    callers may pass screen names in any casing (e.g. from menu labels).
    """
    mapping = _load_mm_mapping()
    module_map = mapping.get(module.lower().strip())
    if not module_map:
        logger.warning("get_biz_transaction_class_id: unknown module %r", module)
        return None

    key = screen_name.lower().strip()
    class_id = module_map.get(key)
    if class_id is None:
        logger.warning(
            "get_biz_transaction_class_id: no confirmed ClassId for module=%r screen=%r "
            "— falling back to name-based inference in the prompt",
            module, screen_name,
        )
    return class_id

# Max backend files to fetch per screen — keeps context budget safe
_MAX_BACKEND_FILES = 5

# C# backend file suffixes to look for
# Built-in defaults (no .env needed). "BLL.cs"/"DAL.cs" were added because the
# backend repo names its business-logic files like ActionBLL.cs.
_BACKEND_SUFFIXES = ("BL.cs", "DL.cs", "SL.cs", "Service.cs", "BLL.cs", "DAL.cs")
# Names the MM backend files are expected to contain (guess based on the API
# names seen in the frontend: /mms/MMHead.svc, MMBizTransactionType).
_MM_BACKEND_KEYWORDS = ("materialmanagement", "mmhead", "mmdetail", "mmbiztransactiontype")

# Modules that share a single materialmanagement component.
_MM_MODULES = {"sales", "purchase", "inventory", "jobwork", "subcontract"}

# Target filenames to search for dynamically in the source tree.
# Never hardcode paths — the repo structure may differ across environments.
# ORDER MATTERS: .ts files are listed first on purpose. GitLab's tree
# listing is alphabetical within a folder, so "...component.html" always
# sorts before "...component.ts" ('h' < 't') — if find_mm_source_files
# just followed that natural order, the HTML files would always win the
# char budget in _enforce_source_char_budget (which keeps whichever files
# come first until the limit is hit), and the .ts files — which is the
# ONLY place grid column fields like ItemCode/ItemName/SKUItemId exist at
# all, since this grid renders columns dynamically from a runtime array
# rather than static markup — would always be dropped. Confirmed in
# production logs on 2026-10-07: both .ts files dropped, both .html kept,
# resulting grid output had zero columns. find_mm_source_files below
# re-sorts its results to match THIS list's order, not the tree's.
_MM_TARGET_FILES_PRIORITY = [
    "materialmanagement.component.ts",
    "gbmaterialmanagementgrid.component.ts",
    "materialmanagement.component.html",
    "gbmaterialmanagementgrid.component.html",
]
_MM_TARGET_FILES = set(_MM_TARGET_FILES_PRIORITY)

# MM screens' own two .ts files alone can run well past the global
# _MAX_SOURCE_CHARS (150_000) that's sized for normal single-screen
# folders — these 4 files together describe EVERY screen in 5 modules,
# not just one. Use a higher ceiling for MM screens specifically rather
# than raising the global limit (which would risk context_length_exceeded
# on the normal, non-MM path this budget was originally sized for).
#
# 300_000 was tried first and still wasn't enough: prod logs 2026-10-07
# show materialmanagement.component.ts + gbmaterialmanagementgrid.component.ts
# together landing somewhere around ~340K+ chars on the live repo (larger
# than local snapshots used during development). Raised accordingly, but
# this is a soft ceiling now, not a hard one — see
# _enforce_mm_source_char_budget in app.py, which keeps both .ts files in
# full even if their combined size exceeds this value, and only drops
# .html. If MM .ts files keep growing, the real fix is not another bump
# here — gpt-4o-mini's 128K token context window is already not far off
# once the ~36K-char system prompt and required JSON output are counted.
# ANALYSIS_CLAUDE_MODEL (200K+ token context) was the originally intended
# fix for ProjectAnalysisAgent specifically (see handover Pending Issue #2
# — Anthropic workspace ID) and would remove this ceiling as a concern
# entirely for MM screens, rather than needing further char-budget tuning.
# Overridable from .env (MM_MAX_SOURCE_CHARS=...) so the budget can be raised
# when ANALYSIS_OPENAI_MODEL is switched to a larger-context model. Default is
# unchanged from before (380_000) so nothing changes until you set it.
MM_MAX_SOURCE_CHARS = int(os.getenv("MM_MAX_SOURCE_CHARS", "500000"))

# Set MM_SQUEEZE_WHITESPACE=0 to send the MM files exactly as stored.
_MM_SQUEEZE = os.getenv("MM_SQUEEZE_WHITESPACE", "1").strip() != "0"


def find_mm_source_files(source_tree: list[str]) -> list[str]:
    """Find materialmanagement component files dynamically from the source
    tree, matching by filename only (no hardcoded paths) — but returned in
    _MM_TARGET_FILES_PRIORITY's order (.ts first), NOT the tree's natural
    (alphabetical) order, so budget trimming drops .html before .ts."""
    found_by_filename: dict[str, str] = {}
    for path in source_tree:
        filename = path.rsplit("/", 1)[-1].lower()
        if filename in _MM_TARGET_FILES:
            found_by_filename[filename] = path

    ordered = []
    for filename in _MM_TARGET_FILES_PRIORITY:
        if filename in found_by_filename:
            ordered.append(found_by_filename[filename])
    return ordered

# Analysis extensions accepted by ProjectAnalysisAgent
_ANALYSIS_EXTENSIONS = {".ts", ".js", ".tsx", ".jsx", ".html", ".xml", ".yaml", ".yml", ".cs", ".java", ".py"}


def is_mm_module(module: str) -> bool:
    """Return True if the module uses the shared materialmanagement component."""
    return module.lower() in _MM_MODULES


def fetch_mm_source_context(
    src: GitLabService, module: str, screen_name: str, source_tree: list[str] | None = None,
) -> dict:
    """Fetch materialmanagement source files for MM module screens dynamically.

    These 5 modules (sales/purchase/inventory/jobwork/subcontract) share one
    component — no per-screen folder exists, so ArchitectureAgent lookup is
    skipped and these files are found by filename in the source tree.

    screen_name is passed as biz_transaction_screen so ProjectAnalysisAgent
    knows which BizTransactionType screen is being tested and can filter
    fields, grid columns and buttons accordingly. module + screen_name are
    also used to resolve the confirmed biz_transaction_class_id (see
    get_biz_transaction_class_id) and are passed through unfiltered as
    explicit_module/explicit_screen so the prompt doesn't have to infer
    navigation_path from folder structure (there isn't one, for MM screens).
    """
    def _derive_navigation_hint(primary_dir: str) -> list[str]:
        _NAV_STRIP_ROOTS = {"projects", "src", "app"}
        _NAV_STRIP_ORG = {"master", "transaction", "shared", "common", "module"}
        parts = primary_dir.replace("\\", "/").strip("/").split("/")
        return [p for p in parts if p and p not in _NAV_STRIP_ROOTS and p not in _NAV_STRIP_ORG]

    # Find correct paths dynamically from source tree
    paths_to_fetch = find_mm_source_files(source_tree or [])
    if not paths_to_fetch:
        logger.warning("Could not find MM source files in source tree — tree may not be available yet")

    fetched: dict[str, str] = {}
    for path in paths_to_fetch:
        file_content = src.fetch_file(path)
        if file_content is not None:
            fetched[path] = file_content
        else:
            logger.warning("MM source fetch returned None for path=%r", path)

    source_files = []
    for path, file_content in fetched.items():
        suffix = "." + path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""
        if suffix in _ANALYSIS_EXTENSIONS:
            source_files.append({"path": path, "language": suffix, "content": file_content})

    # Derive primary_dir from the first found .ts file path
    primary_dir = "materialmanagement"
    for path in fetched:
        if path.endswith("materialmanagement.component.ts"):
            primary_dir = path.rsplit("/", 1)[0]
            break

    logger.info("MM source context: found %d file(s) at primary_dir=%s", len(source_files), primary_dir)

    biz_transaction_class_id = get_biz_transaction_class_id(module, screen_name)

    # Shrink the shared files for THIS screen so the .html (which holds the
    # per-screen show/hide conditions and the locators) can fit in the budget.
    for sf in source_files:
        before = len(sf["content"])
        pruned_blocks = 0
        if sf["language"] == ".html":
            sf["content"], pstats = prune_html_for_class_id(sf["content"], biz_transaction_class_id)
            pruned_blocks = pstats["blocks_pruned"]
        if _MM_SQUEEZE and sf["language"] in (".html", ".ts"):
            sf["content"] = squeeze_whitespace(
                sf["content"], drop_full_line_slash_comments=(sf["language"] == ".ts"),
            )
        logger.info(
            "MM source size: %s | %d -> %d chars | class-id blocks pruned=%d",
            sf["path"].rsplit("/", 1)[-1], before, len(sf["content"]), pruned_blocks,
        )
    if biz_transaction_class_id is not None:
        logger.info(
            "MM source context: resolved biz_transaction_class_id=%s for module=%r screen=%r",
            biz_transaction_class_id, module, screen_name,
        )

    return {
        "folder_structure": list(fetched.keys()),
        "package_json": "",
        "requirements_txt": "",
        "readme": "",
        "source_files": source_files,
        "primary_dir": primary_dir,
        "navigation_hint": _derive_navigation_hint(primary_dir),
        "biz_transaction_screen": screen_name,
        "biz_transaction_class_id": biz_transaction_class_id,
        "explicit_module": module,
        "explicit_screen": screen_name,
    }


# Extra knobs (all optional, set in .env):
#   BACKEND_FILE_KEYWORDS=materialmanagement,biztransaction   extra path keywords
#   BACKEND_FILE_SUFFIXES=BL.cs,DL.cs,Service.cs              file endings to accept
#   BACKEND_MAX_FILE_CHARS=40000                              cap per backend file
_MIN_KEYWORD_LEN = 5
_MIN_BACKEND_CHARS = 5_000


def _env_list(name: str) -> list[str]:
    return [p.strip() for p in os.getenv(name, "").split(",") if p.strip()]


def fetch_backend_context(backend: GitLabService, screen_name: str, source_hints: list[str]) -> list[dict]:
    """Fetch backend BL/DL/Service files for the given screen.

    Uses the backend repo tree to find files matching the screen name or
    source hints (API route fragments extracted from the frontend source).
    Returns a list of source_file dicts to be appended to project_context.

    Always includes materialmanagement files since MM module screens all
    share that component on the backend too.

    Changes vs the original: suffix match is case-insensitive and configurable,
    the best matches (screen name first) are fetched instead of the first five
    in tree order, very short keywords no longer match everything, each file is
    size-capped, and the log now says WHY nothing matched.
    """
    try:
        backend_tree = backend.get_repo_tree()
    except Exception as e:
        logger.warning("Backend GitLab tree fetch failed: %s", e)
        return []

    suffixes = tuple(s.lower() for s in (_env_list("BACKEND_FILE_SUFFIXES") or _BACKEND_SUFFIXES))
    max_file_chars = int(os.getenv("BACKEND_MAX_FILE_CHARS", "40000"))

    screen_normalized = re.sub(r"[^a-z0-9]", "", screen_name.lower())

    hint_keywords: set[str] = {screen_normalized, *_MM_BACKEND_KEYWORDS}
    for extra in _env_list("BACKEND_FILE_KEYWORDS"):
        hint_keywords.add(re.sub(r"[^a-z0-9]", "", extra.lower()))
    for h in source_hints:
        hint_keywords.add(re.sub(r"[^a-z0-9]", "", h.lower()))
    # An empty / tiny keyword is a substring of every path -> would match all.
    hint_keywords = {k for k in hint_keywords if len(k) >= _MIN_KEYWORD_LEN}

    scored: list[tuple[int, str]] = []
    suffix_hits = 0
    for path in backend_tree:
        if not path.lower().endswith(suffixes):
            continue
        suffix_hits += 1
        path_normalized = re.sub(r"[^a-z0-9]", "", path.lower())
        if not any(kw in path_normalized for kw in hint_keywords):
            continue
        if screen_normalized and screen_normalized in path_normalized:
            score = 3
        elif any(k in path_normalized for k in _MM_BACKEND_KEYWORDS):
            score = 2
        else:
            score = 1
        scored.append((score, path))

    scored.sort(key=lambda sp: -sp[0])  # stable: keeps tree order within a score
    candidates = [p for _, p in scored][:_MAX_BACKEND_FILES]

    logger.info(
        "Backend context: tree=%d paths, %d end with %s, %d matched keywords, using %d",
        len(backend_tree), suffix_hits, list(suffixes), len(scored), len(candidates),
    )
    if not candidates:
        cs_sample = [
            p for p in backend_tree
            if p.lower().endswith(".cs") and re.search(r"mm|material|purchase|inventory", p.lower())
        ][:20]
        name_sample = [
            p for p in backend_tree
            if any(k in re.sub(r"[^a-z0-9]", "", p.lower()) for k in ("materialmanagement", "mmhead", screen_normalized) if k)
        ][:15]
        logger.warning(
            "Backend context: NO files matched. keywords=%s | .cs paths that look MM/purchase/inventory-related=%s | "
            "paths containing screen/materialmanagement/mmhead=%s",
            sorted(hint_keywords)[:12], cs_sample, name_sample,
        )
        return []

    backend_files = []
    for path in candidates:
        content = backend.fetch_file(path)
        if content is not None:
            if len(content) > max_file_chars:
                content = content[:max_file_chars] + "\n// [truncated: backend file too large]"
            backend_files.append({
                "path": f"[backend] {path}",
                "language": ".cs",
                "content": content,
            })
            logger.info("Backend file fetched: %s (%d chars)", path, len(content))
        else:
            logger.warning("Backend file fetch returned None for path=%r", path)

    return backend_files


def fit_backend_files_to_budget(project_context: dict, backend_files: list[dict],
                                limit: int | None = None) -> list[dict]:
    """Keep only as many backend chars as still fit under the MM budget AFTER
    the frontend files (frontend wins: it holds the fields/columns/locators).
    Files are truncated, or skipped when almost no room is left."""
    limit = limit or MM_MAX_SOURCE_CHARS
    used = sum(len(f.get("content") or "") for f in project_context.get("source_files", []) or [])
    remaining = limit - used
    kept: list[dict] = []
    for f in backend_files:
        content = f.get("content") or ""
        if remaining < _MIN_BACKEND_CHARS:
            logger.warning(
                "Backend context: no room left in the %d char budget (frontend uses ~%d) -- "
                "skipping %s. Raise MM_MAX_SOURCE_CHARS (and use a larger-context "
                "ANALYSIS_OPENAI_MODEL) to include backend files.", limit, used, f.get("path"),
            )
            continue
        if len(content) > remaining:
            content = content[:remaining] + "\n// [truncated to fit context budget]"
        kept.append({**f, "content": content})
        remaining -= len(content)
    return kept