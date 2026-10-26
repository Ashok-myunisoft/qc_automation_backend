"""Pass planner (code only): decides which topic passes a screen needs, plus ScreenFacts,
the record ledger and the merge ordering (spec 01 sections 2, 3 and 5)."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from service import screen_behavior

logger = logging.getLogger(__name__)

ORDER = {"create": 0, "read_only": 1, "update": 2, "delete": 3}


@dataclass
class Pass:
    id: str
    title_prefix: str
    theme_text: str
    needs: tuple = ()
    mutates: bool = False
    order_group: str = "create"
    section: dict | None = None
    extra: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- ScreenFacts
def build_facts(spec: dict | None, project_analysis: dict, behavior: dict | None = None) -> dict:
    """One dict per screen: fields, buttons, messages, sections, loops, behavior."""
    from service import script_builder  # local import: avoid cycles at module load
    spec = spec or project_analysis.get("screen_spec") or {}
    page = {}
    mods = project_analysis.get("modules") or []
    if mods and (mods[0].get("pages") or []):
        page = mods[0]["pages"][0]
    controls = script_builder.controls_from_analysis(project_analysis)
    by_norm = {re.sub(r"[^a-z0-9]", "", k.lower()): (k, v) for k, v in controls.items()}
    fields = []
    for f in spec.get("fields") or []:
        if not f.get("active", True):
            continue
        hit = by_norm.get(re.sub(r"[^a-z0-9]", "", f["name"].lower()))
        fields.append({
            "name": f["name"], "label": f.get("label") or f["name"], "kind": f.get("kind") or "input",
            "type": f.get("type"), "required": bool(f.get("required")), "required_message": f.get("required_message", ""),
            "max_length": f.get("max_length"), "regex": f.get("regex"),
            "options": [o["text"] for o in f.get("options") or [] if not o.get("blank")],
            "lookup": bool(f.get("lookup")), "record_picker": bool(f.get("record_picker")),
            "key_field": bool(f.get("key_field")), "readonly": bool(f.get("readonly")),
            "grid": bool(f.get("grid")), "data_cy": hit[1]["dataCy"] if hit else None,
            "verified_kind": hit[1]["kind"] if hit else None,
        })
    if not fields:  # no spec: fall back to scan-verified controls so the planner still works
        for base, v in controls.items():
            fields.append({"name": base, "label": base, "kind": v["kind"], "required": False, "required_message": "",
                           "options": [], "key_field": False, "readonly": False, "grid": False,
                           "data_cy": v["dataCy"], "verified_kind": v["kind"]})
    behavior = behavior if behavior is not None else (project_analysis.get("behavior") or {})
    msgs = dict((spec.get("messages") or {}))
    return {
        "fields": fields, "controls": controls, "behavior": behavior,
        "capabilities": screen_behavior.capabilities(behavior),
        "buttons": [a.get("action_name") for a in page.get("business_actions") or [] if isinstance(a, dict)],
        "sections": [s for s in page.get("screen_sections") or [] if isinstance(s, dict)],
        "loops": page.get("loops") or [],
        "messages": msgs,
        "backend_rules": [r for r in ((spec.get("backend") or {}).get("rules") or []) if isinstance(r, dict)],
        "screen_name": spec.get("screen_name") or page.get("page_name") or "",
    }


# --------------------------------------------------------------------------- planner
_THEMES = {
    "form_core": "basic save with required fields only; full save with every field; each mandatory field empty one at a "
                 "time plus all empty (real required messages); duplicate record using the real duplicate message.",
    "field_text_number": "per text/number field: minimum, maximum, zero, negative, just over the limit, decimals, over-length, "
                         "spaces, special characters, invalid format - only those that make sense and only with a real rule.",
    "field_date": "date fields: today, past, future, invalid text, empty, boundary dates; from/to relations when two date "
                  "fields exist; only rules the source states.",
    "field_options": "every option of every combobox/radio/picklist once; checkbox checked and unchecked; combinations that "
                     "the conditional logic in the source makes dependent.",
    "line_items": "grid rows: one row, several rows, add row, remove row, maximum rows if bounded.",
    "business_rules": "one positive and one negative scenario for each backend/conditional rule, using the rule's real message.",
    "form_behavior": "Reset (filled, partly filled, with a record open), Close (filled, empty - nothing is saved), button "
                     "visibility for a new versus an existing record, read-only fields, form open and closed.",
    "open_existing": "open each existing record from the ledger and check that every saved value is shown correctly.",
    "list_behavior": "list capabilities that exist on this screen: search (full, partial, other letter case, name, "
                     "description), no-match with the real empty-state text, clear search, filter by type/status and back to "
                     "all, sort ascending and descending for each sortable column, list/card view switch, displayed "
                     "type/status text, counters after a save.",
    "update": "change each editable field alone, several together, save with no change, clear a mandatory field (real "
              "message), reset a combobox to its default.",
    "delete": "delete from the list and from the open form: confirm Yes (record gone) and answer No (record kept), with the "
              "prompt text asserted.",
}


def _has(facts: dict, *kinds) -> bool:
    return any(f["kind"] in kinds and not f.get("grid") for f in facts["fields"])


def plan_passes(facts: dict) -> list[Pass]:
    fields, caps, buttons = facts["fields"], facts["capabilities"], [str(b).lower() for b in facts["buttons"]]
    beh = facts["behavior"]
    out: list[Pass] = [Pass("form_core", "Form core", _THEMES["form_core"], mutates=True)]
    if any((f.get("max_length") or f.get("regex") or f.get("type") in ("number", "int", "integer", "decimal", "numeric"))
           and f["kind"] in ("input", "textarea") for f in fields):
        out.append(Pass("field_text_number", "Text and number fields", _THEMES["field_text_number"], mutates=True))
    if _has(facts, "date"):
        out.append(Pass("field_date", "Date fields", _THEMES["field_date"], mutates=True))
    if any(f["kind"] in ("combobox", "radio", "checkbox") or (f["kind"] == "picklist" and f["options"]) for f in fields):
        out.append(Pass("field_options", "Field options", _THEMES["field_options"], mutates=True))
    if facts["loops"] or any(f.get("grid") for f in fields):
        out.append(Pass("line_items", "Line items", _THEMES["line_items"], mutates=True))
    for sec in facts["sections"]:
        if sec.get("activation_required"):
            label = sec.get("section_label") or "Section"
            out.append(Pass("tabs", f"Tab {label}", f"only the fields and rules of the tab '{label}'; label the Rule exactly "
                            f"'{label}'.", mutates=True, section=sec, extra={"label": label}))
    if facts["backend_rules"]:
        out.append(Pass("business_rules", "Business rules", _THEMES["business_rules"], needs=("backend",), mutates=True))
    if beh.get("modal") or "reset" in caps or "close" in caps:
        out.append(Pass("form_behavior", "Form behaviour", _THEMES["form_behavior"], order_group="read_only"))
    if any(f.get("key_field") and f["kind"] == "picklist" for f in fields) or beh.get("containers"):
        out.append(Pass("open_existing", "Open existing", _THEMES["open_existing"], order_group="read_only"))
    if beh.get("containers") or any(c in caps for c in ("search", "filter", "sort", "view", "counters", "empty_state")):
        out.append(Pass("list_behavior", "List behaviour", _THEMES["list_behavior"], needs=("behavior",), mutates=True,
                        order_group="read_only"))
    if "update" in buttons or "edit" in buttons:
        out.append(Pass("update", "Update", _THEMES["update"], mutates=True, order_group="update"))
    if "delete" in buttons or beh.get("button_visibility") or beh.get("containers"):
        out.append(Pass("delete", "Delete", _THEMES["delete"], needs=("delete",), mutates=True, order_group="delete"))
    logger.info("planned %d passes: %s", len(out), ", ".join(p.id + (f"[{p.extra.get('label')}]" if p.extra.get("label") else "") for p in out))
    return out


# --------------------------------------------------------------------------- ledger
def _cells(line: str) -> list[str]:
    s = line.strip()
    s = s[1:] if s.startswith("|") else s
    s = s[:-1] if s.endswith("|") else s
    return [c.strip() for c in re.split(r"(?<!\\)\|", s)]


def examples_tables(feature_text: str):
    """Yields (steps_text, header_cells, [row cells...]) per Examples table."""
    lines = (feature_text or "").splitlines()
    steps: list[str] = []
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if re.match(r"^Scenario\s+(Outline|Template)\s*:", s, re.I) or re.match(r"^Scenario\s*:", s, re.I):
            steps = []
        elif re.match(r"^(Given|When|Then|And|But)\s", s):
            steps.append(s)
        elif re.match(r"^(Examples|Scenarios)\s*:", s, re.I):
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines) and lines[j].strip().startswith("|"):
                header = _cells(lines[j])
                rows, k = [], j + 1
                while k < len(lines) and lines[k].strip().startswith("|"):
                    rows.append(_cells(lines[k]))
                    k += 1
                yield "\n".join(steps), header, rows
                i = k
                continue
        i += 1


def build_ledger(feature_text: str, key_field: str = "") -> list[dict]:
    """Records the create-group scenarios save: every row of an Examples table whose scenario clicks Save and whose
    ExpectedResult is a save success. {key, values{col: value}, condition}."""
    ledger: dict[str, dict] = {}
    for steps, header, rows in examples_tables(feature_text):
        if not re.search(r"click the save button", steps, re.I):
            continue
        low = [h.lower() for h in header]
        if "expectedresult" not in low:
            continue
        er = low.index("expectedresult")
        key_col = None
        if key_field and key_field in header:
            key_col = header.index(key_field)
        else:
            key_col = next((i for i, h in enumerate(header) if re.search(r"code$|^code|key$", h, re.I)), None)
        if key_col is None:
            continue
        for r in rows:
            if len(r) < len(header) or not re.search(r"saved successfully", r[er], re.I):
                continue
            key = r[key_col]
            if not key:
                continue
            ledger[key.lower()] = {"key": key, "condition": r[0],
                                   "values": {h: r[i] for i, h in enumerate(header) if i not in (0, er)}}
    return list(ledger.values())


def render_ledger(ledger: list[dict], limit: int = 80) -> str:
    if not ledger:
        return "RECORD LEDGER: no record has been saved yet by earlier passes."
    lines = ["RECORD LEDGER (records that exist at this point; reference ONLY these keys and values, never invent codes):"]
    for rec in ledger[:limit]:
        vals = "; ".join(f"{k}={v}" for k, v in rec["values"].items() if v)
        lines.append(f"- {rec['key']}: {vals}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- ordering / merge
def order_passes(passes_with_text: list[tuple[Pass, str]]) -> list[tuple[Pass, str]]:
    """Stable sort by order_group: create -> read_only -> update -> delete."""
    return sorted(passes_with_text, key=lambda pt: ORDER.get(pt[0].order_group, 9))
