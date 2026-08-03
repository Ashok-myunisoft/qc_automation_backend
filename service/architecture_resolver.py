import logging
import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from Agents.architecture_agent import ArchitectureAgent

logger = logging.getLogger(__name__)

# Single shared agent instance — same eager-instantiation pattern app.py
# already uses for the other four agents.
_architecture_agent = ArchitectureAgent()

# Below this similarity score, we don't trust the match at all.
MIN_CONFIDENCE = 0.55

# Below this agent-reported confidence, treat the agent's answer as not
# trustworthy enough to use on its own — fall back to the fuzzy matcher
# instead of accepting a low-confidence guess.
MIN_AGENT_CONFIDENCE = 0.6

# If the best and second-best candidate are within this distance of each
# other, the match is ambiguous rather than confident. Surfaced to the
# caller so the UI/logs can say "found a couple of close matches" instead
# of silently picking one. (This is also the seam described above where an
# LLM tie-breaker could be inserted later if needed.)
AMBIGUITY_MARGIN = 0.08


def _normalize(text: str) -> str:
    """Strip everything but letters/digits and lowercase, so 'Instrument
    Master', 'instrument-master', and 'InstrumentMaster' all compare equal."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _slugify(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def _pascal(text: str) -> str:
    words = re.split(r"[\s_-]+", text.strip())
    return "".join(w[:1].upper() + w[1:] for w in words if w)


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


@dataclass
class ResolvedScreen:
    dir: str  # e.g. "Finance/InstrumentMaster"
    slug: str  # e.g. "instrument-master"
    feature_path: str
    script_path: str
    confidence: float
    ambiguous: bool
    resolved_by: str = "fuzzy"  # "agent" or "fuzzy" — which resolver produced this


def _group_by_dir(tree: list[str]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for path in tree:
        if "/" not in path:
            continue
        directory, filename = path.rsplit("/", 1)
        groups.setdefault(directory, []).append(filename)
    return groups


def resolve_existing(tree: list[str], module: str, screen: str) -> ResolvedScreen | None:
    module_norm = _normalize(module)
    screen_norm = _normalize(screen)

    scored: list[tuple[float, str, list[str]]] = []

    for directory, files in _group_by_dir(tree).items():
        parts = directory.split("/")

        # The module must appear somewhere in the path (fuzzy — tolerates
        # "Finance" vs "finance" vs "Fin" typos to a degree).
        module_hit = any(_similarity(_normalize(p), module_norm) > 0.75 for p in parts)
        if not module_hit:
            continue

        # Score the deepest folder name (the screen-level folder) against
        # the requested screen name.
        screen_folder = parts[-1]
        score = _similarity(_normalize(screen_folder), screen_norm)

        # A repo folder only counts as a real candidate if it actually has
        # a .feature file in it — otherwise it's not a screen's test folder.
        if not any(f.endswith(".feature") for f in files):
            continue

        scored.append((score, directory, files))

    if not scored:
        return None

    scored.sort(key=lambda t: t[0], reverse=True)
    best_score, best_dir, best_files = scored[0]

    if best_score < MIN_CONFIDENCE:
        return None

    ambiguous = len(scored) > 1 and (best_score - scored[1][0]) < AMBIGUITY_MARGIN

    feature_files = [f for f in best_files if f.endswith(".feature")]
    feature_file = feature_files[0]
    slug = feature_file[: -len(".feature")]

    script_file = next((f for f in best_files if f.startswith(slug) and f != feature_file), None)
    if script_file is None:
        script_file = next((f for f in best_files if f.endswith(".js")), None)
    if script_file is None:
        return None  # feature file with no matching script — not a usable pair

    return ResolvedScreen(
        dir=best_dir,
        slug=slug,
        feature_path=f"{best_dir}/{feature_file}",
        script_path=f"{best_dir}/{script_file}",
        confidence=round(best_score, 3),
        ambiguous=ambiguous,
    )


def resolve_module_screens(tree: list[str], module: str) -> list[ResolvedScreen]:
    """Like resolve_existing, but returns every screen folder under the
    given module instead of matching a single screen name. Reuses the
    same module-hit fuzzy match and the same 'has a .feature + matching
    .js' validity rule so results are consistent with single-screen fetch."""
    module_norm = _normalize(module)
    results: list[ResolvedScreen] = []

    for directory, files in _group_by_dir(tree).items():
        parts = directory.split("/")

        module_hit = any(_similarity(_normalize(p), module_norm) > 0.75 for p in parts)
        if not module_hit:
            continue

        feature_files = [f for f in files if f.endswith(".feature")]
        if not feature_files:
            continue

        feature_file = feature_files[0]
        slug = feature_file[: -len(".feature")]

        script_file = next((f for f in files if f.startswith(slug) and f != feature_file), None)
        if script_file is None:
            script_file = next((f for f in files if f.endswith(".js")), None)
        if script_file is None:
            continue  # feature file with no matching script — skip, not a usable pair

        results.append(ResolvedScreen(
            dir=directory,
            slug=slug,
            feature_path=f"{directory}/{feature_file}",
            script_path=f"{directory}/{script_file}",
            confidence=1.0,   # module hit is binary here — no per-screen ambiguity
            ambiguous=False,
        ))

    # Stable, readable order for the UI's screen-name list.
    results.sort(key=lambda r: r.dir)
    return results


