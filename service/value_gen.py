"""Test values, scenario plan and the text block for the test-case agent - all built by CODE
from the screen spec (service/screen_spec.py). Used only when USE_SCREEN_SPEC=1.

The agent may use ONLY the values in the pool, so it never has to invent PLACEHOLDER_ text.
"""
from __future__ import annotations

import re
import secrets

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
            entry["note"] = "the field already shows today's date; the value today keeps it (never type a fixed date)"
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
    skipped = [f["name"] for f in spec["fields"] if f["active"] and (f.get("readonly") or f["kind"] == "grid")]
    if skipped:
        lines.append(f"Shown on the page but never typed (read-only or a grid): {', '.join(skipped)}")
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