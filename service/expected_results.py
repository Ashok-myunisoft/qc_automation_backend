from __future__ import annotations

import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_OVERRIDE_PATH = Path(__file__).resolve().parent.parent / "config" / "expected_messages.json"

# analysis keys whose text can hold a message
_MESSAGE_KEY_HINTS = ("message", "validation", "success", "error", "signal", "expected", "alert", "toast", "snack")
_STANDARD_TEXTS = ("detailssavedsuccessfully",)
_STANDARD_RE = re.compile(r"^.{1,80}\*\s*should not be empty\s*$", re.IGNORECASE)
_QUOTED_RE = re.compile(r"""['"\u2018\u2019\u201c\u201d]([^'"\u2018\u2019\u201c\u201d]{3,}?)['"\u2018\u2019\u201c\u201d]""")
_MIN_SUBSTRING_LEN = 6
_MAX_DROP_SHARE = 0.5

_BLOCK_RE = re.compile(r"^\s*(?:Feature|Background|Rule|Scenario Outline|Scenario Template|Scenario|Example)\s*:", re.IGNORECASE)
_OUTLINE_RE = re.compile(r"^\s*(?:Scenario Outline|Scenario Template)\s*:", re.IGNORECASE)
_EXAMPLES_RE = re.compile(r"^\s*(?:Examples|Scenarios)\s*:", re.IGNORECASE)
_TAG_RE = re.compile(r"^\s*@")


def message_fragment(text: str) -> str:
    """A message with {placeholders} is matched by its fixed part only: the text before the first {."""
    t = str(text or "")
    return t.split("{")[0].strip() if "{" in t else t.strip()


def _n(text) -> str:
    """Comparison form: lower case, letters and digits only."""
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def _add(text: str, out: set[str]) -> None:
    text = message_fragment(text)
    if not text or text.lower() == "unknown":
        return
    out.add(_n(text))
    for quoted in _QUOTED_RE.findall(text):
        out.add(_n(quoted))
    out.discard("")


def _collect(obj, under_hint: bool, out: set[str]) -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            hint = under_hint or any(h in str(key).lower() for h in _MESSAGE_KEY_HINTS)
            _collect(value, hint, out)
    elif isinstance(obj, list):
        for value in obj:
            _collect(value, under_hint, out)
    elif isinstance(obj, str) and under_hint:
        _add(obj, out)


def known_messages(project_analysis: dict | None, screen_name: str = "") -> set[str]:
    """Normalised messages that are real for this screen."""
    out: set[str] = set()
    try:
        _collect(project_analysis or {}, False, out)
    except Exception:  # never let a strange analysis shape stop generation
        logger.exception("could not read messages from the analysis")
    out.update(_override_messages(screen_name))
    return out


