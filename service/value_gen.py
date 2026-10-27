"""Test values, scenario plan and the text block for the test-case agent - all built by CODE
from the screen spec (service/screen_spec.py).

The agent may use ONLY the values in the pool, so it never has to invent PLACEHOLDER_ text.
"""
from __future__ import annotations

import random
import re
import secrets

_RND = random.SystemRandom()
_WORDS = ["Alpha", "Beta", "Gamma", "Delta", "Omega", "Prime", "Nova", "Zenith"]


def _n(text) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def _uniq(n: int = 3) -> str:
    return str(secrets.randbelow(10 ** n)).zfill(n)


def _fit(text: str, mx: int | None) -> str:
    return text[:mx].rstrip() if mx else text


def _initials(label: str) -> str:
    letters = "".join(w[0] for w in re.findall(r"[A-Za-z]+", label))[:3].upper()
    return letters or "QA"


def _screen_label(spec: dict) -> str:
    name = spec.get("screen_name") or spec.get("formname") or "Item"
    return re.sub(r"\s+", " ", re.sub(r"[^A-Za-z0-9 ]+", " ", re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name))).strip() or "Item"


def _business_value(ctx: dict | None, f: dict):
    if not isinstance(ctx, dict):
        return None
    wanted = {_n(f["name"]), _n(f["label"]), _n(f["name"].replace(spec_prefix(f), ""))}
    wanted.discard("")
    for k, v in ctx.items():
        nk = _n(k)
        if nk and any(nk == w or nk in w or w in nk for w in wanted) and v not in (None, "", [], {}):
            return str(v if not isinstance(v, (list, tuple)) else v[0])
    return None


def spec_prefix(f: dict) -> str:
    return ""


def _is_code(f: dict) -> bool:
    s = f"{f['name']} {f['label']}".lower()
    return bool(re.search(r"code|number|\bno\b", s))


def _is_name(f: dict) -> bool:
    s = f"{f['name']} {f['label']}".lower()
    return bool(re.search(r"name|title", s))


