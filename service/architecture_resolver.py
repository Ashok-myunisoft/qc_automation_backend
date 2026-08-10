import logging
import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from Agents.architecture_agent import ArchitectureAgent

logger = logging.getLogger(__name__)

_architecture_agent = ArchitectureAgent()

MIN_CONFIDENCE = 0.55
MIN_MODULE_CONFIDENCE = 0.5
MIN_AGENT_CONFIDENCE = 0.6
AMBIGUITY_MARGIN = 0.08
MODULE_HIT_THRESHOLD = 0.75

_MODULE_STOPWORDS = {"module", "the", "and"}

REGRESSION_TESTING_PREFIX = "cypress/src/features/Regression-Testing/"


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _normalize_module_text(text: str) -> str:
    words = re.split(r"[^a-zA-Z0-9]+", text.strip().lower())
    return "".join(w for w in words if w and w not in _MODULE_STOPWORDS)


def _pascal(text: str) -> str:
    words = re.split(r"[\s_-]+", text.strip())
    return "".join(w[:1].upper() + w[1:] for w in words if w)


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _searchable_tree(tree: list[str]) -> list[str]:
    scoped = [p for p in tree if p.startswith(REGRESSION_TESTING_PREFIX)]
    return scoped if scoped else tree


@dataclass
class ResolvedFeature:
    dir: str
    feature_path: str
    script_path: str
    slug: str
    confidence: float
    ambiguous: bool
    resolved_by: str = "fuzzy"


def _group_screen_folders(tree: list[str]) -> dict[str, dict]:
    """Every folder containing >=1 .feature file, with the .feature path and
    every candidate script path (.js/.cy.js, excluding the feature itself)
    directly inside it."""
    folders: dict[str, dict] = {}
    for path in tree:
        if "/" not in path:
            continue
        directory, filename = path.rsplit("/", 1)
        if filename.endswith(".feature"):
            folders.setdefault(directory, {"feature": None, "scripts": []})
            folders[directory]["feature"] = path
        elif filename.endswith(".cy.js") or filename.endswith(".js"):
            folders.setdefault(directory, {"feature": None, "scripts": []})
            folders[directory]["scripts"].append(path)

    return {d: v for d, v in folders.items() if v["feature"] is not None and v["scripts"]}


def _pick_script(scripts: list[str], screen: str) -> str:
    """Most screens have exactly one script in their folder. If there's ever
    more than one, pick the one whose filename best matches the screen name."""
    if len(scripts) == 1:
        return scripts[0]
    screen_norm = _normalize(screen)
    scored = sorted(
        scripts,
        key=lambda p: _similarity(_normalize(p.rsplit("/", 1)[-1].rsplit(".", 1)[0]), screen_norm),
        reverse=True,
    )
    return scored[0]


def resolve_existing(tree: list[str], module: str, screen: str) -> ResolvedFeature | None:
    """Fuzzy matcher: finds the screen folder whose path contains the given
    module (anywhere in the path) and whose folder name / feature filename
    best matches the given screen — independent of folder depth."""
    module_norm = _normalize_module_text(module)
    screen_norm = _normalize(screen)

    scored = []
    for directory, info in _group_screen_folders(_searchable_tree(tree)).items():
        parts = directory.split("/")
        module_hit = any(_similarity(_normalize_module_text(p), module_norm) > MODULE_HIT_THRESHOLD for p in parts)
        if not module_hit:
            continue

        screen_folder = parts[-1]
        feature_stem = info["feature"].rsplit("/", 1)[-1][: -len(".feature")]
        score = max(
            _similarity(_normalize(screen_folder), screen_norm),
            _similarity(_normalize(feature_stem), screen_norm),
        )
        scored.append((score, directory, info))

    if not scored:
        return None

    scored.sort(key=lambda t: t[0], reverse=True)
    best_score, best_dir, best_info = scored[0]
    if best_score < MIN_CONFIDENCE:
        return None

    ambiguous = len(scored) > 1 and (best_score - scored[1][0]) < AMBIGUITY_MARGIN
    script_path = _pick_script(best_info["scripts"], screen)

    return ResolvedFeature(
        dir=best_dir,
        feature_path=best_info["feature"],
        script_path=script_path,
        slug=best_dir.rsplit("/", 1)[-1],
        confidence=round(best_score, 3),
        ambiguous=ambiguous,
    )


def resolve_module_screens(tree: list[str], module: str) -> list[ResolvedFeature]:
    """Every screen folder anywhere under a path matching the given module."""
    module_norm = _normalize_module_text(module)
    results = []
    for directory, info in _group_screen_folders(_searchable_tree(tree)).items():
        parts = directory.split("/")
        module_hit = any(_similarity(_normalize_module_text(p), module_norm) > MODULE_HIT_THRESHOLD for p in parts)
        if not module_hit:
            continue

        screen_name = directory.rsplit("/", 1)[-1]
        results.append(ResolvedFeature(
            dir=directory,
            feature_path=info["feature"],
            script_path=_pick_script(info["scripts"], screen_name),
            slug=screen_name,
            confidence=1.0,
            ambiguous=False,
        ))

    results.sort(key=lambda r: r.dir)
    return results


