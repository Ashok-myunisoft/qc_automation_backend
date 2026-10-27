"""Pass planner (code only): decides which topic passes a screen needs, plus ScreenFacts,
the record ledger and the merge ordering (spec 01 sections 2, 3 and 5).

The update scenarios are planned as separate passes by kind (single field per field kind, several fields, options,
boundaries, dates, clear, no change), each only when the screen has the matching field type."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from service import screen_behavior, value_gen

logger = logging.getLogger(__name__)

ORDER = {"create": 0, "read_only": 1, "update": 2, "delete": 3}
MIN_RECORDS = 6          # form_core must save at least this many records so every update row can open a different one
MIN_UPDATE_ROWS = 3      # aim: rows per update kind that exists


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
            "calculated": bool(f.get("calculated")), "computed_from": list(f.get("computed_from") or []),
            "calculated_rule": f.get("calculated_rule"), "calculated_origin": list(f.get("calculated_origin") or []),
        })
    if not fields:  # no spec: fall back to scan-verified controls so the planner still works
        for base, v in controls.items():
            fields.append({"name": base, "label": base, "kind": v["kind"], "required": False, "required_message": "",
                           "options": [], "key_field": False, "readonly": False, "grid": False,
                           "data_cy": v["dataCy"], "verified_kind": v["kind"]})
    behavior = behavior if behavior is not None else (project_analysis.get("behavior") or {})
    msgs = dict((spec.get("messages") or {}))
    server_set = [str(x) for x in ((spec.get("backend") or {}).get("server_set_fields") or [])]
    rules_all = [r for r in ((spec.get("backend") or {}).get("rules") or []) if isinstance(r, dict)]
    rules, skipped = split_backend_rules(rules_all, fields, server_set)
    for r, why in skipped:
        logger.info("backend rule not UI-triggerable (no scenario written): %r - %s", r.get("message") or r.get("template"), why)
    return {
        "fields": fields, "controls": controls, "behavior": behavior,
        "capabilities": screen_behavior.capabilities(behavior),
        "buttons": [a.get("action_name") for a in page.get("business_actions") or [] if isinstance(a, dict)],
        "sections": [s for s in page.get("screen_sections") or [] if isinstance(s, dict)],
        "loops": page.get("loops") or [],
        "messages": msgs,
        "backend_rules": rules,
        "backend_rules_skipped": [{"message": r.get("message") or r.get("template"), "condition": r.get("condition"), "why": why}
                                  for r, why in skipped],
        "calculated": [f for f in fields if f.get("calculated")],
        "date_format": spec.get("date_format") or "",
        "screen_name": spec.get("screen_name") or page.get("page_name") or "",
    }


def split_backend_rules(rules: list[dict], fields: list[dict], server_set: list[str]) -> tuple[list[dict], list[tuple[dict, str]]]:
    """(rules a user can trigger from this screen, [(rule, why not)]). A rule whose condition only uses server-set values,
    internal ids or read-only / calculated fields cannot be produced by typing on the screen, so it gets no scenario."""
    ui = [f["name"] for f in fields if not f.get("readonly") and not f.get("calculated") and not f.get("grid")]
    other = [f["name"] for f in fields if f["name"] not in ui]
    usable, skipped = [], []
    for r in rules:
        if not (r.get("message") or "").strip():
            skipped.append((r, "the message has no fixed text to match (only {placeholders})"))
            continue
        cond = str(r.get("condition") or "")
        why = ""
        if cond.strip():
            def has(name: str) -> bool:
                return bool(re.search(r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % re.escape(name), cond, re.I))
            if not any(has(n) for n in ui):
                server_hits = [n for n in server_set if has(n)]
                id_hits = re.findall(r"\b\w+\.(\w*Id)\b", cond)
                other_hits = [n for n in other if has(n)]
                if server_hits or id_hits or other_hits:
                    why = "its condition uses only server-set / internal-id / read-only values (" + \
                          ", ".join(dict.fromkeys(server_hits + id_hits + other_hits)) + ")"
        (skipped.append((r, why)) if why else usable.append(r))
    return usable, skipped


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
    out.extend(plan_update_passes(facts))
    if "delete" in buttons or beh.get("button_visibility") or beh.get("containers"):
        out.append(Pass("delete", "Delete", _THEMES["delete"], needs=("delete",), mutates=True, order_group="delete"))
    logger.info("planned %d passes: %s", len(out), ", ".join(p.id + (f"[{p.extra.get('label')}]" if p.extra.get("label") else "") for p in out))
    return out


# --------------------------------------------------------------------------- update passes
UPDATE_RULES = (
    "UPDATE RULES (every update pass): open a saved record from the RECORD LEDGER with the existing-record step, a DIFFERENT "
    "record on every row (follow the ledger order, wrap around); change fields only to values of the UPDATE DATA POOL, a "
    "different value on every row and never the value the record already holds in the ledger; never change or claim to change "
    "the record key; never type into a calculated / read-only field; click Update; assert the update message from KNOWN MESSAGES "
    "and, where it is cheap, the new value with the step  the \"<Field>\" field should show \"<Value>\". Write at least "
    f"{MIN_UPDATE_ROWS} rows (more when the pool has more values) and never two rows with the same record and the same values. "
    "Start the title with the title prefix of this pass.")


def plan_update_passes(facts: dict) -> list[Pass]:
    buttons = [str(b).lower() for b in facts["buttons"]]
    if not ("update" in buttons or "edit" in buttons):
        return []
    edit = [f for f in facts["fields"] if value_gen.is_editable(f)]
    if not edit:
        return []
    groups: dict[str, list[dict]] = {}
    for f in edit:
        groups.setdefault(value_gen.update_group(f), []).append(f)

    def mk(pid: str, prefix: str, theme: str, **extra) -> Pass:
        extra.setdefault("label", pid.replace("update_", ""))
        return Pass(pid, prefix, theme, mutates=True, order_group="update", extra=extra)

    out: list[Pass] = []
    for g in ("text", "textarea", "combobox", "picklist", "date", "checkbox", "number"):
        fl = groups.get(g)
        if not fl:
            continue
        names = ", ".join(f["name"] for f in fl)
        out.append(mk("update_single_field", f"Update single field - {g}",
                      f"change ONE {g} field per row and nothing else; fields of this kind: {names}. One Scenario Outline per "
                      "field (or one outline with the field in a column), every row a different record and a different value.",
                      kind=g, fields=[f["name"] for f in fl], label=f"single:{g}"))
    if len(edit) >= 2:
        out.append(mk("update_multiple_fields", "Update multiple fields",
                      "change several fields in ONE update, in different combinations: two fields, three fields, one field of "
                      "each kind that exists, every editable field together; every combination a different record. Editable "
                      "fields: " + ", ".join(f["name"] for f in edit) + "."))
    opts = [f for f in groups.get("combobox", []) if len(f.get("options") or []) >= 2]
    if opts:
        out.append(mk("update_options", "Update options",
                      "every dropdown / radio option reached through an update: for each field below one row per option, opening "
                      "a record that currently holds a different option. " + "; ".join(
                          f"{f['name']}: " + " | ".join(f["options"]) for f in opts)))
    bound = [f for f in edit if value_gen.update_group(f) in ("text", "textarea", "number") and (f.get("max_length") or f.get("regex"))]
    if bound:
        out.append(mk("update_boundaries", "Update boundaries",
                      "limits through an update, for the fields that have a limit or a regex: the longest allowed value, "
                      "special characters, internal spaces, the minimum value (numbers). Only values of the pool; never one "
                      "character over the limit. Fields: " + ", ".join(
                          f"{f['name']}" + (f" (max {f['max_length']})" if f.get("max_length") else "") for f in bound) + "."))
    dates = groups.get("date", [])
    if dates:
        calc = [f["name"] for f in facts.get("calculated", []) if f.get("computed_from")]
        out.append(mk("update_dates", "Update dates",
                      "date changes through an update: a past date, today, a future date, and a changed from / to range "
                      "(relative dates only). Date fields: " + ", ".join(f["name"] for f in dates) + "."
                      + (f" After EACH change re-check the calculated field(s) {', '.join(calc)} with  the \"<Field>\" field "
                         "should show  (the number from the rule when it can be worked out, else \"(not empty)\")." if calc else "")))
    required = [f for f in edit if f.get("required") and f.get("required_message")]
    out.append(mk("update_clear", "Update clear",
                  "clear an OPTIONAL field and save (the update succeeds); clear EACH mandatory field on its own and expect that "
                  "field's real message from KNOWN MESSAGES with the form still open. Optional: "
                  + (", ".join(f["name"] for f in edit if f not in required) or "none") + ". Mandatory: "
                  + (", ".join(f"{f['name']} -> {f['required_message']}" for f in required) or "none") + "."))
    out.append(mk("update_no_change", "Update no change",
                  "open a record and click Update without changing anything: the update message of the screen is shown "
                  "(or the form behaves as the source shows); one row per record, different records."))
    return out


def update_kind_of(title: str) -> str:
    t = (title or "").lower()
    for key, pat in (("no_change", "update no change"), ("clear", "update clear"), ("dates", "update dates"),
                     ("boundaries", "update boundaries"), ("options", "update options"),
                     ("multiple_fields", "update multiple fields"), ("single_field", "update single field")):
        if pat in t:
            return key
    return "other"


# --------------------------------------------------------------------------- parsing helpers
_FSTEP_RE = re.compile(r'^I (?:select|enter|set) "([^"]*)" in the "([^"]*)" (picklist|input|combobox|date|radio|checkbox|textarea|new record) field$')
_CLEARSTEP_RE = re.compile(r'^I clear the "([^"]*)" field$')
_OPENSTEP_RE = re.compile(r'^I open the existing record "([^"]*)" using the "([^"]*)" field$')
_CONFSTEP_RE = re.compile(r'^I should see a confirmation message "(.*)"$')


def _sub(text: str, header: list[str], row: list[str]) -> str:
    def rep(m):
        for i, h in enumerate(header):
            if h.strip().lower() == m.group(1).strip().lower():
                return row[i] if i < len(row) else ""
        return m.group(0)
    return re.sub(r"<([^<>]+)>", rep, text or "")


def iter_outlines(feature_text: str):
    """Yields {"title", "steps": [step text without keyword], "header", "rows"} for every Examples table."""
    lines = (feature_text or "").splitlines()
    title, steps, i = "", [], 0
    while i < len(lines):
        s = lines[i].strip()
        m = re.match(r"^Scenario(?:\s+(?:Outline|Template))?\s*:\s*(.*)$", s, re.I)
        if m:
            title, steps = m.group(1).strip(), []
        elif re.match(r"^(Given|When|Then|And|But|\*)\s", s):
            steps.append(re.sub(r"^(Given|When|Then|And|But|\*)\s+", "", s).strip())
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
                yield {"title": title, "steps": list(steps), "header": header, "rows": rows}
                i = k
                continue
        i += 1


def update_rows(feature_text: str) -> list[dict]:
    """One dict per Examples row of every scenario that clicks Update: record, changes {field: value}, cleared [fields],
    message, kind (single_field / multiple_fields / options / boundaries / dates / clear / no_change / other), title."""
    out = []
    for oi, o in enumerate(iter_outlines(feature_text)):
        if not any(re.fullmatch(r"I click the Update button", s) for s in o["steps"]):
            continue
        for r in o["rows"]:
            rec, changes, cleared, msg = "", {}, [], ""
            for st in o["steps"]:
                m = _OPENSTEP_RE.match(st)
                if m:
                    rec = _sub(m.group(1), o["header"], r).strip()
                    continue
                m = _FSTEP_RE.match(st)
                if m:
                    v = _sub(m.group(1), o["header"], r)
                    if v.strip():
                        changes[_sub(m.group(2), o["header"], r)] = v
                    continue
                m = _CLEARSTEP_RE.match(st)
                if m:
                    cleared.append(_sub(m.group(1), o["header"], r))
                    continue
                m = _CONFSTEP_RE.match(st)
                if m:
                    msg = _sub(m.group(1), o["header"], r)
            out.append({"outline": oi, "title": o["title"], "record": rec, "changes": changes, "cleared": cleared,
                        "message": msg, "kind": update_kind_of(o["title"])})
    return out


def apply_update_text(ledger: list[dict], update_text: str) -> list[dict]:
    """Updates the ledger IN PLACE: every row that succeeded sets the record's current values (so the next update pass
    knows what each record holds now). Rows that expect a validation message change nothing."""
    by_key = {r["key"].lower(): r for r in ledger}
    for u in update_rows(update_text):
        rec = by_key.get(u["record"].strip().lower())
        if not rec:
            continue
        msg = u["message"]
        if msg and (re.search(r"should not be empty", msg, re.I) or not re.search(r"success|updated|saved", msg, re.I)):
            continue
        for f, v in u["changes"].items():
            rec["values"][f] = v
        for f in u["cleared"]:
            rec["values"].pop(f, None)
    return ledger


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
    message is a save success. {key, values{FIELD NAME: value}, condition}. Values are keyed by the field name written
    in the steps (not by the column name) so later passes and the update tracking speak about fields."""
    ledger: dict[str, dict] = {}
    for o in iter_outlines(feature_text):
        if not any(re.search(r"click the save button", st, re.I) for st in o["steps"]):
            continue
        header, rows = o["header"], o["rows"]
        low = [h.lower() for h in header]
        er = low.index("expectedresult") if "expectedresult" in low else -1
        lit_msg = next((m.group(1) for m in (_CONFSTEP_RE.match(st) for st in o["steps"]) if m), "")
        col_field: dict[int, str] = {}
        consts: dict[str, str] = {}
        for st in o["steps"]:
            m = _FSTEP_RE.match(st)
            if not m:
                continue
            ph = re.fullmatch(r"<([^<>]+)>", m.group(1).strip())
            if ph and ph.group(1) in header:
                col_field[header.index(ph.group(1))] = m.group(2)
            elif not ph and m.group(1).strip():
                consts[m.group(2)] = m.group(1).strip()
        key_col = None
        if key_field:
            key_col = next((i for i, f in col_field.items() if f == key_field), None)
        if key_col is None and not (key_field and key_field in consts):
            if key_field and key_field in header:
                key_col = header.index(key_field)
            else:
                key_col = next((i for i, h in enumerate(header) if re.search(r"code$|^code|key$", h, re.I)), None)
        if key_col is None and not (key_field and key_field in consts):
            continue
        if er < 0 and not lit_msg:
            continue
        for r in rows:
            if len(r) < len(header):
                continue
            msg = r[er] if er >= 0 else _sub(lit_msg, header, r)
            if not re.search(r"saved successfully", msg, re.I):
                continue
            key = r[key_col] if key_col is not None else consts.get(key_field, "")
            if not key:
                continue
            values = {col_field.get(i, h): r[i] for i, h in enumerate(header) if i not in (0, er) and r[i]}
            for k, v in consts.items():
                values.setdefault(k, v)
            ledger[key.lower()] = {"key": key, "condition": r[0], "values": values}
    return list(ledger.values())