def _valid_test_candidate(tree_set: set[str], directory: str, feature_path: str, script_path: str) -> bool:
    """An agent-proposed test-repo answer is only trustworthy if every path
    it named is copied verbatim from the real tree — never trust a path the
    agent might have constructed or normalized itself."""
    if not directory or not feature_path or not script_path:
        return False
    if feature_path not in tree_set or script_path not in tree_set:
        return False
    if not feature_path.startswith(directory + "/") or not script_path.startswith(directory + "/"):
        return False
    if not feature_path.endswith(".feature"):
        return False
    if script_path == feature_path or not script_path.endswith((".js", ".cy.js")):
        return False
    return True


def _screen_from_agent_test_result(result: dict) -> ResolvedScreen | None:
    directory     = result.get("dir")
    feature_path  = result.get("feature_path")
    script_path   = result.get("script_path")
    confidence    = result.get("confidence")

    if not isinstance(confidence, (int, float)) or confidence < MIN_AGENT_CONFIDENCE:
        return None

    feature_file = feature_path.rsplit("/", 1)[-1]
    slug = feature_file[:-len(".feature")] if feature_file.endswith(".feature") else feature_file

    return ResolvedScreen(
        dir=directory,
        slug=slug,
        feature_path=feature_path,
        script_path=script_path,
        confidence=round(float(confidence), 3),
        ambiguous=bool(result.get("ambiguous", False)),
        resolved_by="agent",
    )


async def resolve_existing_precise(tree: list[str], module: str, screen: str) -> ResolvedScreen | None:
    """Agent-first version of resolve_existing: asks ArchitectureAgent to
    pick the exact folder/feature/script for this module+screen, verifies
    the answer is actually present in the tree, and only falls back to the
    deterministic fuzzy matcher if the agent errors, is under-confident, or
    names something that isn't real."""
    result = await _architecture_agent.resolve_screen(tree, module, screen, repo_kind="test")

    if "error" not in result:
        tree_set = set(tree)
        if _valid_test_candidate(tree_set, result.get("dir"), result.get("feature_path"), result.get("script_path")):
            resolved = _screen_from_agent_test_result(result)
            if resolved is not None:
                logger.info(
                    "architecture agent resolved module=%r screen=%r -> dir=%r confidence=%.3f ambiguous=%s",
                    module, screen, resolved.dir, resolved.confidence, resolved.ambiguous,
                )
                return resolved

    logger.info("architecture agent unusable for %s/%s (%r) — falling back to fuzzy matcher", module, screen, result)
    return resolve_existing(tree, module, screen)


