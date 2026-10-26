"""Coverage loop (flag USE_COVERAGE_LOOP): code computes a checklist from ScreenFacts, finds gaps in the feature
text, the agent fills only the gaps (max COVERAGE_LOOP_MAX_ROUNDS rounds)."""
from __future__ import annotations

import logging
import os
import re
from typing import Awaitable, Callable

from service import pass_planner

logger = logging.getLogger(__name__)


def enabled() -> bool:
    return os.getenv("USE_COVERAGE_LOOP", "0").strip().lower() in ("1", "true", "yes", "on")


def max_rounds() -> int:
    try:
        return max(0, int(os.getenv("COVERAGE_LOOP_MAX_ROUNDS", "2")))
    except ValueError:
        return 2


# capability -> regex of the step sentence that proves it is covered
_CAP_STEP = {
    "search": r"I search the list for",
    "filter": r"I filter the list by",
    "sort": r"I sort the list by",
    "view": r"I switch to the",
    "counters": r"I remember the number shown for",
    "empty_state": r"I should see the empty state",
    "reset": r"I click the Reset button",
    "close": r"I click the Close button",
    "button_visibility": r"the \"[^\"]*\" action should be",
}


def _norm(t) -> str:
    return re.sub(r"[^a-z0-9]", "", str(t or "").lower())


def checklist(facts: dict, feature_text: str) -> list[dict]:
    """[{item, covered, gap_text}] - one entry per checklist item."""
    text = feature_text or ""
    low = text.lower()
    tables = list(pass_planner.examples_tables(text))
    cells_by_col: dict[str, set[str]] = {}
    for _steps, header, rows in tables:
        for ci, h in enumerate(header):
            for r in rows:
                if ci < len(r):
                    cells_by_col.setdefault(_norm(h), set()).add(r[ci].strip().lower())
    all_cells = {c for s in cells_by_col.values() for c in s}
    expected = {c.lower() for c in cells_by_col.get("expectedresult", set())}
    expected_blob = " | ".join(expected)
    items: list[dict] = []

    def add(item, covered, gap):
        items.append({"item": item, "covered": bool(covered), "gap": gap})

    for f in facts["fields"]:
        names = {_norm(f["name"]), _norm(f["label"])}
        used = any(n and n in _norm(h) for n in names for h in cells_by_col) or f["name"].lower() in low \
            or f"\"{f['label'].lower()}\"" in low
        add(f"field {f['name']} used", used, f"the field '{f['name']}' ({f['kind']}) is not used in any scenario")
        if f.get("required") and f.get("required_message") and not f.get("readonly"):
            col = next((h for h in cells_by_col if _norm(f["name"]) == h), None)
            empty_row = False
            if col:
                for _s, header, rows in tables:
                    hn = [_norm(h) for h in header]
                    if col in hn and "expectedresult" in hn:
                        ci, ei = hn.index(col), hn.index("expectedresult")
                        empty_row |= any(len(r) > max(ci, ei) and not r[ci] and _norm(f["required_message"]) in _norm(r[ei])
                                         for r in rows)
            add(f"required {f['name']}", empty_row or _norm(f["required_message"]) in _norm(expected_blob),
                f"a row with '{f['name']}' empty and ExpectedResult '{f['required_message']}'")
        if f["kind"] in ("combobox", "radio", "picklist") and f.get("options"):
            for opt in f["options"]:
                add(f"option {f['name']}={opt}", opt.lower() in all_cells,
                    f"a row that uses the option '{opt}' for '{f['name']}'")
        if f["kind"] == "checkbox":
            for state in ("checked", "unchecked"):
                add(f"checkbox {f['name']} {state}", state in all_cells, f"a row with '{f['name']}' {state}")
    for b in facts["buttons"]:
        key = _norm(b)
        pat = {"addnew": r"Add New button", "save": r"save button", "update": r"Update button",
               "edit": r"Edit button", "delete": r"Delete button|delete the (open )?record|delete prompt"}.get(key)
        if pat:
            add(f"button {b}", re.search(pat, text, re.I), f"a scenario that uses the {b} action")
    msg_roles = facts["messages"]
    for role in ("save", "update", "delete", "duplicate"):
        for m in (msg_roles.get(role) or []):
            add(f"message {role}", _norm(m) in _norm(expected_blob), f"a row whose ExpectedResult is the {role} message '{m}'")
    for r in facts["backend_rules"]:
        m = r.get("message")
        if m:
            add(f"rule {m[:40]}", _norm(m) in _norm(expected_blob), f"a row that triggers the rule and expects '{m}'")
    for cap in facts["capabilities"]:
        pat = _CAP_STEP.get(cap)
        if pat:
            add(f"capability {cap}", re.search(pat, text, re.I), f"scenarios for the list/form capability '{cap}'")
    for s in facts["sections"]:
        if s.get("activation_required"):
            lab = s.get("section_label", "")
            add(f"tab {lab}", re.search(r"^\s*Rule\s*:\s*" + re.escape(lab), text, re.I | re.M), f"a Rule block for the tab '{lab}'")
    return items


def score(items: list[dict]) -> tuple[int, int]:
    return sum(1 for i in items if i["covered"]), len(items)


def gaps(items: list[dict]) -> list[str]:
    return [i["gap"] for i in items if not i["covered"]]


async def run_loop(facts: dict, feature_text: str,
                   gap_pass: Callable[[list[str], str], Awaitable[str]],
                   merge: Callable[[str, str], str],
                   log: Callable[[str], Awaitable[None]] | None = None) -> tuple[str, dict]:
    """gap_pass(gap_lines, current_feature) -> new Gherkin scenarios; merge(current, new) -> merged feature."""
    report = {"before": None, "after": None, "rounds": 0, "uncovered": []}
    items = checklist(facts, feature_text)
    report["before"] = score(items)
    current = feature_text
    for rnd in range(max_rounds()):
        g = gaps(items)
        if not g:
            break
        report["rounds"] = rnd + 1
        try:
            new_text = await gap_pass(g, current)
        except Exception:  # noqa: BLE001
            logger.exception("coverage gap pass failed - keeping the feature as it is")
            break
        merged = merge(current, new_text)
        new_items = checklist(facts, merged)
        if score(new_items)[0] <= score(items)[0]:
            logger.info("coverage round %d closed no gap - stopping", rnd + 1)
            break
        current, items = merged, new_items
    report["after"] = score(items)
    report["uncovered"] = gaps(items)
    msg = f"coverage: before {report['before'][0]}/{report['before'][1]} items, after {report['after'][0]}/{report['after'][1]}"
    logger.info(msg + ("; uncovered: " + " | ".join(report["uncovered"]) if report["uncovered"] else ""))
    if log:
        await log(msg + (f" ({len(report['uncovered'])} still uncovered - listed in the backend log)" if report["uncovered"] else ""))
    return current, report