def _valid_feature_candidate(tree_set: set[str], directory: str, feature_path: str, script_path: str) -> bool:
    if not directory or not feature_path or not script_path:
        return False
    if feature_path not in tree_set or script_path not in tree_set:
        return False
    if feature_path.rsplit("/", 1)[0] != directory:
        return False
    if script_path.rsplit("/", 1)[0] != directory:
        return False
    if not feature_path.endswith(".feature"):
        return False
    if script_path == feature_path:
        return False
    if not (script_path.endswith(".js") or script_path.endswith(".cy.js")):
        return False
    return True


def _feature_from_agent_result(result: dict) -> ResolvedFeature | None:
    directory    = result.get("dir")
    feature_path = result.get("feature_path")
    script_path  = result.get("script_path")
    confidence   = result.get("confidence")

    if not isinstance(confidence, (int, float)) or confidence < MIN_AGENT_CONFIDENCE:
        return None

    return ResolvedFeature(
        dir=directory,
        feature_path=feature_path,
        script_path=script_path,
        slug=directory.rsplit("/", 1)[-1],
        confidence=round(float(confidence), 3),
        ambiguous=bool(result.get("ambiguous", False)),
        resolved_by="agent",
    )


async def resolve_existing_precise(tree: list[str], module: str, screen: str) -> ResolvedFeature | None:
    result = await _architecture_agent.resolve_screen(tree, module, screen, repo_kind="test")

    if "error" not in result:
        tree_set = set(tree)
        if _valid_feature_candidate(tree_set, result.get("dir"), result.get("feature_path"), result.get("script_path")):
            resolved = _feature_from_agent_result(result)
            if resolved is not None:
                logger.info(
                    "architecture agent resolved module=%r screen=%r -> feature_path=%r script_path=%r confidence=%.3f ambiguous=%s",
                    module, screen, resolved.feature_path, resolved.script_path, resolved.confidence, resolved.ambiguous,
                )
                return resolved

    logger.info("architecture agent unusable for %s/%s (%r) — falling back to fuzzy matcher", module, screen, result)
    return resolve_existing(tree, module, screen)


async def resolve_module_screens_precise(tree: list[str], module: str) -> list[ResolvedFeature]:
    result = await _architecture_agent.resolve_module(tree, module, repo_kind="test")

    if "error" not in result and isinstance(result.get("screens"), list) and result["screens"]:
        tree_set = set(tree)
        resolved_list: list[ResolvedFeature] = []
        for item in result["screens"]:
            if not _valid_feature_candidate(tree_set, item.get("dir"), item.get("feature_path"), item.get("script_path")):
                resolved_list = []
                break
            feature = _feature_from_agent_result(item)
            if feature is None:
                resolved_list = []
                break
            resolved_list.append(feature)

        if resolved_list:
            resolved_list.sort(key=lambda r: r.feature_path)
            logger.info(
                "architecture agent resolved module=%r -> %d screen(s): %s",
                module, len(resolved_list), [r.feature_path for r in resolved_list],
            )
            return resolved_list

    logger.info("architecture agent unusable for module %s (%r) — falling back to fuzzy matcher", module, result)
    return resolve_module_screens(tree, module)


def build_new_path(module: str, screen: str) -> ResolvedFeature:
    """No existing match — construct a path for a brand new screen, per-screen
    folder with its own self-contained feature + script."""
    module_folder = f"{_pascal(module)}_Module"
    screen_name = _pascal(screen)
    directory = f"cypress/src/features/Regression-Testing/{module_folder}/{screen_name}"

    return ResolvedFeature(
        dir=directory,
        feature_path=f"{directory}/{screen_name}.feature",
        script_path=f"{directory}/{screen_name}.js",
        slug=screen_name,
        confidence=1.0,
        ambiguous=False,
    )


@dataclass
class ResolvedSource:
    dir: str
    files: list[str]
    confidence: float
    ambiguous: bool
    resolved_by: str = "fuzzy"


