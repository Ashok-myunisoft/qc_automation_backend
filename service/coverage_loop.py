"""Coverage loop: code computes a checklist from ScreenFacts, finds gaps in the feature text (fields, options, messages,
rules, capabilities, tabs and the UPDATE scenarios), the agent fills only the gaps (max COVERAGE_LOOP_MAX_ROUNDS rounds,
default 2)."""
from __future__ import annotations

import logging
import os
import re
from collections import Counter
from typing import Awaitable, Callable

from service import pass_planner, value_gen

logger = logging.getLogger(__name__)


def max_rounds() -> int:
    """A number setting with a default in code (no .env needed)."""
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
        if f.get("readonly") and not f.get("computed_from"):
            continue                                   # read-only, nothing known to check: not a gap
        names = {_norm(f["name"]), _norm(f["label"])}
        used = any(n and n in _norm(h) for n in names for h in cells_by_col) or f["name"].lower() in low \
            or f"\"{f['label'].lower()}\"" in low
        if f.get("calculated"):
            add(f"calculated {f['name']} checked", re.search(r'the "%s" field should show' % re.escape(f["name"]), text, re.I),
                f"the calculated field '{f['name']}' is never checked: add a row that checks it with the step  the \"{f['name']}\" "
                f"field should show \"<Value>\"  (never fill it)")
            continue
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
    try:
        _update_items(facts, text, add)
    except Exception:  # noqa: BLE001
        logger.exception("coverage loop: the update checks failed - the other checks stand")
    return items


# --------------------------------------------------------------------------- update checks
_KIND_OF_PASS = {"update_single_field": "single_field", "update_multiple_fields": "multiple_fields", "update_options": "options",
                 "update_boundaries": "boundaries", "update_dates": "dates", "update_clear": "clear", "update_no_change": "no_change"}


def update_row_counts(text: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for u in pass_planner.update_rows(text):
        out[u["kind"]] = out.get(u["kind"], 0) + 1
    return out


def _update_items(facts: dict, text: str, add) -> None:
    plan = pass_planner.plan_update_passes(facts)
    if not plan:
        return
    rows = pass_planner.update_rows(text)
    counts = Counter(u["kind"] for u in rows)
    seen_kind = set()
    for p in plan:
        kind = _KIND_OF_PASS.get(p.id, "other")
        if kind in seen_kind:
            continue
        seen_kind.add(kind)
        n = counts.get(kind, 0)
        add(f"update kind {kind} present", n >= 1,
            f"UPDATE kind '{kind}' has no row: write it with the title prefix \"{p.title_prefix.split(' - ')[0]}\" ({p.theme_text[:160]})")
        if n >= 1:
            add(f"update kind {kind} rows", n >= pass_planner.MIN_UPDATE_ROWS,
                f"UPDATE kind '{kind}' has only {n} row(s): write at least {pass_planner.MIN_UPDATE_ROWS} with other records and other pool values")
    # every editable field changed alone at least once
    alone = {_norm(f) for u in rows if len(u["changes"]) == 1 and not u["cleared"] for f in u["changes"]}
    for f in facts["fields"]:
        if value_gen.is_editable(f):
            g = value_gen.update_group(f)
            add(f"update {f['name']} alone", _norm(f["name"]) in alone,
                f"UPDATE single field - {g}: a row that changes ONLY '{f['name']}' (a pool value, a record from the ledger)")
    # every option of every dropdown reached through an update
    by_field: dict[str, set] = {}
    for u in rows:
        for fld, val in u["changes"].items():
            by_field.setdefault(_norm(fld), set()).add(str(val).strip().lower())
    for f in facts["fields"]:
        if value_gen.is_editable(f) and value_gen.update_group(f) == "combobox" and len(f.get("options") or []) >= 2:
            for opt in f["options"]:
                add(f"update option {f['name']}={opt}", opt.lower() in by_field.get(_norm(f["name"]), set()),
                    f"UPDATE options: a row whose update sets '{f['name']}' to the option '{opt}'")
    # no two update rows with identical values
    sigs = Counter((u["record"].lower(), tuple(sorted((_norm(f), str(v).strip().lower()) for f, v in u["changes"].items())),
                    tuple(sorted(_norm(c) for c in u["cleared"]))) for u in rows)
    dup = sum(1 for n in sigs.values() if n > 1)
    add("update rows differ", dup == 0,
        f"UPDATE: {dup} update row(s) are repeated (same record, same values): write rows with other records and other pool values")


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
    counts = update_row_counts(current)
    msg = f"coverage: before {report['before'][0]}/{report['before'][1]} items, after {report['after'][0]}/{report['after'][1]}"
    if counts:
        msg += "; update rows by kind: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    logger.info(msg + ("; uncovered: " + " | ".join(report["uncovered"]) if report["uncovered"] else ""))
    if log:
        await log(msg + (f" ({len(report['uncovered'])} still uncovered - listed in the backend log)" if report["uncovered"] else ""))
    return current, report