async def resolve_module_screens_precise(tree: list[str], module: str) -> list[ResolvedScreen]:
    """Agent-first version of resolve_module_screens. Falls back to the
    fuzzy matcher as a whole if the agent errors or returns nothing usable
    at all — a partially-bad module response isn't trustworthy either, since
    we can't tell if the agent simply missed a screen."""
    result = await _architecture_agent.resolve_module(tree, module, repo_kind="test")

    if "error" not in result and isinstance(result.get("screens"), list) and result["screens"]:
        tree_set = set(tree)
        resolved_list: list[ResolvedScreen] = []
        for item in result["screens"]:
            if not _valid_test_candidate(tree_set, item.get("dir"), item.get("feature_path"), item.get("script_path")):
                resolved_list = []
                break
            screen = _screen_from_agent_test_result(item)
            if screen is None:
                resolved_list = []
                break
            resolved_list.append(screen)

        if resolved_list:
            resolved_list.sort(key=lambda r: r.dir)
            logger.info(
                "architecture agent resolved module=%r -> %d screen(s): %s",
                module, len(resolved_list), [r.dir for r in resolved_list],
            )
            return resolved_list

    logger.info("architecture agent unusable for module %s (%r) — falling back to fuzzy matcher", module, result)
    return resolve_module_screens(tree, module)


@dataclass
class ResolvedSource:
    dir: str          # e.g. "Finance/InstrumentMaster"
    files: list[str]  # filenames directly inside that dir, e.g. ["instrument-master.component.ts", "...html"]
    confidence: float
    ambiguous: bool
    resolved_by: str = "fuzzy"  # "agent" or "fuzzy" — which resolver produced this


def resolve_source_screen(tree: list[str], module: str, screen: str) -> ResolvedSource | None:
    """Same fuzzy module/screen matching as resolve_existing, but against the
    *source* repo: any non-empty folder under the module counts as a
    candidate (source screens aren't necessarily .feature/.js pairs)."""
    module_norm = _normalize(module)
    screen_norm = _normalize(screen)

    scored: list[tuple[float, str, list[str]]] = []

    for directory, files in _group_by_dir(tree).items():
        parts = directory.split("/")

        module_hit = any(_similarity(_normalize(p), module_norm) > 0.75 for p in parts)
        if not module_hit or not files:
            continue

        screen_folder = parts[-1]
        score = _similarity(_normalize(screen_folder), screen_norm)
        scored.append((score, directory, files))

    if not scored:
        return None

    scored.sort(key=lambda t: t[0], reverse=True)
    best_score, best_dir, best_files = scored[0]

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
    """Like resolve_source_screen, but returns every screen folder under the
    given module in the source repo."""
    module_norm = _normalize(module)
    results: list[ResolvedSource] = []

    for directory, files in _group_by_dir(tree).items():
        parts = directory.split("/")

        module_hit = any(_similarity(_normalize(p), module_norm) > 0.75 for p in parts)
        if not module_hit or not files:
            continue

        results.append(ResolvedSource(
            dir=directory,
            files=files,
            confidence=1.0,
            ambiguous=False,
        ))

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
        files=dirs_to_files[directory],   # file list comes from the real tree, never the agent
        confidence=round(float(confidence), 3),
        ambiguous=bool(result.get("ambiguous", False)),
        resolved_by="agent",
    )


async def resolve_source_screen_precise(tree: list[str], module: str, screen: str) -> ResolvedSource | None:
    """Agent-first version of resolve_source_screen. The agent only ever
    needs to name the correct folder — the actual file list inside it is
    taken from the real tree, not from the agent, so a hallucinated
    filename can't slip through."""
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
    """Agent-first version of resolve_source_module_screens. Falls back to
    the fuzzy matcher as a whole if the agent's response isn't fully usable
    — same reasoning as resolve_module_screens_precise."""
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


def build_new_path(module: str, screen: str) -> ResolvedScreen:
    """No existing match in the repo — construct a path for a brand new
    screen, following the same <Module>/<ScreenFolder>/<slug>.{feature,js}
    convention the resolver looks for above."""
    module_folder = module.strip().replace(" ", "")
    screen_folder = _pascal(screen)
    slug = _slugify(screen)
    directory = f"{module_folder}/{screen_folder}"

    return ResolvedScreen(
        dir=directory,
        slug=slug,
        feature_path=f"{directory}/{slug}.feature",
        script_path=f"{directory}/{slug}.js",
        confidence=1.0,
        ambiguous=False,
    )