def render_ledger(ledger: list[dict], limit: int = 80) -> str:
    if not ledger:
        return "RECORD LEDGER: no record has been saved yet by earlier passes."
    lines = ["RECORD LEDGER (records that exist at this point; reference ONLY these keys and values, never invent codes):"]
    for rec in ledger[:limit]:
        vals = "; ".join(f"{k}={v}" for k, v in rec["values"].items() if v)
        lines.append(f"- {rec['key']}: {vals}")
    return "\n".join(lines)


def _typed_picklist(f: dict) -> bool:
    return f.get("kind") == "picklist" and (f.get("key_field") or f.get("record_picker")
                                            or (not f.get("lookup") and re.search(r"name|title", f["name"], re.I)))


def _create_step(f: dict, ph: str) -> str | None:
    k, n = f.get("kind") or "input", f["name"]
    if f.get("readonly") or f.get("calculated"):
        return None
    if k == "picklist":
        return f'I enter "{ph}" in the "{n}" new record field' if _typed_picklist(f) else f'I select "{ph}" in the "{n}" picklist field'
    if k == "checkbox":
        return f'I set "{ph}" in the "{n}" checkbox field'
    if k in ("combobox", "radio", "date", "textarea"):
        return f'I {"select" if k in ("combobox", "radio") else "enter"} "{ph}" in the "{n}" {k} field'
    return f'I enter "{ph}" in the "{n}" input field'