def _text_values(f: dict, screen: str) -> dict:
    mx = f["max_length"]
    if _is_code(f):
        init = _initials(screen)
        room = (mx - len(init)) if mx else 6
        valid = _fit(f"{init}{_uniq(max(min(4, room), 1))}", mx)
        longest = (init + _uniq(max(room, 1)))[:mx] if mx else None
    elif _is_name(f):
        valid = _fit(f"{screen} {_WORDS[secrets.randbelow(len(_WORDS))]}", mx)
        longest = None
        if mx:
            longest = (valid + " Test Entry" * (mx // 10 + 2))[:mx]
            if longest.endswith(" "):
                longest = longest[:-1] + "X"
    elif f["kind"] == "textarea" or re.search(r"desc|remark|note", f["name"].lower()):
        valid = _fit(f"Automated test entry for {screen}", mx)
        longest = ("Automated test entry " * ((mx or 0) // 21 + 2))[:mx] if mx else None
    else:
        valid = _fit(f"Auto {f['label']} {_uniq(2)}", mx)
        longest = ("Auto " * ((mx or 0) // 5 + 2))[:mx] if mx else None
    return {"valid": valid, "max": longest if (longest and not f["regex"]) else None}


def _number_values(f: dict) -> dict:
    mx = f["max_length"]
    valid = "25" if (mx is None or mx >= 2) else "5"
    return {"valid": valid, "max": ("9" * mx) if mx else None, "invalid": "abc"}


def build_value_pool(spec: dict, business_context: dict | None = None) -> dict:
    """{field NAME: values}. Read-only fields, grids and record pickers are never typed on a new record."""
    screen = _screen_label(spec)
    pool: dict[str, dict] = {}
    for f in spec["fields"]:
        if not f["active"] or f.get("readonly") or f["kind"] == "grid" or f.get("record_picker"):
            continue
        entry = {
            "name": f["name"], "label": f["label"], "kind": f["kind"], "type": f["type"], "required": f["required"],
            "max_length": f["max_length"], "valid": None, "max": None, "invalid": None,
            "options": [], "empty_allowed": True, "required_message": f["required_message"], "note": "",
        }
        opts = [o["text"] for o in f["options"] if not o["blank"] and o["text"].strip()]
        if f["kind"] in ("combobox", "radio") and opts:
            entry.update(options=opts, valid=opts[0])
            entry["note"] = "select the option TEXT shown to the user, never its numeric key"
        elif f["kind"] == "checkbox":
            entry.update(options=["Checked", "Unchecked"], valid="Checked")
        elif f["kind"] == "date":
            entry["valid"] = "today"
            entry["note"] = ("write dates as today, today+N or today-N (N = days); never a literal date: the step types the "
                             "date in the screen's own format" + (f" ({spec['date_format']})" if spec.get("date_format") else ""))
        elif f["lookup"] or f["kind"] == "picklist" and not f["key_field"]:
            real = _business_value(business_context, f)
            entry["valid"] = real if real else f"PLACEHOLDER_{re.sub(r'[^A-Za-z0-9]+', '_', f['label']).strip('_')}"
            entry["note"] = "existing record of another entity: real value from Business Context" if real else \
                "existing record of another entity: no real data found"
        elif f["type"] in ("number", "percentage", "int", "decimal"):
            entry.update(_number_values(f))
        else:
            entry.update(_text_values(f, screen))
            if f["regex"]:
                entry["note"] = f"must match regex {f['regex']}"
        if entry["required"]:
            entry["empty_allowed"] = False
        pool[f["name"]] = entry
    return pool


# --------------------------------------------------------------------------- scenario plan
def build_scenario_plan(spec: dict, pool: dict) -> list[str]:
    msgs = spec.get("messages", {})
    save_frag = (msgs.get("save") or ["Details Saved Successfully"])[0]
    upd_frag = (msgs.get("update") or [""])[0]
    del_frag = (msgs.get("delete") or [""])[0]
    pickers = [f["name"] for f in spec["fields"] if f.get("record_picker") and f["active"]]
    plan = [f"CREATE - all fields valid, leave the record picker{'s' if len(pickers) > 1 else ''} "
            f"({', '.join(pickers) or 'none'}) EMPTY; ExpectedResult: {save_frag}"]
    for name, p in pool.items():
        if p["required"] and p["required_message"]:
            plan.append(f"REQUIRED - leave only {name} ('{p['label']}') empty; ExpectedResult: {p['required_message']}")
    for name, p in pool.items():
        if p["max"] and p["kind"] in ("input", "textarea", "picklist"):
            plan.append(f"MAX LENGTH - {name} with exactly {p['max_length']} characters; ExpectedResult: {save_frag}")
    for name, p in pool.items():
        if p["kind"] in ("combobox", "radio") and len(p["options"]) > 1:
            plan.append(f"EACH OPTION - {name} once per option: {', '.join(p['options'])}; ExpectedResult: {save_frag}")
        if p["kind"] == "checkbox":
            plan.append(f"CHECKBOX - {name} Checked and Unchecked; ExpectedResult: {save_frag}")
    dup = (msgs.get("duplicate") or [""])[0]
    if dup:
        plan.append(f"DUPLICATE - create again with the same code; ExpectedResult fragment: {dup}")
    opener = pickers[0] if pickers else ""
    note = ("only if the save message of this file gives the record's number; if it does not, WRITE NO row for it. "
            "NEVER use a record number from Business Context (that is a real record of your database)")
    if upd_frag or opener:
        plan.append(f"UPDATE - open the record created earlier in this file{(' with ' + opener) if opener else ''} and change "
                    f"a field; ExpectedResult: {upd_frag or save_frag} ({note})")
    if del_frag or opener:
        plan.append(f"DELETE confirm / DELETE cancel - the record created earlier in this file; ExpectedResult: "
                    f"{del_frag or 'Details have been deleted successfully'} ({note})")
    return plan


# --------------------------------------------------------------------------- text for the agent
def render_for_test_cases(spec: dict, pool: dict, plan: list[str]) -> str:
    lines = ["SCREEN SPEC (built by code from the real formjson, the page, the screen's own code and the backend - AUTHORITATIVE)",
             f"formjson: {spec['formname']} | buttons found in the page: {', '.join(spec['buttons']) or 'not listed'}",
             "", "Fields to fill. In EVERY step write the field NAME exactly as given (first word), never its label:"]
    for name, p in pool.items():
        bits = [f"{name} (label '{p['label']}') [{p['kind']}, {p['type']}{', required' if p['required'] else ''}"
                f"{', max ' + str(p['max_length']) if p['max_length'] else ''}]"]
        if p["options"]:
            bits.append("options: " + " | ".join(p["options"]))
        if p["valid"]:
            bits.append(f"valid: {p['valid']}")
        if p["max"]:
            bits.append(f"max-length value: {p['max']}")
        if p["invalid"]:
            bits.append(f"invalid value: {p['invalid']}")
        if p["required_message"]:
            bits.append(f"empty -> {p['required_message']}")
        if p["note"]:
            bits.append(p["note"])
        lines.append("- " + "; ".join(bits))
    skipped = [f["name"] for f in spec["fields"] if f["active"] and (f.get("readonly") or f["kind"] == "grid")
               and not f.get("calculated")]
    if skipped:
        lines.append(f"Shown on the page but never typed (a grid): {', '.join(skipped)}")
    calc = describe_calculated([f for f in spec["fields"] if f["active"]])
    if calc:
        lines.append("CALCULATED / READ-ONLY FIELDS - never filled, never given a data-pool value, never an input step; "
                     "they are only CHECKED with the step  the \"<Field>\" field should show \"<Value>\":")
        lines += calc
    pickers = [f["name"] for f in spec["fields"] if f.get("record_picker") and f["active"]]
    if pickers:
        lines.append(f"Record pickers (they OPEN an existing record - empty when creating): {', '.join(pickers)}")
    alt = [f["name"] for f in spec["fields"] if f.get("alternate_of") and not f["active"]]
    if alt:
        lines.append(f"Not shown for this user/module (an alternate of another field; never use): {', '.join(alt)}")
    if spec.get("not_rendered"):
        lines.append(f"Not on the page (never test): {', '.join(spec['not_rendered'])}")
    if spec["messages"].get("frontend"):
        lines.append("Other messages this screen shows (reference): " + " | ".join(spec["messages"]["frontend"]))
    b = spec.get("backend")
    if b:
        lines += ["", f"BACKEND DIGEST (entity {b['entity']}):",
                  f"- server requires: {', '.join(b['required_fields']) or 'nothing listed'}",
                  f"- set by the server, never typed: {', '.join(b['server_set_fields']) or 'nothing listed'}",
                  f"- auto-number keys: {', '.join(b['auto_number_keys']) or 'none'}"]
        for r in b["rules"]:
            lines.append(f"- backend rule: {r['condition'] or 'always'} -> \"{r['message']}\"")
        for role, m in b["messages"].items():
            lines.append(f"- {role} message: \"{m['template']}\" (assert the fragment \"{m['fragment']}\")")
    lines += ["", "SCENARIO PLAN (cover every line; one Examples row each, grouped into outlines):"]
    lines += [f"{i}. {row}" for i, row in enumerate(plan, 1)]
    return "\n".join(lines)

# --------------------------------------------------------------------------- calculated fields (text for the agents)
def describe_calculated(fields: list[dict]) -> list[str]:
    """One line per calculated / read-only field: where it comes from and what to assert."""
    lines = []
    for f in fields:
        if not f.get("calculated"):
            continue
        src = f.get("computed_from") or []
        rule = f.get("calculated_rule") or {}
        bits = [f"{f['name']} (label '{f.get('label') or f['name']}')"]
        if src:
            bits.append("computed from " + ", ".join(src))
        if isinstance(rule, dict) and rule.get("kind") == "days_between":
            bits.append(f"value = number of days between {rule['from_field']} and {rule['to_field']}"
                        + (" INCLUSIVE (to - from + 1)" if rule.get("inclusive") else " (to - from)")
                        + "; with relative dates the number can be worked out exactly")
        elif src:
            bits.append("exact rule not readable from the code: assert only  \"(not empty)\"")
        else:
            bits.append("read-only: never typed; assert nothing unless the source shows its value")
        lines.append("- " + "; ".join(bits))
    return lines


# --------------------------------------------------------------------------- update data pool
UPDATE_NUMERIC = ("number", "percentage", "int", "integer", "decimal", "numeric", "double", "float", "money", "currency")
UPDATE_MIN_VALUES, UPDATE_MAX_VALUES = 4, 6


def update_group(f: dict) -> str:
    """Kind of an editable field for the update passes: text / textarea / combobox / picklist / date / checkbox / number."""
    k = f.get("kind") or "input"
    if k == "textarea":
        return "textarea"
    if k in ("combobox", "radio"):
        return "combobox"
    if k == "checkbox":
        return "checkbox"
    if k == "date":
        return "date"
    if k == "picklist":
        # a lookup of ANOTHER entity is picked; the screen's own name / title picklist is typed like text
        return "picklist" if f.get("lookup") else "text"
    if str(f.get("type") or "").lower() in UPDATE_NUMERIC:
        return "number"
    return "text"


def is_editable(f: dict) -> bool:
    """A field an update may change: shown, not read-only / calculated, not a grid, not the record key / picker."""
    return (f.get("active", True) is not False and not f.get("readonly") and not f.get("calculated")
            and (f.get("kind") or "") != "grid" and not f.get("record_picker") and not f.get("key_field")
            and not f.get("grid"))


def _fits(v: str, f: dict) -> bool:
    mx = f.get("max_length")
    if not v or (mx and len(v) > mx) or v != v.strip() or any(c in v for c in '"|<>'):
        return False
    rx = f.get("regex")
    if rx:
        try:
            return re.fullmatch(rx, v) is not None
        except re.error:
            return False
    return True


def _text_update_values(f: dict, screen: str) -> list[tuple[str, str]]:
    mx = f.get("max_length")
    w = _RND.sample(_WORDS, 4)
    label = f.get("label") or f["name"]
    if _is_code(f):
        init = _initials(screen)
        typical = [f"{init}{_uniq(3)}", f"{init}{_uniq(3)}X", f"{_initials(label)}{_uniq(3)}"]
    elif _is_name(f):
        typical = [f"{screen} {w[0]} {_uniq(2)}", f"{w[1]} {label} {_uniq(2)}", f"{w[2]} {w[3]} {_uniq(2)}"]
    elif f.get("kind") == "textarea" or re.search(r"desc|remark|note", f["name"].lower()):
        typical = [f"Updated by automated test {w[0]} {_uniq(3)}", f"{w[1]} remark {_uniq(3)}", f"{w[2]} {w[3]} note {_uniq(3)}"]
    else:
        typical = [f"{label} {w[0]} {_uniq(2)}", f"{w[1]} {_uniq(3)}", f"{label} {w[2]} {_uniq(2)}"]
    cands: list[tuple[str, str]] = [("typical", _fit(t, mx)) for t in typical]
    if mx and mx >= 2:
        unit = (typical[0] + " ")
        longest = (unit * (mx // len(unit) + 2))[:mx].rstrip()
        longest = longest + "X" * (mx - len(longest))
        cands.append(("longest allowed", longest))
    if not mx or mx >= 8:
        cands.append(("special characters", _fit(f"{w[0]}&{w[1]}-{_uniq(2)}_(x) #1", mx)))
        cands.append(("internal spaces", _fit(f"{w[2]}  {w[3]} {_uniq(2)}", mx)))
    good = [(c, v) for c, v in cands if _fits(v, f)]
    if not good and f.get("regex"):          # a regex: try plain shapes until one matches
        for c, v in (("typical", "ABC"), ("typical", "AB12"), ("typical", "A123"), ("typical", "Test1"),
                     ("typical", "12345"), ("typical", "abc"), ("typical", "Test Value")):
            if _fits(v, f):
                good.append((c, v))
    return good


def _number_update_values(f: dict) -> list[tuple[str, str]]:
    mx = f.get("max_length")
    decimal = str(f.get("type") or "").lower() in ("decimal", "percentage", "double", "float", "money", "currency")
    base = [("typical", str(_RND.randint(2, 9) * 10 + _RND.randint(1, 9))),
            ("typical", str(_RND.randint(100, 499))),
            ("minimum value", "1")]
    if decimal and (not mx or mx >= 4):
        base.append(("decimal", f"{_RND.randint(1, 90)}.5"))
    if mx and mx >= 2:
        base.append(("longest allowed", "9" * min(mx, 9)))
    else:
        base.append(("typical", str(_RND.randint(1000, 9999))))
    out, seen = [], set()
    for c, v in base:
        if v not in seen and _fits(v, f):
            seen.add(v)
            out.append((c, v))
    return out


def build_update_pool(spec: dict, business_context: dict | None = None) -> dict:
    """{field NAME: {...values: [(category, value)]}} for EVERY editable field - 4 to 6 valid, different values (typical,
    longest, special characters, every option, different dates, different numbers). The update passes use them one per
    row; the repair step replaces a value the record already holds. Built by code from the spec, never invented."""
    screen = _screen_label(spec)
    pool: dict[str, dict] = {}
    for f in spec["fields"]:
        if not f.get("active", True) or not is_editable(f):
            continue
        group = update_group(f)
        vals: list[tuple[str, str]] = []
        note = ""
        opts = [o["text"] for o in f.get("options") or [] if not o.get("blank") and str(o.get("text", "")).strip()]
        if group == "combobox":
            vals = [("option", o) for o in opts]
            note = "every option is listed: use each one, the one the record already shows excluded"
        elif group == "checkbox":
            vals = [("checked", "Checked"), ("unchecked", "Unchecked")]
            note = "toggle: pick the state the record does NOT hold"
        elif group == "date":
            vals = [("today", "today"), ("future", "today+1"), ("future", "today+7"), ("past", "today-1"),
                    ("future", "today+30"), ("past", "today-7")]
            note = ("relative dates only; the step types them in the screen's format"
                    + (f" ({spec['date_format']})" if spec.get("date_format") else "")
                    + "; use a past date only when the source does not forbid it")
        elif group == "picklist":
            real = _business_value(business_context, f)
            vals = [("existing record", real if real else f"PLACEHOLDER_{re.sub(r'[^A-Za-z0-9]+', '_', f['label']).strip('_')}")]
            note = "lookup of another entity: only this value is known - one row per record"
        elif group == "number":
            vals = _number_update_values(f)
        else:
            vals = _text_update_values(f, screen)
        seen, uniq = set(), []
        for c, v in vals:
            if v.lower() not in seen:
                seen.add(v.lower())
                uniq.append((c, v))
        if group != "combobox":
            uniq = uniq[:UPDATE_MAX_VALUES]
        if f.get("regex") and not uniq:
            note = f"regex {f['regex']}: no safe generated value - write values for this field only from the source"
        pool[f["name"]] = {"name": f["name"], "label": f.get("label") or f["name"], "kind": f.get("kind"), "group": group,
                           "max_length": f.get("max_length"), "regex": f.get("regex") or "", "values": uniq, "note": note}
    return pool


def render_update_pool(pool: dict) -> str:
    if not pool:
        return "UPDATE DATA POOL: no editable field."
    lines = ["UPDATE DATA POOL (built by code; use ONLY these values for update rows, a DIFFERENT value on every row; a changed "
             "value must differ from the value the record currently holds in the RECORD LEDGER; never change the record key):"]
    for name, p in pool.items():
        vals = " | ".join(f"{c}: {v}" for c, v in p["values"]) or "none"
        lim = f", max {p['max_length']}" if p["max_length"] else ""
        lines.append(f"- {name} (label '{p['label']}', {p['group']}{lim}): {vals}" + (f"  [{p['note']}]" if p["note"] else ""))
    return "\n".join(lines)