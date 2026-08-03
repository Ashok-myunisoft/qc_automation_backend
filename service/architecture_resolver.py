import re
from dataclasses import dataclass
from difflib import SequenceMatcher

# Below this similarity score, we don't trust the match at all.
MIN_CONFIDENCE = 0.55

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