def topup_records(facts: dict, ledger: list[dict], need: int) -> tuple[str, list[dict]]:
    """Code-written create scenario that saves `need` more records (a copy of the richest saved record with its own key and
    its own typed names), so that every update row can open a different record. ("", []) when it cannot be built."""
    from service.feature_fixer import bump          # same code-bumping the repair step uses
    key = next((f for f in facts["fields"] if f.get("key_field") and f["kind"] == "picklist"), None)
    if not key or not ledger or need <= 0:
        return "", []
    by_name = {f["name"]: f for f in facts["fields"]}
    template = max(ledger, key=lambda r: len(r["values"]))
    names = [n for n in template["values"] if n in by_name and _create_step(by_name[n], "x")]
    if key["name"] not in names:
        return "", []
    names = [key["name"]] + [n for n in names if n != key["name"]]
    used = {v.lower() for r in ledger for v in r["values"].values()} | {r["key"].lower() for r in ledger}
    new = []
    for i in range(1, need + 1):
        vals = dict(template["values"])
        k = bump(template["key"], used, int(key.get("max_length") or 0))
        used.add(k.lower())
        vals[key["name"]] = k
        for n in names:
            if n != key["name"] and _typed_picklist(by_name[n]) and vals.get(n):
                vals[n] = bump(vals[n], used, int(by_name[n].get("max_length") or 0))
                used.add(vals[n].lower())
        new.append({"key": k, "condition": f"Additional record {i}", "values": vals})
    msg = ((facts["messages"] or {}).get("save") or ["Details Saved Successfully With Code:"])[0]
    lines = ['Scenario Outline: Form core - Records for the later checks (additional) - "<Condition>"',
             "  When I click the Add New button"]
    for n in names:
        lines.append(f"  And {_create_step(by_name[n], '<' + n + '>')}")
    lines += ["  And I click the save button", f'  Then I should see a confirmation message "{msg}"', "",
              "  Examples:", "    | " + " | ".join(["Condition"] + names) + " |"]
    for r in new:
        lines.append("    | " + " | ".join([r["condition"]] + [r["values"].get(n, "") for n in names]) + " |")
    return "\n".join(lines) + "\n", new


# --------------------------------------------------------------------------- ordering / merge
def order_passes(passes_with_text: list[tuple[Pass, str]]) -> list[tuple[Pass, str]]:
    """Stable sort by order_group: create -> read_only -> update -> delete."""
    return sorted(passes_with_text, key=lambda pt: ORDER.get(pt[0].order_group, 9))