def _override_messages(screen_name: str) -> set[str]:
    """Messages the QC team confirmed (config/expected_messages.json)."""
    out: set[str] = set()
    try:
        if not _OVERRIDE_PATH.is_file():
            return out
        data = json.loads(_OVERRIDE_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("config/expected_messages.json could not be read - ignoring it")
        return out
    if not isinstance(data, dict):
        return out
    for text in data.get("all") or []:
        if isinstance(text, str):
            _add(text, out)
    screens = data.get("screens")
    if isinstance(screens, dict):
        wanted = _n(screen_name)
        for name, messages in screens.items():
            if wanted and _n(name) == wanted and isinstance(messages, list):
                for text in messages:
                    if isinstance(text, str):
                        _add(text, out)
    return out


def _single_ok(expected: str, known: set[str]) -> bool:
    expected = message_fragment(expected)
    if _STANDARD_RE.match(expected):
        return True
    ne = _n(expected)
    if not ne:
        return True
    if ne in _STANDARD_TEXTS or ne in known:
        return True
    # the confirmation step uses cy.contains, so a fragment of a real message passes
    if len(ne) >= _MIN_SUBSTRING_LEN and any(ne in k for k in known):
        return True
    return False


def is_verified(expected: str, known: set[str]) -> bool:
    expected = message_fragment(expected)
    if not expected:
        return True
    if _single_ok(expected, known):
        return True
    parts = [p.strip() for p in re.split(r"\s*[,;]\s*", expected) if p.strip()]
    return len(parts) > 1 and all(_single_ok(p, known) for p in parts)


# ---------------------------------------------------------------------------
# reading / editing the feature text
# ---------------------------------------------------------------------------
def _cells(line: str) -> list[str]:
    inner = line.strip()
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|"):
        inner = inner[:-1]
    return [c.strip() for c in re.split(r"(?<!\\)\|", inner)]


def _tables(lines: list[str]):
    """Yields (examples_line_idx, header_idx, [data row idx...], expected_col) for every
    Examples table that has an ExpectedResult column."""
    i, n = 0, len(lines)
    while i < n:
        if _EXAMPLES_RE.match(lines[i]):
            ex = i
            j = i + 1
            while j < n and not lines[j].strip().startswith("|") and not lines[j].strip():
                j += 1
            if j < n and lines[j].strip().startswith("|"):
                header = j
                rows = []
                k = j + 1
                while k < n and lines[k].strip().startswith("|"):
                    rows.append(k)
                    k += 1
                names = [c.lower() for c in _cells(lines[header])]
                if "expectedresult" in names:
                    yield ex, header, rows, names.index("expectedresult")
                i = k
                continue
        i += 1


def verify_feature(feature_text: str, project_analysis: dict | None, screen_name: str = ""):
    """Returns (feature_text, report). The text is returned unchanged when nothing
    has to be removed. report = {checked, dropped:[(condition, expected)],
    kept_unverified:[...], removed_outlines, note}."""
    report = {"checked": 0, "dropped": [], "kept_unverified": [], "removed_outlines": 0, "note": ""}
    if not feature_text or not feature_text.strip():
        return feature_text, report

    lines = feature_text.split("\n")
    known = known_messages(project_analysis, screen_name)

    bad: list[tuple[int, str, str]] = []  # (line idx, condition, expected)
    for _ex, _header, rows, col in _tables(lines):
        for r in rows:
            cells = _cells(lines[r])
            if col >= len(cells):
                continue
            report["checked"] += 1
            expected = cells[col]
            if not is_verified(expected, known):
                bad.append((r, cells[0] if cells else "", expected))

    if not bad:
        return feature_text, report

    if report["checked"] and len(bad) / report["checked"] > _MAX_DROP_SHARE:
        report["kept_unverified"] = [(c, e) for _r, c, e in bad]
        report["note"] = (f"{len(bad)} of {report['checked']} expected results could not be matched to a "
                          "message found for this screen - the analysis probably carries few messages, "
                          "so none were removed")
        return feature_text, report

    drop_rows = {r for r, _c, _e in bad}
    report["dropped"] = [(c, e) for _r, c, e in bad]

    # which tables become empty
    drop_lines: set[int] = set(drop_rows)
    for ex, header, rows, _col in _tables(lines):
        if rows and all(r in drop_rows for r in rows):
            drop_lines.add(header)
            drop_lines.add(ex)

    kept = [ln for idx, ln in enumerate(lines) if idx not in drop_lines]

    # remove an outline that has no Examples table left
    kept, removed = _remove_outlines_without_examples(kept)
    report["removed_outlines"] = removed

    text = "\n".join(kept)
    return text, report


def _remove_outlines_without_examples(lines: list[str]):
    starts: list[int] = []  # start of every block, tags included
    for idx, ln in enumerate(lines):
        if _BLOCK_RE.match(ln):
            s = idx
            while s - 1 >= 0 and _TAG_RE.match(lines[s - 1]):
                s -= 1
            starts.append(s)
    removed = 0
    drop: set[int] = set()
    for pos, s in enumerate(starts):
        e = starts[pos + 1] if pos + 1 < len(starts) else len(lines)
        header_idx = next((k for k in range(s, e) if _BLOCK_RE.match(lines[k])), None)
        if header_idx is None or not _OUTLINE_RE.match(lines[header_idx]):
            continue
        if not any(_EXAMPLES_RE.match(lines[k]) for k in range(s, e)):
            drop.update(range(s, e))
            removed += 1
    if not drop:
        return lines, 0
    return [ln for idx, ln in enumerate(lines) if idx not in drop], removed