def _group_by_dir(tree: list[str]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for path in tree:
        if "/" not in path:
            continue
        directory, filename = path.rsplit("/", 1)
        groups.setdefault(directory, []).append(filename)
    return groups


CROSS_MODULE_MARGIN = 0.15


def _best_by_screen_name_only(tree: list[str], screen: str) -> tuple[float, str, list[str]] | None:
    """Ignores the module entirely — scores every directory in the tree by
    how well its LAST segment matches the screen name. Exists because some
    real repos implement a module's screen inside a *different* project
    folder (e.g. an ESS screen whose actual source lives under hrms/, since
    HRMS owns the underlying employee data and ESS just surfaces it) — a
    module-scoped search can never find that, no matter how good the
    screen-name matching is, because it never even looks at that path."""
    screen_norm = _normalize(screen)
    scored = []
    for directory, files in _group_by_dir(tree).items():
        if not files:
            continue
        screen_folder = directory.rsplit("/", 1)[-1]
        score = _similarity(_normalize(screen_folder), screen_norm)
        scored.append((score, directory, files))
    if not scored:
        return None
    scored.sort(key=lambda t: t[0], reverse=True)
    return scored[0]


def resolve_source_screen(tree: list[str], module: str, screen: str) -> ResolvedSource | None:
    module_norm = _normalize(module)
    screen_norm = _normalize(screen)

    scored: list[tuple[float, str, list[str]]] = []
    for directory, files in _group_by_dir(tree).items():
        parts = directory.split("/")
        module_hit = any(_similarity(_normalize(p), module_norm) > MODULE_HIT_THRESHOLD for p in parts)
        if not module_hit or not files:
            continue
        screen_folder = parts[-1]
        score = _similarity(_normalize(screen_folder), screen_norm)
        scored.append((score, directory, files))

    scored.sort(key=lambda t: t[0], reverse=True)
    module_scoped_best = scored[0] if scored else None

    cross_module_best = _best_by_screen_name_only(tree, screen)

    if cross_module_best and (
        module_scoped_best is None
        or cross_module_best[0] - module_scoped_best[0] >= CROSS_MODULE_MARGIN
    ):
        best_score, best_dir, best_files = cross_module_best
        if best_score < MIN_CONFIDENCE:
            return None
        logger.info(
            "resolve_source_screen: cross-module match for module=%r screen=%r -> %r "
            "(score %.3f beat module-scoped %s by >= %.2f)",
            module, screen, best_dir, best_score,
            f"{module_scoped_best[0]:.3f}" if module_scoped_best else "None",
            CROSS_MODULE_MARGIN,
        )
        return ResolvedSource(
            dir=best_dir, files=best_files, confidence=round(best_score, 3),
            ambiguous=True,
            resolved_by="fuzzy-cross-module",
        )

    if module_scoped_best is None:
        return None

    best_score, best_dir, best_files = module_scoped_best
    if best_score < MIN_CONFIDENCE:
        return None

    ambiguous = len(scored) > 1 and (best_score - scored[1][0]) < AMBIGUITY_MARGIN

    return ResolvedSource(
        dir=best_dir,
        files=best_files,
        confidence=round(best_score, 3),
        ambiguous=ambiguous,
    )


def resolve_source_module_screens(tree: list[str], module: str) -> list[ResolvedSource]:
    module_norm = _normalize(module)
    results: list[ResolvedSource] = []
    for directory, files in _group_by_dir(tree).items():
        parts = directory.split("/")
        module_hit = any(_similarity(_normalize(p), module_norm) > MODULE_HIT_THRESHOLD for p in parts)
        if not module_hit or not files:
            continue
        results.append(ResolvedSource(dir=directory, files=files, confidence=1.0, ambiguous=False))

    results.sort(key=lambda r: r.dir)
    return results


def _source_from_agent_result(dirs_to_files: dict[str, list[str]], result: dict) -> ResolvedSource | None:
    directory  = result.get("dir")
    confidence = result.get("confidence")

    if not directory or directory not in dirs_to_files:
        return None
    if not isinstance(confidence, (int, float)) or confidence < MIN_AGENT_CONFIDENCE:
        return None

    return ResolvedSource(
        dir=directory,
        files=dirs_to_files[directory],
        confidence=round(float(confidence), 3),
        ambiguous=bool(result.get("ambiguous", False)),
        resolved_by="agent",
    )


async def resolve_source_screen_precise(tree: list[str], module: str, screen: str) -> ResolvedSource | None:
    result = await _architecture_agent.resolve_screen(tree, module, screen, repo_kind="source")

    if "error" not in result:
        dirs_to_files = _group_by_dir(tree)
        resolved = _source_from_agent_result(dirs_to_files, result)
        if resolved is not None:
            logger.info(
                "architecture agent resolved source module=%r screen=%r -> dir=%r confidence=%.3f ambiguous=%s",
                module, screen, resolved.dir, resolved.confidence, resolved.ambiguous,
            )
            return resolved

    logger.info("architecture agent unusable for source %s/%s (%r) — falling back to fuzzy matcher", module, screen, result)
    return resolve_source_screen(tree, module, screen)


async def resolve_source_module_screens_precise(tree: list[str], module: str) -> list[ResolvedSource]:
    result = await _architecture_agent.resolve_module(tree, module, repo_kind="source")

    if "error" not in result and isinstance(result.get("screens"), list) and result["screens"]:
        dirs_to_files = _group_by_dir(tree)
        resolved_list: list[ResolvedSource] = []
        for item in result["screens"]:
            resolved = _source_from_agent_result(dirs_to_files, item)
            if resolved is None:
                resolved_list = []
                break
            resolved_list.append(resolved)

        if resolved_list:
            resolved_list.sort(key=lambda r: r.dir)
            logger.info(
                "architecture agent resolved source module=%r -> %d screen(s): %s",
                module, len(resolved_list), [r.dir for r in resolved_list],
            )
            return resolved_list

    logger.info("architecture agent unusable for source module %s (%r) — falling back to fuzzy matcher", module, result)
    return resolve_source_module_screens(tree, module)
