"""Deterministic repair of a generated feature file (no AI).

The topic passes write their scenarios at the same time and cannot see each other, so the merged
feature has defects that code can find and fix reliably:

  * the same record code is saved by many scenarios (only the first save can succeed)
  * a NEW record's key is entered with the "picklist field" step (that step picks an EXISTING row)
  * rows that cannot be run on the real screen (an option the dropdown does not have, a field that
    is not on the screen, a filter / sort column / view that does not exist)
  * nonsense or wrongly worded steps ("form open or closed", a bare "filter ... by type")
  * delete scenarios that reuse a record an earlier scenario already deleted
  * the duplicate scenario that expects success twice
  * "open existing" checks that expect values the record was never saved with
  * list scenarios that put search, filter, sort and view into one row of blanks
  * low-value scenarios (rows that repeat the same test, a "read-only" scenario that tests nothing)
  * steps that are not in the step vocabulary (mapped to the right sentence, or removed and logged)
  * input steps / Examples cells aimed at calculated fields (removed, logged); the calculated value is checked instead
  * dates written as literal dates (made relative: today+N), messages written with {placeholders} (fixed part only)
  * update rows that repeat a value the record already holds, or repeat another row (replaced / removed)

Public function:  fix_feature(feature_text, project_analysis, behavior=None, controls=None)
                  -> (new_feature_text, {"notes": [...]})
Any problem inside returns the feature unchanged.
"""
from __future__ import annotations

import copy
import html
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from service import calculated_fields
from service.expected_results import message_fragment

logger = logging.getLogger(__name__)

STANDARD_SAVE = "Details Saved Successfully With Code:"

_STEP_RE = re.compile(r"^(\s*)(Given|When|Then|And|But|\*)\s+(.*\S)\s*$")
_SCENARIO_RE = re.compile(r"^\s*(Scenario\s+Outline|Scenario\s+Template|Scenario|Example)\s*:\s*(.*)$", re.I)
_EXAMPLES_RE = re.compile(r"^\s*(Examples|Scenarios)\s*:", re.I)
_RULE_RE = re.compile(r"^\s*Rule\s*:", re.I)
_BACKGROUND_RE = re.compile(r"^\s*Background\s*:", re.I)
_PH_RE = re.compile(r"<([^<>]+)>")
_FIELD_STEP_RE = re.compile(
    r'^I (select|enter|set) "([^"]*)" in the "([^"]*)" (picklist|input|combobox|date|radio|checkbox|textarea|new record) field$')
_CLEAR_RE = re.compile(r'^I clear the "([^"]*)" field$')
_SHOW_RE = re.compile(r'^the "([^"]*)" field should show "([^"]*)"$')


def _n(t) -> str:
    return re.sub(r"[^a-z0-9]", "", str(t or "").lower())


# =========================================================================== model
@dataclass
class Step:
    kw: str
    text: str


@dataclass
class Table:
    header: list
    rows: list
    label: str = "Examples:"


@dataclass
class Outline:
    title: str
    kind: str = "Scenario Outline"
    steps: list = field(default_factory=list)
    tables: list = field(default_factory=list)
    pre: list = field(default_factory=list)


@dataclass
class Raw:
    lines: list


@dataclass
class RuleLine:
    line: str


def _cells(line: str) -> list:
    s = line.strip()
    s = s[1:] if s.startswith("|") else s
    s = s[:-1] if s.endswith("|") else s
    return [c.strip() for c in re.split(r"(?<!\\)\|", s)]


def parse(text: str) -> list:
    items: list = []
    lines = (text or "").splitlines()
    raw: list = []
    cur: Optional[Outline] = None
    tbl: Optional[Table] = None
    pending_pre: list = []
    i = 0

    def flush_raw():
        nonlocal raw
        if raw and any(l.strip() for l in raw):
            while raw and not raw[-1].strip():
                raw.pop()
            items.append(Raw(raw))
        raw = []

    while i < len(lines):
        line = lines[i]
        s = line.strip()
        m = _SCENARIO_RE.match(line)
        if m:
            if cur is None:
                # tags written right above the scenario stay with it
                while raw and raw[-1].strip().startswith("@"):
                    pending_pre.insert(0, raw.pop())
                flush_raw()
            cur = Outline(title=m.group(2).strip(), kind=re.sub(r"\s+", " ", m.group(1)).title().replace("Outline", "Outline"),
                          pre=pending_pre)
            pending_pre = []
            items.append(cur)
            tbl = None
            i += 1
            continue
        if _RULE_RE.match(line):
            cur, tbl = None, None
            flush_raw()
            items.append(RuleLine(line.strip()))
            i += 1
            continue
        if cur is None:
            raw.append(line)
            i += 1
            continue
        if _EXAMPLES_RE.match(line):
            tbl = Table(header=[], rows=[], label=s)
            cur.tables.append(tbl)
            i += 1
            continue
        if s.startswith("|") and tbl is not None:
            cells = _cells(line)
            if not tbl.header:
                tbl.header = cells
            else:
                tbl.rows.append(cells)
            i += 1
            continue
        if s.startswith("@") and cur is not None and tbl is not None:
            pending_pre.append(line)
            i += 1
            continue
        sm = _STEP_RE.match(line)
        if sm and tbl is None:
            cur.steps.append(Step(sm.group(2), sm.group(3)))
        i += 1
    flush_raw()
    return items


def _render_table(t: Table, indent: str) -> list:
    rows = [t.header] + t.rows
    width = max(len(r) for r in rows) if rows else 0
    rows = [r + [""] * (width - len(r)) for r in rows]
    w = [max(len(r[c]) for r in rows) for c in range(width)]
    return [indent + "| " + " | ".join(r[c].ljust(w[c]) for c in range(width)) + " |" for r in rows]


def render(items: list) -> str:
    out: list = []
    for it in items:
        if isinstance(it, Raw):
            out.extend(it.lines)
            out.append("")
        elif isinstance(it, RuleLine):
            out.append(it.line)
            out.append("")
        elif isinstance(it, Outline):
            out.extend(it.pre)
            out.append(f"  {it.kind}: {it.title}")
            for st in it.steps:
                out.append(f"    {st.kw} {st.text}")
            for t in it.tables:
                out.append("")
                out.append(f"    {t.label}")
                out.extend(_render_table(t, "      "))
            out.append("")
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out) + "\n"


# =========================================================================== context
class Ctx:
    def __init__(self, project_analysis, behavior, controls):
        pa = project_analysis if isinstance(project_analysis, dict) else {}
        self.spec = pa.get("screen_spec") or {}
        self.behavior = behavior or pa.get("behavior") or {}
        self.fields: list = []
        for f in self.spec.get("fields") or []:
            if not isinstance(f, dict) or not f.get("active", True) or f.get("grid"):
                continue
            texts, blank = [], None
            for o in f.get("options") or []:
                if isinstance(o, dict):
                    if o.get("blank"):
                        blank = blank or (o.get("text") or None)
                    elif str(o.get("text", "")).strip():
                        texts.append(str(o["text"]).strip())
                elif str(o).strip():
                    texts.append(str(o).strip())
            self.fields.append({
                "name": f["name"], "label": f.get("label") or f["name"], "kind": f.get("kind") or "input",
                "options": texts, "blank": blank, "key": bool(f.get("key_field")),
                "record_picker": bool(f.get("record_picker")), "readonly": bool(f.get("readonly")),
                "max_length": f.get("max_length"), "lookup": bool(f.get("lookup")),
                "required": bool(f.get("required")), "required_message": str(f.get("required_message") or ""),
                "calculated": bool(f.get("calculated")), "computed_from": list(f.get("computed_from") or []),
                "rule": f.get("calculated_rule") or {},
            })
        if not self.fields:   # no spec: the scan-verified controls give names and kinds only
            for base, c in (controls or {}).items():
                if isinstance(c, dict):
                    self.fields.append({"name": base, "label": base, "kind": c.get("kind") or "input", "options": [],
                                        "blank": None, "key": False, "record_picker": False, "readonly": False,
                                        "max_length": None, "lookup": False,
                                        "required": False, "required_message": "",
                                        "calculated": False, "computed_from": [], "rule": {}})
        self.by_norm = {}
        for f in self.fields:
            self.by_norm[_n(f["name"])] = f
            self.by_norm.setdefault(_n(f["label"]), f)
        keys = [f for f in self.fields if f["key"] and f["kind"] == "picklist"]
        self.key_field = keys[0]["name"] if keys else None
        # picklists whose value is TYPED for a new record (the record's own key / name), not picked from another entity
        self.typed = {f["name"] for f in self.fields
                      if f["kind"] == "picklist" and (f["key"] or f["record_picker"]
                                                      or (not f["lookup"] and re.search(r"name|title", f["name"], re.I)))}
        msgs = self.spec.get("messages") or {}
        self.messages = msgs if isinstance(msgs, dict) else {}
        self.save_msg = (self.messages.get("save") or [STANDARD_SAVE])[0]
        dup = self.messages.get("duplicate") or []
        self.dup_msg = dup[0] if dup else ""
        self.date_format = str(self.spec.get("date_format") or "")
        self.update_pool: dict = {}
        try:
            from service import value_gen as _vg
            if self.spec.get("fields"):
                self.update_pool = _vg.build_update_pool(self.spec)
        except Exception:  # noqa: BLE001
            logger.exception("feature_fixer: update pool unavailable - repeated update values are only reported")
        self.notes: list = []
        self.nav: list = []
        try:
            from service import datacy_scan as _ds
            page = ((pa.get("modules") or [{}])[0].get("pages") or [{}])[0]
            self.nav = [n for n in _ds._clean_path(page.get("navigation_path")) if n]
        except Exception:  # noqa: BLE001
            self.nav = []
        try:
            from service import template_steps as _ts
            self.aliases = _ts.load_aliases()
        except Exception:  # noqa: BLE001
            self.aliases = {}

    def field(self, name: str):
        return self.by_norm.get(_n(re.sub(r"-(PickListDrop|PickList|ComboBox|Input|Textarea|DatePicker|RadioBox|CheckBox|UnCheckBox)$", "", str(name))))

    def known(self, name: str) -> bool:
        return (not self.fields) or self.field(name) is not None

    def note(self, text: str) -> None:
        self.notes.append(text)


# =========================================================================== helpers
def _fs(step: Step):
    m = _FIELD_STEP_RE.match(step.text)
    return (m.group(1), m.group(2), m.group(3), m.group(4)) if m else None


def _has(o: Outline, rx: str) -> bool:
    return any(re.search(rx, s.text, re.I) for s in o.steps)


def _row_map(t: Table, row: list) -> dict:
    return {h: (row[i] if i < len(row) else "") for i, h in enumerate(t.header)}


def _resolve(text: str, rm: dict) -> str:
    return _PH_RE.sub(lambda m: rm.get(m.group(1), m.group(0)), text)


def _placeholders_used(o: Outline) -> set:
    used = set()
    for s in o.steps:
        used.update(_PH_RE.findall(s.text))
    return used


def _col(t: Table, name: str) -> int:
    for i, h in enumerate(t.header):
        if h == name:
            return i
    for i, h in enumerate(t.header):
        if _n(h) == _n(name):
            return i
    return -1


def _is_create(o: Outline, ctx: Ctx) -> bool:
    return bool(ctx.key_field) and _has(o, r"^I click the Add New button$") and _has(o, r"^I click the save button$") \
        and any((_fs(s) and _fs(s)[2] == ctx.key_field) for s in o.steps)


def _success_rows(o: Outline, t: Table) -> list:
    """Row indexes of a create outline whose save is expected to succeed."""
    save_i = next((i for i, s in enumerate(o.steps) if re.match(r"^I click the save button$", s.text)), None)
    conf = None
    if save_i is not None:
        conf = next((s for s in o.steps[save_i + 1:] if re.match(r'^I should see a confirmation message "', s.text)), None)
    if conf is None:
        # no message is asserted: the save counts as successful unless the scenario expects the form to stay open
        if save_i is not None and not any(re.match(r"^the form should still be open$", x.text) for x in o.steps[save_i + 1:]):
            return list(range(len(t.rows)))
        return []
    out = []
    for r, row in enumerate(t.rows):
        msg = _resolve(re.match(r'^I should see a confirmation message "(.*)"$', conf.text).group(1), _row_map(t, row))
        if re.search(r"saved successfully", msg, re.I):
            out.append(r)
    return out


def bump(value: str, used: set, max_len: int = 0) -> str:
    """Another value of the same shape that is not in `used` (SD4223 -> SD4224, 'SD 4223-#' -> 'SD 4224-#')."""
    m = re.match(r"^(.*?)(\d+)(\D*)$", value or "")
    if m:
        pre, digits, post = m.groups()
        n = int(digits)
        for i in range(1, 5000):
            cand = f"{pre}{str(n + i).zfill(len(digits))}{post}"
            if max_len and len(cand) > max_len:
                break
            if cand.lower() not in used:
                return cand
    base = (value or "X")
    if max_len:
        base = base[: max(1, max_len - 3)]
    for i in range(2, 5000):
        cand = f"{base}{i}"
        if cand.lower() not in used and (not max_len or len(cand) <= max_len):
            return cand
    return value


def _drop(items: list, o) -> None:
    if o in items:
        items.remove(o)


def _outlines(items: list) -> list:
    return [i for i in items if isinstance(i, Outline)]


# =========================================================================== passes
def _p_background(items, ctx):
    bg_texts = set()
    for it in items:
        if isinstance(it, Raw):
            in_bg, first = False, True
            for k, line in enumerate(it.lines):
                if _BACKGROUND_RE.match(line):
                    in_bg, first = True, True
                    continue
                sm = _STEP_RE.match(line)
                if in_bg and sm:
                    if first and sm.group(2) in ("And", "But", "When", "Then", "*"):
                        it.lines[k] = f"{sm.group(1)}Given {sm.group(3)}"
                        ctx.note("Background: first step now starts with Given")
                    first = False
                    if sm.group(3).startswith("I navigate through the following path"):
                        bg_texts.add(sm.group(3))
                elif in_bg and line.strip() and not sm:
                    in_bg = False
    if not bg_texts:
        return
    removed = 0
    for o in _outlines(items):
        keep = []
        for s in o.steps:
            if s.text in bg_texts:
                removed += 1
            else:
                keep.append(s)
        o.steps = keep
    if removed:
        ctx.note(f"removed {removed} navigation step(s) repeated from the Background")


_NORMALIZE = [
    (re.compile(r"^I should (?:still )?see the form open or closed$", re.I), None),
    (re.compile(r"^I should still see the form open$", re.I), "the form should still be open"),
    (re.compile(r"^I should (?:still )?see the form open$", re.I), "the form should be open"),
    (re.compile(r"^I should (?:still )?see the form closed$", re.I), "the form should be closed"),
    (re.compile(r"^the form should still be closed$", re.I), "the form should be closed"),
    (re.compile(r"^I filter the list by type\s*$", re.I), None),
    (re.compile(r'^I filter the list by type ""$', re.I), None),
]


def _normalize_text(text: str):
    t = text.strip()
    for rx, rep in _NORMALIZE:
        if rx.match(t):
            return rep
    m = re.match(r'^I clear the ([A-Za-z_]\w*) field$', t)
    if m:
        return f'I clear the "{m.group(1)}" field'
    m = re.match(r'^I clear the "([^"]*)" (?:input|picklist|combobox|textarea|date|radio|checkbox) field$', t)
    if m:
        return f'I clear the "{m.group(1)}" field'
    m = re.match(r'^I should see "([^"]*)" in the "([^"]*)" field$', t)
    if m:
        return f'the "{m.group(2)}" field should show "{m.group(1)}"'
    m = re.match(r'^the "([^"]*)" action should be (visible|hidden)$', t)
    if m:
        return f'the "{m.group(1)}" action should be "{m.group(2)}"'
    m = re.match(r"^the (Save|Delete|Update|AddNew|Edit)Form action should be (visible|hidden)$", t)
    if m:
        name = "Add New" if m.group(1) == "AddNew" else m.group(1)
        return f'the "{name}" action should be "{m.group(2)}"'
    m = re.match(r'^(I sort the list by the "[^"]*" column in) (asc|desc|ascending|descending) order$', t, re.I)
    if m:
        return f'{m.group(1)} "{m.group(2)[:4].lower().rstrip("e")}" order'.replace('"asc"', '"asc"')
    m = re.match(r'^(the list should be sorted by "[^"]*" in) (asc|desc|ascending|descending) order$', t, re.I)
    if m:
        return f'{m.group(1)} "{m.group(2)[:4].lower().rstrip("e")}" order'
    return text


_LABEL_PREFIX_RE = re.compile(r"^\[[A-Za-z_ ]{3,20}\]\s*")


def _p_normalize_steps(items, ctx):
    n = 0
    for o in _outlines(items):
        # "[required] Code* Should not be Empty": the bracket is only the label of the known-message list
        for s in o.steps:
            m = re.match(r'^(I should see a confirmation message ")\[[A-Za-z_ ]{3,20}\]\s*(.*)$', s.text)
            if m:
                s.text = m.group(1) + m.group(2)
                n += 1
        for t in o.tables:
            for ci, h in enumerate(t.header):
                if "expected" in h.lower():
                    for row in t.rows:
                        if ci < len(row) and _LABEL_PREFIX_RE.match(row[ci]):
                            row[ci] = _LABEL_PREFIX_RE.sub("", row[ci])
                            n += 1
        out = []
        for s in o.steps:
            new = _normalize_text(s.text)
            if new is None:
                n += 1
                continue
            if new != s.text:
                n += 1
                s = Step(s.kw, new)
            out.append(s)
        o.steps = out
    if n:
        ctx.note(f"reworded or removed {n} step(s) that did not match the step vocabulary")


def _p_unknown_fields(items, ctx):
    if not ctx.fields:
        return
    removed = 0
    for o in list(_outlines(items)):
        keep = []
        for s in o.steps:
            name = None
            f = _fs(s)
            if f:
                name = f[2]
            else:
                m = _CLEAR_RE.match(s.text) or _SHOW_RE.match(s.text)
                if m:
                    name = m.group(1)
            if name and "<" not in name and not ctx.known(name):
                removed += 1
                continue
            keep.append(s)
        o.steps = keep
        if not o.steps:
            _drop(items, o)
    if removed:
        ctx.note(f"removed {removed} step(s) for fields that are not on this screen")


def _p_options(items, ctx):
    dropped_rows = 0
    for o in list(_outlines(items)):
        # a literal empty value for a combobox is a silent no-op: use the "Select ..." option instead
        for s in o.steps:
            f = _fs(s)
            if f and f[1] == "" and f[3] == "combobox":
                fld = ctx.field(f[2])
                if fld and fld["blank"]:
                    s.text = f'I select "{fld["blank"]}" in the "{f[2]}" combobox field'
                    ctx.note(f'an empty combobox value for {f[2]} now selects "{fld["blank"]}" (an empty value is skipped by the step)')
                elif fld:
                    _drop(items, o)
                    ctx.note(f'dropped "{o.title}": the combobox has no blank option to reset to')
                    break
        else:
            checks = []
            for s in o.steps:
                f = _fs(s)
                if f and f[3] in ("combobox", "radio"):
                    fld = ctx.field(f[2])
                    if fld and fld["options"]:
                        checks.append((f[1], fld))
            if not checks:
                continue
            tables = o.tables or []
            if not tables:
                bad = [v for v, fld in checks if v and "<" not in v and _n(v) not in {_n(x) for x in fld["options"]} | {_n(fld["blank"])}]
                if bad:
                    _drop(items, o)
                    ctx.note(f'dropped "{o.title}": option "{bad[0]}" is not in the dropdown')
                continue
            for t in tables:
                keep = []
                for row in t.rows:
                    rm = _row_map(t, row)
                    ok = True
                    for v, fld in checks:
                        val = _resolve(v, rm).strip()
                        allowed = {_n(x) for x in fld["options"]} | ({_n(fld["blank"])} if fld["blank"] else set())
                        if val and _n(val) not in allowed:
                            ok = False
                            ctx.note(f'dropped a row of "{o.title}": "{val}" is not an option of {fld["name"]} ({", ".join(fld["options"])})')
                            break
                    if ok:
                        keep.append(row)
                    else:
                        dropped_rows += 1
                t.rows = keep
            if all(not t.rows for t in tables):
                _drop(items, o)


def _p_update_key(items, ctx):
    """Picking another code in the key picklist switches to that record - it does not rename the open record."""
    if not ctx.key_field:
        return
    for o in list(_outlines(items)):
        if not _has(o, r"^I click the Update button$"):
            continue
        key_steps = [s for s in o.steps if _fs(s) and _fs(s)[2] == ctx.key_field]
        if not key_steps:
            continue
        cols = {m for s in key_steps for m in _PH_RE.findall(s.text)}
        o.steps = [s for s in o.steps if s not in key_steps]
        for t in o.tables:
            keep = []
            for row in t.rows:
                cond = row[0] if row else ""
                if re.search(re.escape(ctx.key_field), cond, re.I) and re.search(r"\b(alone|only)\b", cond, re.I):
                    ctx.note(f'dropped row "{cond}": the key cannot be changed on an open record')
                    continue
                keep.append(row)
            t.rows = keep
            still = _placeholders_used(o) | set(_PH_RE.findall(o.title))
            for c in cols:
                ci = _col(t, c)
                if ci > 0 and c not in still:
                    t.header.pop(ci)
                    for r in t.rows:
                        if ci < len(r):
                            r.pop(ci)
        if all(not t.rows for t in o.tables) and o.tables:
            _drop(items, o)
        elif not o.tables and not any(re.match(r"^I (select|enter|set)", s.text) for s in o.steps):
            pass


def _p_update_typed(items, ctx):
    """On an open record, picking from the name picklist would switch to another record (or fail when the
    value is new): the new value is typed instead."""
    n = 0
    for o in _outlines(items):
        if not (_has(o, r"^I click the Update button$") and _has(o, r"^I open the existing record")):
            continue
        for s in o.steps:
            f = _fs(s)
            if f and f[3] == "picklist" and f[0] == "select" and f[2] in ctx.typed and f[2] != ctx.key_field:
                s.text = f'I enter "{f[1]}" in the "{f[2]}" new record field'
                n += 1
    if n:
        ctx.note(f"{n} name picklist step(s) in update scenarios now type the new value instead of picking another record")


def _p_clear_rows(items, ctx):
    for o in list(_outlines(items)):
        if not any(_CLEAR_RE.match(s.text) for s in o.steps):
            continue
        for t in o.tables:
            fcol = next((c for s in o.steps for c in _PH_RE.findall(s.text) if _CLEAR_RE.match(s.text)), None)
            ci = _col(t, fcol) if fcol else -1
            if ci < 0:
                continue
            keep = []
            for row in t.rows:
                fld = ctx.field(row[ci]) if ci < len(row) else None
                if fld and (fld["name"] == ctx.key_field or fld["readonly"]):
                    ctx.note(f'dropped row "{row[0]}": {fld["name"]} cannot be cleared on an open record')
                    continue
                keep.append(row)
            t.rows = keep
        if o.tables and all(not t.rows for t in o.tables):
            _drop(items, o)


def _p_new_record_steps(items, ctx):
    n = 0
    for o in _outlines(items):
        if not _has(o, r"^I click the Add New button$"):
            continue
        for s in o.steps:
            f = _fs(s)
            if f and f[3] == "picklist" and f[0] == "select" and f[2] in ctx.typed:
                s.text = f'I enter "{f[1]}" in the "{f[2]}" new record field'
                n += 1
    if n:
        ctx.note(f"{n} key/name picklist step(s) for NEW records now use the 'new record field' step")


def _signature(o: Outline, t: Table) -> tuple:
    return (tuple(s.text for s in o.steps), tuple(t.header), tuple(tuple(r) for r in t.rows))


def _p_collapse(items, ctx):
    seen_sig = set()
    for o in list(_outlines(items)):
        used = _placeholders_used(o)
        cond_is_data = "Condition" in used          # e.g. open the record "<Condition>": the label IS the test data
        for t in o.tables:
            # identical rows (the Condition label only counts when the steps use it)
            seen, keep = set(), []
            label_col = bool(t.header) and t.header[0].lower() == "condition" and not cond_is_data
            for row in t.rows:
                sig = tuple(c.strip().lower() for c in (row[1:] if label_col else row))
                if sig in seen:
                    continue
                seen.add(sig)
                keep.append(row)
            if len(keep) != len(t.rows):
                ctx.note(f'removed {len(t.rows) - len(keep)} repeated row(s) in "{o.title}"')
            t.rows = keep
            # rows that cannot differ: the steps use no column of the table at all
            if len(t.rows) > 1 and not used:
                ctx.note(f'"{o.title}": rows use no placeholder, kept 1 of {len(t.rows)}')
                t.rows = t.rows[:1]
        if o.tables:
            sig = _signature(o, o.tables[0])
            sig = (sig[0], sig[1][1:], tuple(r[1:] for r in sig[2])) if not cond_is_data else sig
            if sig in seen_sig:
                _drop(items, o)
                ctx.note(f'dropped "{o.title}": same steps and rows as an earlier scenario')
                continue
            seen_sig.add(sig)


_SUCCESS_RX = re.compile(r"(saved|updated|deleted) successfully", re.I)


def _p_duplicate_outline(items, ctx):
    for o in _outlines(items):
        if not re.search(r"duplicate", o.title, re.I):
            continue
        saves = [i for i, s in enumerate(o.steps) if re.match(r"^I click the save button$", s.text)]
        adds = [i for i, s in enumerate(o.steps) if re.match(r"^I click the Add New button$", s.text)]
        if len(saves) < 2:
            continue
        if len(adds) == 1:
            # the second save re-saves an OPENED record: that is an update, not a duplicate insert.
            # Rebuild the second half as a second "Add New" with the same values.
            open_i = next((i for i, s in enumerate(o.steps) if i > saves[0] and re.match(r"^I open the existing record", s.text)), None)
            if open_i is None:
                continue
            first_block = o.steps[adds[0] + 1:saves[0]]
            confs = [s for s in o.steps[saves[1] + 1:] if re.match(r'^I should see a confirmation message "', s.text)]
            tail = [Step("When", "I click the Add New button")] + [Step("And", s.text) for s in first_block] \
                + [Step("And", "I click the save button")]
            tail.append(Step("Then", confs[-1].text if confs else 'I should see a confirmation message "' + (ctx.dup_msg or "") + '"'))
            o.steps = o.steps[:open_i] + tail
            saves = [i for i, s in enumerate(o.steps) if re.match(r"^I click the save button$", s.text)]
            ctx.note("duplicate scenario: the second half is now a second 'Add New' with the same code (it used to re-save the opened record, which is an update)")
        for s in o.steps[saves[1] + 1:]:
            m = re.match(r'^I should see a confirmation message "(.*)"$', s.text)
            if m and (_SUCCESS_RX.search(m.group(1)) or not m.group(1).strip()):
                if ctx.dup_msg:
                    s.text = f'I should see a confirmation message "{ctx.dup_msg}"'
                    ctx.note("duplicate scenario: the second save now expects the duplicate message")
                else:
                    s.text = "the form should still be open"
                    ctx.note("duplicate scenario: no duplicate message is known, the second save now only checks the form stays open")
                break


def _p_unique_keys(items, ctx):
    if not ctx.key_field:
        ctx.note("no key picklist known: record codes were not made unique")
        return
    kf = next((f for f in ctx.fields if f["name"] == ctx.key_field), {})
    cap = int(kf.get("max_length") or 0)
    used: set = set()
    changed = 0
    for o in _outlines(items):
        if not _is_create(o, ctx):
            continue
        key_step = next(s for s in o.steps if _fs(s) and _fs(s)[2] == ctx.key_field)
        verb, val, _f, _k = _fs(key_step)
        if not o.tables:
            continue
        t = o.tables[0]
        m = re.fullmatch(r"<([^<>]+)>", val)
        if m:
            ci = _col(t, m.group(1))
        else:
            # a literal key: move it into a column so every row can get its own code
            name = ctx.key_field if _col(t, ctx.key_field) < 0 else ctx.key_field + "Value"
            t.header.append(name)
            for row in t.rows:
                row.append(val)
            key_step.text = key_step.text.replace(f'"{val}"', f'"<{name}>"', 1)
            ci = len(t.header) - 1
        if ci < 0:
            continue
        good = set(_success_rows(o, t))
        for r, row in enumerate(t.rows):
            if r not in good:
                continue
            cur = row[ci].strip()
            if not cur:
                continue
            if cur.lower() in used:
                new = bump(cur, used, cap)
                if new != cur:
                    row[ci] = new
                    changed += 1
                cur = new
            used.add(cur.lower())
    ctx.saved_keys = used
    if changed:
        ctx.note(f"{changed} saved record code(s) were repeated by other scenarios and now have their own code")


def _collect_records(items, ctx) -> list:
    """Records the create scenarios save, in file order: {key, fields{name: value}}."""
    out, seen = [], set()
    if not ctx.key_field:
        return out
    for o in _outlines(items):
        if not _is_create(o, ctx) or not o.tables:
            continue
        t = o.tables[0]
        for r in _success_rows(o, t):
            rm = _row_map(t, t.rows[r])
            fields = {}
            for s in o.steps:
                f = _fs(s)
                if f:
                    v = _resolve(f[1], rm).strip()
                    if v:
                        fields[f[2]] = v
            key = fields.get(ctx.key_field, "")
            if key and key.lower() not in seen and "|" not in key:
                seen.add(key.lower())
                out.append({"key": key, "fields": fields})
    return out


def _field_step(ctx: Ctx, fname: str, ph: str) -> Optional[str]:
    f = ctx.field(fname)
    if not f or f["readonly"]:
        return None
    k = f["kind"]
    if k == "picklist":
        return (f'I enter "{ph}" in the "{fname}" new record field' if fname in ctx.typed
                else f'I select "{ph}" in the "{fname}" picklist field')
    if k == "checkbox":
        return f'I set "{ph}" in the "{fname}" checkbox field'
    if k in ("combobox", "radio", "date", "textarea"):
        verb = "select" if k in ("combobox", "radio") else "enter"
        return f'I {verb} "{ph}" in the "{fname}" {k} field'
    return f'I enter "{ph}" in the "{fname}" input field'


def _make_create_outline(ctx: Ctx, title: str, recs: list, label: str, labels: Optional[list] = None) -> Optional[Outline]:
    if not recs:
        return None
    names = [f["name"] for f in ctx.fields if not f["readonly"] and any(f["name"] in r["fields"] for r in recs)]
    if not names:
        return None
    o = Outline(title=f'{title} - "<Condition>"')
    o.steps.append(Step("When", "I click the Add New button"))
    for nm in names:
        st = _field_step(ctx, nm, f"<{nm}>")
        if st:
            o.steps.append(Step("And", st))
    o.steps.append(Step("And", "I click the save button"))
    o.steps.append(Step("Then", f'I should see a confirmation message "{ctx.save_msg}"'))
    t = Table(header=["Condition"] + names, rows=[])
    for i, r in enumerate(recs, 1):
        t.rows.append([(labels[i - 1] if labels and i - 1 < len(labels) else f"{label} {i}")]
                      + [r["fields"].get(nm, "") for nm in names])
    o.tables.append(t)
    return o


def _record_key_refs(o: Outline) -> list:
    """(step, key text) of steps that open / delete an existing record."""
    refs = []
    for s in o.steps:
        m = re.match(r'^I (?:open the existing|request to delete the) record "([^"]*)" using the "([^"]*)" field$', s.text)
        if m:
            refs.append((s, m.group(1)))
    return refs


def _is_destructive(o: Outline) -> bool:
    return _has(o, r"^I confirm the delete$") or _has(o, r"^I click the Delete button$")


def _replace_key(o: Outline, old: str, new: str) -> None:
    rx = re.compile(r"(?<![A-Za-z0-9])" + re.escape(old) + r"(?![A-Za-z0-9])")
    o.title = rx.sub(new, o.title)
    for s in o.steps:
        s.text = rx.sub(lambda m: new, s.text)
    for t in o.tables:
        for row in t.rows:
            for i, c in enumerate(row):
                if c.strip() == old:
                    row[i] = new


def _p_delete_flow(items, ctx):
    n_btn = 0
    for o in _outlines(items):
        for i, s in enumerate(o.steps):
            if s.text == "I click the Delete button" and any(
                    re.match(r"^I should see the delete prompt|^I confirm the delete|^I cancel the delete", x.text) for x in o.steps[i + 1:]):
                s.text = "I request to delete the open record"
                n_btn += 1
    if n_btn:
        ctx.note(f"{n_btn} 'I click the Delete button' step(s) before a prompt check now only press Delete (the template step also confirms)")
    outs = _outlines(items)
    if not any(_is_destructive(o) for o in outs):
        return
    if not any(isinstance(i, RuleLine) for i in items):
        keep = [i for i in items if not (isinstance(i, Outline) and _is_destructive(i))]
        dest = [i for i in items if isinstance(i, Outline) and _is_destructive(i)]
        if dest and keep != [i for i in items if i not in dest]:
            pass
        new_items = [i for i in items if i not in dest] + dest
        if new_items != items:
            ctx.note("scenarios that delete a record moved to the end, after every scenario that still needs the records")
        items[:] = new_items
    records = _collect_records(items, ctx)
    known = {r["key"].lower() for r in records}
    used = set(getattr(ctx, "saved_keys", set())) | known
    kf = next((f for f in ctx.fields if f["name"] == ctx.key_field), {})
    cap = int(kf.get("max_length") or 0)
    used_vals = {v.lower() for r in records for v in r["fields"].values()}      # codes AND names already taken
    deleted: set = set()
    fresh: list = []
    first_dest = None
    for o in [i for i in items if isinstance(i, Outline) and _is_destructive(i)]:
        if first_dest is None:
            first_dest = o
        refs = _record_key_refs(o)
        if not refs:
            continue
        s0, k0 = refs[0]
        t = o.tables[0] if o.tables else None
        ph = re.fullmatch(r"<([^<>]+)>", k0)
        rows = list(range(len(t.rows))) if (t and ph) else [None]
        for r in rows:
            key = (t.rows[r][_col(t, ph.group(1))] if r is not None else k0).strip()
            if not key:
                continue
            if key.lower() in deleted or key.lower() not in known:
                template = next((x for x in records if x["key"].lower() == key.lower()), records[0] if records else None)
                if template is None:
                    continue
                new = bump(key, used, cap)
                used.add(new.lower())
                fields = dict(template["fields"])
                fields[ctx.key_field] = new
                for nm in ctx.typed:
                    if nm != ctx.key_field and nm in fields:
                        fields[nm] = bump(fields[nm], used | used_vals, 0)
                        used_vals.add(fields[nm].lower())
                fresh.append({"key": new, "fields": fields})
                if r is None:
                    _replace_key(o, key, new)
                else:
                    t.rows[r][_col(t, ph.group(1))] = new
                key = new
            deleted.add(key.lower())
    if fresh:
        outline = _make_create_outline(ctx, "Records for the delete checks", fresh, "DeleteRecord")
        if outline is not None:
            idx = items.index(first_dest) if first_dest in items else len(items)
            items.insert(idx, outline)
            ctx.note(f"added {len(fresh)} record(s) for the delete scenarios, so no scenario deletes a record that is already gone")


def _p_open_existing(items, ctx):
    targets = [o for o in _outlines(items)
               if re.match(r"^Open existing", o.title, re.I) or (_has(o, r"^I open the existing record") and any(_SHOW_RE.match(s.text) for s in o.steps) and not _has(o, r"Update button"))]
    if not targets or not ctx.key_field:
        return
    records = [r for r in _collect_records(items, ctx) if len(r["fields"]) >= 3]
    if not records:
        return
    recs = records[:4]
    names = [f["name"] for f in ctx.fields if not f["readonly"] and any(f["name"] in r["fields"] for r in recs)]
    o = Outline(title='Open existing - Load each existing record and verify display - "<Condition>"')
    o.steps.append(Step("When", f'I open the existing record "<{ctx.key_field}>" using the "{ctx.key_field}" field'))
    o.steps.append(Step("Then", "the form should be open"))
    for nm in names:
        o.steps.append(Step("And", f'the "{nm}" field should show "<{nm}>"'))
    t = Table(header=["Condition"] + names, rows=[])
    for r in recs:
        t.rows.append([f"Existing {r['key']}"] + [r["fields"].get(nm, "") for nm in names])
    o.tables.append(t)
    at = items.index(targets[0])
    items[at] = o
    for x in targets[1:]:
        _drop(items, x)
    ctx.note(f"'Open existing' rebuilt from the {len(recs)} record(s) the file really saves (values match what was saved)")


# --------------------------------------------------------------------------- list behaviour (rebuilt from screen facts)
def _clean_label(t: str) -> str:
    """'Code &#9650; &#9660;' (header with sort arrows) -> 'Code'."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w ]", " ", html.unescape(t or ""))).strip()


def _p_list_behaviour(items, ctx):
    b = ctx.behavior or {}
    targets = [o for o in _outlines(items) if re.match(r"^List behaviou?r", o.title, re.I)]
    if not targets:
        return
    has_caps = any(b.get(k) for k in ("search", "filters", "sortable_columns", "views", "counters", "empty_state"))
    if not has_caps:
        return
    records = _collect_records(items, ctx)
    if not records:
        return
    name_field = next((f["name"] for f in ctx.fields if re.search(r"name|title", f["name"] + " " + f["label"], re.I)
                       and f["kind"] in ("input", "picklist")), None)
    recs = records[:3]
    new: list = []
    used_k = set(getattr(ctx, "saved_keys", set())) | {r["key"].lower() for r in records} \
        | {v.lower() for r in records for v in r["fields"].values()}                         # every code and name taken so far

    def outline(title, steps, header, rows):
        o = Outline(title=f'List behaviour - {title} - "<Condition>"')
        o.steps = [Step(k, t) for k, t in steps]
        o.tables.append(Table(header=header, rows=rows))
        new.append(o)

    if b.get("search"):
        rows = [[f"Search by code {r['key']}", r["key"], r["key"]] for r in recs]
        r0 = recs[0]
        nm = r0["fields"].get(name_field or "", "")
        if nm:
            rows.append([f"Search by full name {nm}", nm, nm])
            rows.append([f"Search by lower-case name {nm.lower()}", nm.lower(), nm])
            first = nm.split()[0]
            if len(first) >= 3 and first.lower() != nm.lower():
                rows.append([f"Search by part of the name {first}", first, nm])
        outline("Search the list", [("When", 'I search the list for "<SearchTerm>"'),
                                    ("Then", 'I should see the record "<ExpectedRecord>" in the list')],
                ["Condition", "SearchTerm", "ExpectedRecord"], rows)
        if len(recs) >= 2:
            outline("Clear the search", [("When", 'I search the list for "<SearchTerm>"'),
                                         ("And", "I clear the list search"),
                                         ("Then", 'I should see the record "<OtherRecord>" in the list')],
                    ["Condition", "SearchTerm", "OtherRecord"],
                    [[f"Clear after searching {recs[0]['key']}", recs[0]["key"], recs[1]["key"]]])
        msg = (b.get("empty_state") or {}).get("message", "")
        if msg:
            outline("Search with no match shows the empty state",
                    [("When", 'I search the list for "<SearchTerm>"'), ("Then", f'I should see the empty state "{msg}"'),
                     ("When", "I clear the list search"), ("Then", 'I should see the record "<ExpectedRecord>" in the list')],
                    ["Condition", "SearchTerm", "ExpectedRecord"], [["No record matches", "zzqnomatch987", recs[0]["key"]]])
        else:
            outline("Search with no match hides the records",
                    [("When", 'I search the list for "<SearchTerm>"'), ("Then", 'I should not see the record "<HiddenRecord>" in the list')],
                    ["Condition", "SearchTerm", "HiddenRecord"], [["No record matches", "zzqnomatch987", recs[0]["key"]]])

    if b.get("filters"):
        flt = b["filters"][0]
        all_opt = flt.get("all_option", "")
        static = [o for o in flt.get("static_options") or [] if o and o != all_opt]
        cb = next((f for f in ctx.fields if f["kind"] == "combobox" and f["options"]
                   and re.search(r"type|status|category|kind", f["name"], re.I)), None) \
            or next((f for f in ctx.fields if f["kind"] == "combobox" and f["options"]), None)
        # the filter can name an option differently from the form ("Manufacturing" in the filter, "Production" in the form):
        # the rank map of the screen's TS (same order as the form options) or config/option_aliases.json tells which is which
        if not static and cb:
            for mp in (b.get("sort_ranks") or {}).values():
                if isinstance(mp, dict) and len(mp) == len(cb["options"]):
                    static = [k for k, _ in sorted(mp.items(), key=lambda kv: kv[1])]
                    break
        pairs: list = []      # (form option, filter label)
        if cb:
            aliases = ctx.aliases.get(cb["name"], {})
            for idx, opt in enumerate(cb["options"]):
                label = aliases.get(opt)
                if not label:
                    if static and _n(opt) in {_n(x) for x in static}:
                        label = next(x for x in static if _n(x) == _n(opt))
                    elif static and len(static) == len(cb["options"]):
                        label = static[idx]
                    elif not static:
                        label = opt
                if label:
                    pairs.append((opt, label))
                else:
                    ctx.note(f'list filter: no filter label known for the form option "{opt}" - add it to config/option_aliases.json')
        else:
            pairs = [(x, x) for x in static]
        rows = []
        extra: list = []
        for opt, label in pairs:
            hit = next((r for r in records if cb and _n(r["fields"].get(cb["name"], "")) == _n(opt)), None)
            if not hit and cb and ctx.key_field:
                tmpl = recs[0]
                vals = dict(tmpl["fields"])
                vals[cb["name"]] = opt
                vals[ctx.key_field] = bump(tmpl["key"], used_k, 0)
                used_k.add(vals[ctx.key_field].lower())
                for nm in ctx.typed:
                    if nm != ctx.key_field and nm in vals:
                        vals[nm] = bump(vals[nm], used_k, 0)
                        used_k.add(vals[nm].lower())
                hit = {"key": vals[ctx.key_field], "fields": vals}
                extra.append(hit)
            if hit:
                rows.append([f"Filter by {label}", label, hit["key"], all_opt])
        if extra:
            ro = _make_create_outline(ctx, "Records for the list checks", extra, "ListRecord")
            if ro is not None:
                new.insert(0, ro)
        if rows:
            steps = [("When", 'I filter the list by type "<FilterValue>"'), ("Then", 'I should see the record "<ExpectedRecord>" in the list')]
            if all_opt:
                steps += [("When", 'I filter the list by type "<AllOption>"'), ("Then", 'I should see the record "<ExpectedRecord>" in the list')]
                hdr = ["Condition", "FilterValue", "ExpectedRecord", "AllOption"]
            else:
                hdr = ["Condition", "FilterValue", "ExpectedRecord"]
                rows = [r[:3] for r in rows]
            outline("Filter the list by type and back to all", steps, hdr, rows)

    if b.get("sortable_columns"):
        rows = []
        for c in b["sortable_columns"]:
            lab = _clean_label(c.get("label", ""))
            if lab:
                for order in ("asc", "desc"):
                    rows.append([f"Sort by {lab} {order}", lab, order])
        if rows:
            outline("Sort a column", [("When", 'I sort the list by the "<Column>" column in "<Order>" order'),
                                      ("Then", 'the list should be sorted by "<Column>" in "<Order>" order')],
                    ["Condition", "Column", "Order"], rows)

    if b.get("views"):
        known = b.get("containers") or {}
        rows = [[f"Switch to the {v['mode']} view", v["mode"], recs[0]["key"]] for v in b["views"]
                if v.get("mode") and (known.get(v["mode"]) or {}).get("class")]   # a view is only checked when its container is known
        if rows:
            outline("Switch the list view", [("When", 'I switch to the "<View>" view'), ("Then", 'I should see the "<View>" view'),
                                             ("And", 'I should see the record "<ExpectedRecord>" in the list')],
                    ["Condition", "View", "ExpectedRecord"], rows)

    labels = (b.get("counters") or {}).get("labels") or []
    plan = []
    for lab in labels:
        low = lab.lower()
        if "total" in low:
            plan.append((lab, "1"))
        elif "active" in low and "inactive" not in low:
            plan.append((lab, "1"))
    if plan and ctx.key_field:
        template = recs[0]
        used = used_k
        names = [f["name"] for f in ctx.fields if not f["readonly"] and f["name"] in template["fields"]]
        o = Outline(title='List behaviour - Counters change after a save - "<Condition>"')
        o.steps.append(Step("When", 'I remember the number shown for "<Label>"'))
        o.steps.append(Step("And", "I click the Add New button"))
        for nm in names:
            st = _field_step(ctx, nm, f"<{nm}>")
            if st:
                o.steps.append(Step("And", st))
        o.steps += [Step("And", "I click the save button"), Step("Then", f'I should see a confirmation message "{ctx.save_msg}"'),
                    Step("And", 'the number shown for "<Label>" should have changed by "<Delta>"')]
        t = Table(header=["Condition", "Label", "Delta"] + names, rows=[])
        for lab, delta in plan:
            vals = dict(template["fields"])
            key = bump(template["key"], used, 0)
            used.add(key.lower())
            vals[ctx.key_field] = key
            for nm in ctx.typed:
                if nm != ctx.key_field and nm in vals:
                    vals[nm] = bump(vals[nm], used, 0)
                    used.add(vals[nm].lower())
            if any(f["kind"] == "checkbox" for f in ctx.fields):
                for f in ctx.fields:
                    if f["kind"] == "checkbox" and f["name"] in vals:
                        vals[f["name"]] = "Checked"
            t.rows.append([f"{lab} changes by {delta} after saving a new record", lab, delta] + [vals.get(nm, "") for nm in names])
        o.tables.append(t)
        new.append(o)

    at = items.index(targets[0])
    for x in targets:
        _drop(items, x)
    for k, o in enumerate(new):
        items.insert(at + k, o)
    ctx.note(f"'List behaviour' rebuilt from the screen's own facts: {len(new)} scenario(s) that can run on this screen")


def _p_drop_pointless(items, ctx):
    for o in list(_outlines(items)):
        if re.search(r"not applicable", o.title, re.I):
            _drop(items, o)
            ctx.note(f'dropped "{o.title}": it tests nothing')
            continue
        if o.tables and all(not t.rows for t in o.tables):
            _drop(items, o)
            continue
        acts = [s for s in o.steps if not re.match(r"^the form should (still )?be (open|closed)$", s.text)]
        if not acts:
            _drop(items, o)
            ctx.note(f'dropped "{o.title}": it only checks the form state with no action before it')


def _p_keywords(items, ctx):
    for o in _outlines(items):
        if o.steps and o.steps[0].kw in ("And", "But", "*"):
            o.steps[0].kw = "Given" if o.steps[0].text.startswith("I navigate") else "When"


def _opens_form(text: str) -> bool:
    return bool(re.match(r"^(I click the Add New button|I click the Edit button|I open the existing record )", text))


def _p_form_state_start(items, ctx):
    """'the form should be open' as the first step: the form is closed when the screen loads."""
    n = 0
    for o in _outlines(items):
        if o.steps and re.match(r"^the form should (still )?be open$", o.steps[0].text):
            if any(_opens_form(s.text) for s in o.steps[1:]):
                o.steps.pop(0)
            else:
                o.steps[0] = Step("When", "I click the Add New button")
            n += 1
    if n:
        ctx.note(f"{n} scenario(s) started by checking the form is open right after the screen loads (it is closed): now they open it first")


def _p_overlength(items, ctx):
    """A text box with a maximum length cuts longer input, so an over-length row would save a copy of the max-length record."""
    caps = {f["name"]: int(f["max_length"]) for f in ctx.fields if f.get("max_length")}
    if not caps:
        return
    for o in list(_outlines(items)):
        checks = []
        for s in o.steps:
            f = _fs(s)
            if f and f[2] in caps and f[3] in ("input", "textarea", "new record"):
                checks.append((f[1], caps[f[2]], f[2]))
        if not checks:
            continue
        if not o.tables:
            if any("<" not in v and len(v) > cap for v, cap, _ in checks):
                _drop(items, o)
            continue
        for t in o.tables:
            keep = []
            for row in t.rows:
                rm = _row_map(t, row)
                bad = next(((fn, cap) for v, cap, fn in checks
                            if "<" not in _resolve(v, rm) and len(_resolve(v, rm)) > cap), None)
                if bad:
                    ctx.note(f'dropped a row of "{o.title}": the text is longer than the {bad[1]} characters {bad[0]} accepts')
                    continue
                keep.append(row)
            t.rows = keep
        if all(not t.rows for t in o.tables):
            _drop(items, o)


def _p_open_by_key(items, ctx):
    """Without 'Add New', choosing a key in the key picklist means opening that record."""
    if not ctx.key_field:
        return
    n = 0
    for o in _outlines(items):
        if _has(o, r"^I click the Add New button$"):
            continue
        for s in o.steps:
            f = _fs(s)
            if f and f[0] == "select" and f[3] == "picklist" and f[2] == ctx.key_field and f[1]:
                s.text = f'I open the existing record "{f[1]}" using the "{f[2]}" field'
                n += 1
    if n:
        ctx.note(f"{n} key picklist step(s) outside 'Add New' now open the existing record")


def _p_save_form_state(items, ctx):
    """A successful save closes the form: 'the form should (still) be open' after it only holds for an error."""
    for o in list(_outlines(items)):
        saves = [i for i, s in enumerate(o.steps) if re.match(r"^I click the (save|Update) button$", s.text)]
        if not saves:
            continue
        last = saves[-1]
        state = [i for i in range(last + 1, len(o.steps)) if re.match(r"^the form should (still )?be open$", o.steps[i].text)]
        conf = next((s for s in o.steps[last + 1:] if re.match(r'^I should see a confirmation message "', s.text)), None)
        if not state or conf is None:
            continue
        raw_msg = re.match(r'^I should see a confirmation message "(.*)"$', conf.text).group(1)
        if not o.tables:
            if _SUCCESS_RX.search(raw_msg):
                for i in state:
                    o.steps[i].text = "the form should be closed"
                ctx.note(f'"{o.title}": the form closes after a successful save')
            continue
        t = o.tables[0]
        ok = [r for r, row in enumerate(t.rows) if _SUCCESS_RX.search(_resolve(raw_msg, _row_map(t, row)))]
        if not ok:
            continue
        if len(ok) == len(t.rows):
            for i in state:
                o.steps[i].text = "the form should be closed"
            ctx.note(f'"{o.title}": the form closes after a successful save')
            continue
        good = copy.deepcopy(o)
        good.tables[0].rows = [t.rows[r] for r in ok]
        for i in state:
            good.steps[i].text = "the form should be closed"
        good.title = o.title.replace(' - "<Condition>"', ' (saved) - "<Condition>"') if o.title.endswith(' - "<Condition>"') else o.title + " (saved)"
        t.rows = [row for r, row in enumerate(t.rows) if r not in ok]
        items.insert(items.index(o), good)
        ctx.note(f'"{o.title}" split: rows that save close the form, rows with an error keep it open')


def _p_edit_button(items, ctx):
    """'I click the Edit button' needs a list row or an open record. Open a saved record instead."""
    if not ctx.key_field:
        return
    records = _collect_records(items, ctx)
    if not records:
        return
    n = 0
    for o in _outlines(items):
        if not _has(o, r"^I click the Edit button$"):
            continue
        has_open = any(re.match(r"^I open the existing record", s.text) for s in o.steps)
        new = []
        for s in o.steps:
            if s.text == "I click the Edit button":
                if not has_open:
                    new.append(Step(s.kw, f'I open the existing record "{records[0]["key"]}" using the "{ctx.key_field}" field'))
                    has_open = True
                n += 1
                continue
            new.append(s)
        o.steps = new
    if n:
        ctx.note(f"{n} 'I click the Edit button' step(s) replaced by opening a saved record (the button is not on screen with no record open)")


def _p_visibility(items, ctx):
    """Save and Update share one button: a new record shows Save, an open record shows Update."""
    targets = [o for o in _outlines(items) if any(re.match(r'^the "<[^>]+>" action should be', s.text) for s in o.steps)]
    if not targets or not ctx.key_field:
        return
    records = _collect_records(items, ctx)
    if not records:
        return
    a = Outline(title='Form behaviour - Buttons of a new record - "<Condition>"')
    a.steps = [Step("When", "I click the Add New button"), Step("Then", 'the "Save" action should be "visible"'),
               Step("And", 'the "Delete" action should be "hidden"')]
    a.tables.append(Table(header=["Condition"], rows=[["New record shows Save and hides Delete"]]))
    b = Outline(title='Form behaviour - Buttons of an existing record - "<Condition>"')
    b.steps = [Step("When", f'I open the existing record "{records[0]["key"]}" using the "{ctx.key_field}" field'),
               Step("Then", 'the "Update" action should be "visible"'), Step("And", 'the "Delete" action should be "visible"')]
    b.tables.append(Table(header=["Condition"], rows=[["Existing record shows Update and Delete"]]))
    at = items.index(targets[0])
    for x in targets:
        _drop(items, x)
    items[at:at] = [a, b]
    ctx.note("button visibility rebuilt: new record = Save visible / Delete hidden, open record = Update visible / Delete visible")



# =========================================================================== second round of repairs
# (navigation, forms that never open, invented / empty / not-yet-saved records, update flow, clean-up)
def _nav_text(ctx) -> str:
    if len(ctx.nav) in (2, 3):
        return "I navigate through the following path " + " -> ".join(f'"{n}"' for n in ctx.nav)
    return ""


def _p_navigation(items, ctx):
    """The Background must use the menu path the live scan found, with the right number of levels."""
    new = _nav_text(ctx)
    if not new:
        return
    changed = 0
    for it in items:
        if isinstance(it, Raw):
            for k, line in enumerate(it.lines):
                sm = _STEP_RE.match(line)
                if sm and sm.group(3).startswith("I navigate through the following path") and sm.group(3) != new:
                    it.lines[k] = f"{sm.group(1)}{sm.group(2)} {new}"
                    changed += 1
        elif isinstance(it, Outline):
            for st in it.steps:
                if st.text.startswith("I navigate through the following path") and st.text != new:
                    st.text = new
                    changed += 1
    if changed:
        ctx.note(f"navigation now uses the path the live scan found: {' > '.join(ctx.nav)}")


def _is_field_action(text: str) -> bool:
    return bool(_fs(Step("", text)) or _CLEAR_RE.match(text))


def _p_ensure_open(items, ctx):
    """A scenario that types into the form without opening it first (the form is closed when the screen loads)."""
    n = 0
    for o in _outlines(items):
        if any(_opens_form(s.text) for s in o.steps) or _has(o, r"^I click the Update button$"):
            continue
        for i, s in enumerate(o.steps):
            if _is_field_action(s.text):
                o.steps.insert(i, Step("When", "I click the Add New button"))
                n += 1
                break
    if n:
        ctx.note(f"{n} scenario(s) typed into the form without opening it: they now start with 'Add New'")


def _p_create_clear(items, ctx):
    """On a NEW record every field is already empty: a 'clear' step there does nothing."""
    n = 0
    for o in _outlines(items):
        if not (_has(o, r"^I click the Add New button$") and _has(o, r"^I click the save button$")):
            continue
        keep = [s for s in o.steps if not _CLEAR_RE.match(s.text)]
        if len(keep) != len(o.steps):
            n += len(o.steps) - len(keep)
            o.steps = keep
    if n:
        ctx.note(f"{n} 'clear' step(s) on a new record removed (its fields start empty; an empty Examples cell leaves a field empty)")


def _deleted_keys(items, ctx) -> set:
    out = set()
    for o in _outlines(items):
        if not _has(o, r"^I confirm the delete$"):
            continue
        for s, k0 in _record_key_refs(o):
            ph = re.fullmatch(r"<([^<>]+)>", k0)
            if ph and o.tables:
                ci = _col(o.tables[0], ph.group(1))
                out.update(r[ci].strip().lower() for r in o.tables[0].rows if 0 <= ci < len(r) and r[ci].strip())
            elif not ph:
                out.add(k0.strip().lower())
    return out


def _usable_records(items, ctx) -> list:
    gone = _deleted_keys(items, ctx)
    allr = _collect_records(items, ctx)
    return [r for r in allr if r["key"].lower() not in gone] or allr


def _p_update_open(items, ctx):
    """Update starts from an OPEN record, not from 'Add New' (that would be a save)."""
    if not ctx.key_field:
        return
    recs = _usable_records(items, ctx)
    if not recs:
        return
    n = 0
    for o in _outlines(items):
        if not _has(o, r"^I click the Update button$") or _has(o, r"^I open the existing record") or not o.tables:
            continue
        t = o.tables[0]
        col = "Record" if _col(t, "Record") < 0 else "RecordKey"
        step = f'I open the existing record "<{col}>" using the "{ctx.key_field}" field'
        idx = next((i for i, s in enumerate(o.steps) if s.text == "I click the Add New button"), None)
        if idx is None:
            o.steps.insert(0, Step("When", step))
        else:
            o.steps[idx] = Step(o.steps[idx].kw, step)
        t.header.insert(1, col)
        for r, row in enumerate(t.rows):
            row.insert(1, recs[r % len(recs)]["key"])
        n += 1
    if n:
        ctx.note(f"{n} update scenario(s) started with 'Add New': they now open a saved record first")


def _clear_shape(o: Outline):
    """(column of the cleared field, column of the expected message) of a 'clear a field and update' outline, or None."""
    if not o.tables:
        return None
    t = o.tables[0]
    cs = next((_CLEAR_RE.match(s.text) for s in o.steps if _CLEAR_RE.match(s.text)), None)
    ph = re.fullmatch(r"<([^<>]+)>", cs.group(1)) if cs else None
    ci = _col(t, ph.group(1)) if ph else -1
    conf = next((s for s in o.steps if s.text.startswith('I should see a confirmation message "')), None)
    ph2 = re.fullmatch(r'I should see a confirmation message "<([^<>]+)>"', conf.text) if conf else None
    ei = _col(t, ph2.group(1)) if ph2 else -1
    return (ci, ei) if ci >= 0 and ei >= 0 else None


def _p_update_clear(items, ctx):
    """'Clear a field and Update': a mandatory field expects its real message, an optional field keeps the agent's row
    (the update succeeds); a mandatory field with no row of its own gets one."""
    if not ctx.key_field:
        return
    recs = _usable_records(items, ctx)
    targets = [o for o in _outlines(items) if _has(o, r"^I click the Update button$") and any(_CLEAR_RE.match(s.text) for s in o.steps)]
    if not targets:
        return
    required = {f["name"]: f for f in ctx.fields
                if f["required"] and f["required_message"] and f["name"] != ctx.key_field and not f["readonly"]}
    covered: set = set()
    shaped = []
    for o in targets:
        sh = _clear_shape(o)
        if sh is None:
            _drop(items, o)                      # a shape the repair cannot read: rebuilt below from the required fields
            continue
        shaped.append((o, sh))
        t, (ci, ei) = o.tables[0], sh
        for row in t.rows:
            fld = ctx.field(row[ci]) if ci < len(row) else None
            if fld and fld["name"] in required and ei < len(row):
                row[ei] = required[fld["name"]]["required_message"]
                covered.add(fld["name"])
    missing = [f for n, f in required.items() if n not in covered]
    if not missing:
        return
    if shaped:
        o, (ci, ei) = shaped[0]
        t = o.tables[0]
        for f in missing:
            base = list(t.rows[0]) if t.rows else [""] * len(t.header)
            base = base + [""] * (len(t.header) - len(base))
            base[0] = f"Clear {f['label'] or f['name']}"
            base[ci], base[ei] = f["name"], f["required_message"]
            t.rows.append(base)
        ctx.note(f"update 'clear a field': {len(missing)} mandatory field(s) had no row and got one with the real message")
        return
    if not recs:
        ctx.note("update 'clear a mandatory field' scenario dropped: no saved record to open")
        return
    rows = [[f"Clear {f['label'] or f['name']}", f["name"], f["required_message"]] for f in missing]
    o = Outline(title='Update clear - Clear a mandatory field and update - "<Condition>"')
    o.steps = [Step("When", f'I open the existing record "{recs[0]["key"]}" using the "{ctx.key_field}" field'),
               Step("And", 'I clear the "<ClearField>" field'), Step("And", "I click the Update button"),
               Step("Then", 'I should see a confirmation message "<ExpectedResult>"'), Step("And", "the form should still be open")]
    o.tables.append(Table(header=["Condition", "ClearField", "ExpectedResult"], rows=rows))
    at = next((i for i, it in enumerate(items) if isinstance(it, Outline) and _is_destructive(it)), len(items))
    items.insert(at, o)
    ctx.note(f"'clear a mandatory field' update scenario rebuilt: one row per required field ({len(rows)}), each with its own field and message")


def _p_update_label_rows(items, ctx):
    """An update row that says it changes the code cannot change it."""
    if not ctx.key_field:
        return
    kf = ctx.field(ctx.key_field) or {}
    words = {_n(ctx.key_field), _n(kf.get("label", ""))} - {""}
    names = [x for x in (ctx.key_field, kf.get("label", "")) if x]
    rx = re.compile(r"\b(?:" + "|".join(re.escape(x) for x in names) + r")\b(?:\s*(?:and|,|&)\s*|\s+)?", re.I)
    for o in _outlines(items):
        if not _has(o, r"^I click the Update button$"):
            continue
        for t in o.tables:
            for r in t.rows:
                if r and any(w in _n(r[0]) for w in words):
                    old = r[0]
                    new = re.sub(r"\s+", " ", rx.sub("", old)).strip(" ,-")
                    r[0] = new if len(new) > 8 else "Update the other fields"
                    ctx.note(f'update row "{old}" renamed to "{r[0]}": an open record keeps its {ctx.key_field}')


def _ref_cells(o: Outline) -> list:
    out = []
    for s in o.steps:
        m = re.match(r'^(?:I (?:open the existing|request to delete the) record|I should (?:still|no longer) see the record) "([^"]*)" using the "([^"]*)" field$', s.text)
        if m:
            ph = re.fullmatch(r"<([^<>]+)>", m.group(1))
            out.append((s, ph.group(1) if ph else None, m.group(1)))
    return out


def _p_known_records(items, ctx):
    """Every record a scenario opens / keeps / duplicates must be one this file saves. Empty keys are dropped,
    invented ones (seed-data guesses) are replaced by a record the file really saves."""
    if not ctx.key_field:
        return
    recs = _usable_records(items, ctx)
    if not recs:
        return
    known = {r["key"].lower() for r in _collect_records(items, ctx)}
    rot = [0]

    def pick():
        k = recs[rot[0] % len(recs)]["key"]
        rot[0] += 1
        return k

    dropped = replaced = 0
    for o in list(_outlines(items)):
        if _has(o, r"^I confirm the delete$"):
            continue                                   # destructive scenarios are handled by _p_delete_flow
        refs = _ref_cells(o)
        dup = bool(re.search(r"duplicate", o.title, re.I)) or any(
            re.search(r"duplicate", s.text, re.I) for s in o.steps if s.text.startswith("I should see a confirmation message"))
        is_dup_create = dup and _is_create(o, ctx)
        if not refs and not is_dup_create:
            continue
        for t in o.tables:
            keep = []
            for row in t.rows:
                rm = _row_map(t, row)
                bad_row = False
                for s, ph, lit in refs:
                    if not ph:
                        continue
                    key = rm.get(ph, "").strip()
                    if not key:
                        bad_row = True
                        break
                    if key.lower() not in known:
                        row[_col(t, ph)] = pick()
                        replaced += 1
                if bad_row:
                    dropped += 1
                    continue
                if is_dup_create:
                    kstep = next(x for x in o.steps if _fs(x) and _fs(x)[2] == ctx.key_field)
                    kph = re.fullmatch(r"<([^<>]+)>", _fs(kstep)[1])
                    ci = _col(t, kph.group(1)) if kph else -1
                    cond = row[0] if row else ""
                    other = [f for f in ctx.fields if f["name"] != ctx.key_field
                             and (re.search(re.escape(f["name"]), cond, re.I) or re.search(r"\b" + re.escape(f["label"]) + r"\b", cond, re.I))]
                    if other:
                        dropped += 1
                        ctx.note(f'dropped duplicate row "{cond}": only a repeated {ctx.key_field} is known to be rejected as a duplicate')
                        continue
                    if ci >= 0 and row[ci].strip().lower() not in known:
                        row[ci] = pick()
                        replaced += 1
                keep.append(row)
            t.rows = keep
        for s, ph, lit in refs:                         # literal keys written straight into the step
            if not ph:
                if not lit.strip():
                    _drop(items, o)
                    dropped += 1
                    break
                if lit.strip().lower() not in known:
                    s.text = s.text.replace(f'"{lit}"', f'"{pick()}"', 1)
                    replaced += 1
        if o.tables and all(not t.rows for t in o.tables):
            _drop(items, o)
    if dropped:
        ctx.note(f"{dropped} row(s) dropped: they opened a record with no code, or a duplicate that cannot be proven")
    if replaced:
        ctx.note(f"{replaced} record code(s) that no scenario saves (guessed from the source) now point at records this file saves")


def _p_order_creates(items, ctx):
    """Scenarios that save records run before the scenarios that use them (up to the first Rule block)."""
    if not ctx.key_field:
        return
    cut = next((i for i, it in enumerate(items) if isinstance(it, RuleLine)), len(items))
    head, tail = items[:cut], items[cut:]
    first = next((i for i, it in enumerate(head) if isinstance(it, Outline)), None)
    if first is None:
        return
    outs = [it for it in head[first:] if isinstance(it, Outline)]
    creators = [it for it in outs if _is_create(it, ctx) and it.tables and _success_rows(it, it.tables[0])]
    rest = [it for it in head[first:] if it not in creators]
    new_order = head[:first] + creators + rest
    if any(a is not b for a, b in zip(new_order, head)):
        ctx.note("scenarios that save records now run first, so nothing opens, searches or duplicates a record before it exists")
    items[:] = new_order + tail


def _p_retitle_misleading(items, ctx):
    for o in _outlines(items):
        if not re.search(r"\b(fails?|invalid|reject(s|ed)?|errors?)\b", o.title, re.I) or not o.tables:
            continue
        conf = next((s for s in o.steps if s.text.startswith("I should see a confirmation message")), None)
        t = o.tables[0]
        if conf is None or not t.rows:
            continue
        raw = re.match(r'^I should see a confirmation message "(.*)"$', conf.text)
        if raw and all(_SUCCESS_RX.search(_resolve(raw.group(1), _row_map(t, r))) for r in t.rows):
            prefix = o.title.split(" - ")[0] if " - " in o.title else "Save"
            old = o.title
            o.title = f'{prefix} - Save with a valid value - "<Condition>"'
            ctx.note(f'"{old}" only tests successful saves: renamed so the title matches what it checks')


def _p_coverage_creates(items, ctx):
    """Every dropdown option, both checkbox states and every maximum length are SAVED at least once."""
    if not ctx.key_field:
        return
    recs = [r for r in _collect_records(items, ctx) if len(r["fields"]) >= 3]
    if not recs:
        return
    tmpl = recs[0]
    saved = [r["fields"] for r in _collect_records(items, ctx)]
    used = {v.lower() for r in saved for v in r.values()} | set(getattr(ctx, "saved_keys", set()))
    new: list = []
    labels: list = []
    kcap = int((ctx.field(ctx.key_field) or {}).get("max_length") or 0)

    def clone(**chg):
        fields = dict(tmpl["fields"])
        fields.update(chg)
        key = bump(tmpl["key"], used, kcap)
        fields[ctx.key_field] = key
        used.add(key.lower())
        for nm in ctx.typed:
            if nm != ctx.key_field and nm in fields and nm not in chg:
                fields[nm] = bump(fields[nm], used, int((ctx.field(nm) or {}).get("max_length") or 0))
                used.add(fields[nm].lower())
        return {"key": key, "fields": fields}

    for f in ctx.fields:
        if f["readonly"] or f["name"] == ctx.key_field:
            continue
        if f["kind"] in ("combobox", "radio") and f["options"]:
            have = {_n(r.get(f["name"], "")) for r in saved}
            for opt in f["options"]:
                if _n(opt) not in have:
                    new.append(clone(**{f["name"]: opt}))
                    labels.append(f"{f['label']} {opt} saved")
        elif f["kind"] == "checkbox":
            have = {_n(r.get(f["name"], "")) for r in saved}
            for st in ("Checked", "Unchecked"):
                if _n(st) not in have:
                    new.append(clone(**{f["name"]: st}))
                    labels.append(f"{f['label']} {st.lower()} saved")
    for f in ctx.fields:
        cap = int(f.get("max_length") or 0)
        if f["readonly"] or cap < 2 or f["kind"] not in ("input", "textarea", "picklist"):
            continue
        if any(len(r.get(f["name"], "")) == cap for r in saved):
            continue
        seed = (tmpl["fields"].get(f["name"]) or f["label"] or "Value").strip()
        if f["name"] == ctx.key_field:
            m = re.match(r"^(\D*)", seed)
            pre = (m.group(1) if m else "")[: max(0, cap - 2)] or "K"
            val = None
            for n in range(1, 100000):
                cand = pre + str(n).zfill(cap - len(pre))
                if len(cand) == cap and cand.lower() not in used:
                    val = cand
                    break
            if not val:
                continue
            used.add(val.lower())
            fields = dict(tmpl["fields"])
            fields[ctx.key_field] = val
            for nm in ctx.typed:
                if nm != ctx.key_field and nm in fields:
                    fields[nm] = bump(fields[nm], used, int((ctx.field(nm) or {}).get("max_length") or 0))
                    used.add(fields[nm].lower())
            new.append({"key": val, "fields": fields})
        else:
            val = seed
            while len(val) < cap:
                val += " " + seed
            val = val[:cap].rstrip()
            val = val + "x" * (cap - len(val))
            if f["name"] in ctx.typed:
                for n in range(1, 100000):
                    tail = str(n)
                    cand = val[: cap - len(tail)] + tail
                    if cand.lower() not in used:
                        val = cand
                        break
            used.add(val.lower())
            new.append(clone(**{f["name"]: val}))
        labels.append(f"{f['label']} at its maximum length ({cap})")
    if not new:
        return
    o = _make_create_outline(ctx, "Field values - every option, checkbox state and maximum length saved", new, "Field value", labels)
    if o is None:
        return
    last_create = max((i for i, it in enumerate(items) if isinstance(it, Outline) and _is_create(it, ctx)), default=len(items) - 1)
    items.insert(last_create + 1, o)
    ctx.note(f"added {len(new)} saving row(s): options, checkbox states and maximum lengths that no scenario saved")


_STRONG_RE = re.compile(r"^(I click the (save|Update|Reset|Close|Delete) button|I should see|I confirm|I cancel|I request|the \"[^\"]*\" field should show"
                        r"|the list|the number|I search|I filter|I sort|I switch|I remember)")


def _p_drop_weak(items, ctx):
    """'Type a value, the form is open' tests nothing: no save, no message, no value check."""
    for o in list(_outlines(items)):
        if not any(_is_field_action(s.text) for s in o.steps):
            continue
        if any(_STRONG_RE.match(s.text) for s in o.steps):
            continue
        _drop(items, o)
        ctx.note(f'dropped "{o.title}": it types values and only checks the form is open (tests nothing; saving is covered by the save scenarios)')


def _p_unused_columns(items, ctx):
    for o in _outlines(items):
        used = _placeholders_used(o) | set(_PH_RE.findall(o.title))
        for t in o.tables:
            for ci in range(len(t.header) - 1, 0, -1):
                if t.header[ci] not in used:
                    t.header.pop(ci)
                    for r in t.rows:
                        if ci < len(r):
                            r.pop(ci)


def _p_cleanup(items, ctx):
    """Re-runs: records this run created are deleted at the end (the clean-up scenario always runs)."""
    if not ctx.key_field:
        return
    gone = _deleted_keys(items, ctx)
    recs = [r for r in _collect_records(items, ctx) if r["key"].lower() not in gone]
    if not recs:
        return
    msg = (ctx.messages.get("delete") or ["Details have been deleted successfully"])[0]
    o = Outline(title='Cleanup - Remove the records this run created - "<Condition>"')
    o.steps = [Step("When", f'I request to delete the record "<Record>" using the "{ctx.key_field}" field'),
               Step("And", "I confirm the delete"), Step("Then", f'I should see a confirmation message "{msg}"')]
    o.tables.append(Table(header=["Condition", "Record"], rows=[[f"Remove {r['key']}", r["key"]] for r in recs]))
    items.append(o)
    ctx.note(f"added a final clean-up scenario that deletes the {len(recs)} record(s) this run created (so the file can run again)")



# =========================================================================== vocabulary, calculated fields, dates, messages
_BUTTON_NAMES = {"add new": "Add New", "save": "save", "update": "Update", "edit": "Edit", "delete": "Delete",
                 "reset": "Reset", "close": "Close"}
_STEP_MAP = (
    (re.compile(r"^(?:I (?:should )?see )?the form (?:is |remains |stays |still )*(?:still )(?:open|opened)$", re.I), "the form should still be open"),
    (re.compile(r"^(?:I (?:should )?see )?the form (?:is |remains |stays )?(?:open|opened)$", re.I), "the form should be open"),
    (re.compile(r"^(?:I (?:should )?see )?the form (?:is |gets |has )?(?:closed|close)$", re.I), "the form should be closed"),
    (re.compile(r"^(?:I (?:should )?see )?the form fields? (?:is |are |should be |remain )?(?:empty|cleared|blank)$", re.I),
     "the form fields should be empty"),
)


def _map_step(text: str) -> Optional[str]:
    t = text.strip()
    for rx, rep in _STEP_MAP:
        if rx.match(t):
            return rep
    m = re.match(r"^I click (?:on )?the (add new|save|update|edit|delete|reset|close) button$", t, re.I)
    if m:
        return f"I click the {_BUTTON_NAMES[m.group(1).lower()]} button"
    m = re.match(r'^I (?:should )?see (?:a |the )?(?:confirmation |success |validation |error |alert )?message "(.*)"$', t, re.I)
    if m:
        return f'I should see a confirmation message "{m.group(1)}"'
    return None


def _p_vocabulary(items, ctx):
    """A step sentence that is not in the vocabulary has no definition: map it to the right sentence or remove it. The
    script never gets a new definition to make an invalid sentence pass."""
    from service import extended_steps
    fixed = removed = 0
    for o in list(_outlines(items)):
        keep = []
        for s in o.steps:
            if extended_steps.in_vocabulary(s.text):
                keep.append(s)
                continue
            new = _map_step(s.text)
            if new and extended_steps.in_vocabulary(new):
                ctx.note(f'step not in the vocabulary: "{s.text}" -> "{new}"')
                s.text = new
                fixed += 1
                keep.append(s)
                continue
            removed += 1
            ctx.note(f'step not in the vocabulary, removed: "{s.text}" (scenario "{o.title}")')
        o.steps = keep
        if not o.steps:
            _drop(items, o)
    if fixed or removed:
        ctx.note(f"{fixed} step(s) mapped and {removed} step(s) removed because they are not in the step vocabulary")


def _p_message_braces(items, ctx):
    """Expected messages with {placeholders} (Leave No {leaveNumbers}) are matched by their fixed part only."""
    n = 0
    for o in _outlines(items):
        for s in o.steps:
            m = re.match(r'^(I should see a confirmation message ")(.*)"$', s.text)
            if m and "{" in m.group(2):
                s.text = f'{m.group(1)}{message_fragment(m.group(2))}"'
                n += 1
        for t in o.tables:
            for ci, h in enumerate(t.header):
                if "expected" in h.lower():
                    for row in t.rows:
                        if ci < len(row) and "{" in row[ci]:
                            row[ci] = message_fragment(row[ci])
                            n += 1
    if n:
        ctx.note(f"{n} expected message(s) with {{placeholders}} now hold only their fixed text")


def _calc_name(ctx, name: str):
    f = ctx.field(name) if name and "<" not in name else None
    return f if f and f.get("calculated") else None


def _p_calculated(items, ctx):
    """Calculated / read-only fields are never filled: remove every input step (and clear step) that targets one."""
    if not any(f.get("calculated") for f in ctx.fields):
        return
    n = 0
    for o in list(_outlines(items)):
        keep = []
        for s in o.steps:
            f = _fs(s)
            name = f[2] if f else (_CLEAR_RE.match(s.text).group(1) if _CLEAR_RE.match(s.text) else None)
            if name and _calc_name(ctx, name):
                n += 1
                ctx.note(f'calculated field "{name}": removed the input step "{s.text}" (scenario "{o.title}") - it is only checked')
                continue
            keep.append(s)
        o.steps = keep
        if not o.steps:
            _drop(items, o)
    if n:
        ctx.note(f"{n} input step(s) aimed at calculated fields removed (their Examples cells go with the unused columns)")


def _p_calculated_values(items, ctx):
    """The value a calculated field should show: worked out in code when the rule is readable from the TS (days between
    two relative dates), otherwise only 'not empty'."""
    calc = [f for f in ctx.fields if f.get("calculated") and f.get("computed_from")]
    if not calc:
        return
    n = 0
    for o in _outlines(items):
        for s in list(o.steps):
            m = _SHOW_RE.match(s.text)
            f = _calc_name(ctx, m.group(1)) if m else None
            if not f or not f.get("computed_from"):
                continue
            rule = f.get("rule") or {}
            srcs = {}
            if rule.get("kind") == "days_between":
                for st in o.steps:
                    fs = _fs(st)
                    fld = ctx.field(fs[2]) if fs else None
                    if fld and fld["name"] in (rule.get("from_field"), rule.get("to_field")):
                        srcs[fld["name"]] = fs[1]

            def compute(rm: dict) -> str:
                if rule.get("kind") != "days_between" or len(srcs) < 2:
                    return "(not empty)"
                v = calculated_fields.days_between(_resolve(srcs[rule["from_field"]], rm), _resolve(srcs[rule["to_field"]], rm),
                                                   bool(rule.get("inclusive")))
                return str(v) if v is not None and v >= 1 else "(not empty)"

            ph = re.fullmatch(r"<([^<>]+)>", m.group(2))
            t = o.tables[0] if o.tables else None
            if ph and t is not None and _col(t, ph.group(1)) >= 0:
                ci = _col(t, ph.group(1))
                for row in t.rows:
                    new = compute(_row_map(t, row))
                    if ci < len(row) and row[ci] != new:
                        row[ci] = new
                        n += 1
            elif not ph:
                new = compute({})
                if m.group(2) != new:
                    s.text = f'the "{m.group(1)}" field should show "{new}"'
                    n += 1
    if n:
        ctx.note(f"{n} expected value(s) of calculated fields set by code (worked out from the rule, else '(not empty)')")


def _p_dates(items, ctx):
    """Date values are relative (today, today+N, today-N) so the file never goes stale: a literal date is converted."""
    n = bad = 0

    def rel(v: str) -> str:
        nonlocal n, bad
        if not v.strip():
            return v
        r = calculated_fields.to_relative(v, ctx.date_format)
        if r is None:
            bad += 1
            return v
        if r != v:
            n += 1
        return r

    for o in _outlines(items):
        for s in o.steps:
            f = _fs(s)
            if not f or f[3] != "date":
                continue
            ph = re.fullmatch(r"<([^<>]+)>", f[1])
            if ph and o.tables:
                for t in o.tables:
                    ci = _col(t, ph.group(1))
                    if ci >= 0:
                        for row in t.rows:
                            if ci < len(row):
                                row[ci] = rel(row[ci])
            elif not ph:
                new = rel(f[1])
                if new != f[1]:
                    s.text = s.text.replace(f'"{f[1]}"', f'"{new}"', 1)
    if n:
        ctx.note(f"{n} literal date(s) made relative (today+N), so the file does not go stale")
    if bad:
        ctx.note(f"{bad} date value(s) could not be read as a date and were left as written")


def _row_view(o: Outline, t: Table, row: list, ctx) -> dict:
    """What one row of an update scenario does: record, {field: value}, cleared fields, message."""
    rm = _row_map(t, row)
    key = ""
    for s in o.steps:
        m = re.match(r'^I open the existing record "([^"]*)" using the "([^"]*)" field$', s.text)
        if m:
            key = _resolve(m.group(1), rm).strip()
    changes, cleared, msg = {}, [], ""
    for s in o.steps:
        f = _fs(s)
        if f:
            v = _resolve(f[1], rm).strip()
            fld = ctx.field(f[2])
            if v:
                changes[fld["name"] if fld else f[2]] = v
            continue
        m = _CLEAR_RE.match(s.text)
        if m:
            cleared.append(_resolve(m.group(1), rm))
            continue
        m = re.match(r'^I should see a confirmation message "(.*)"$', s.text)
        if m:
            msg = _resolve(m.group(1), rm)
    return {"key": key, "changes": changes, "cleared": cleared, "message": msg}


def _is_update_outline(o: Outline) -> bool:
    return bool(o.tables) and _has(o, r"^I click the Update button$") and _has(o, r"^I open the existing record")


def _alt_value(entry: Optional[dict], val: str, cur: str, used: set) -> Optional[str]:
    if not entry:
        return None
    for strict in (True, False):
        for _cat, v in entry.get("values") or []:
            if _n(v) in (_n(cur), _n(val)) or (strict and v.lower() in used):
                continue
            return v
    return None


def _p_update_values(items, ctx):
    """Every update row changes a field to a value the record does not hold yet, and no two rows of one column repeat a
    value (when the update data pool has another one). The record's current values are tracked through the file."""
    if not ctx.key_field:
        return
    recs = {r["key"].lower(): dict(r["fields"]) for r in _collect_records(items, ctx)}
    changed = 0
    for o in _outlines(items):
        if not _is_update_outline(o):
            continue
        t = o.tables[0]
        fsteps = [(s, _fs(s)) for s in o.steps if _fs(s)]
        used: dict = {}
        for row in t.rows:
            view = _row_view(o, t, row, ctx)
            cur = recs.get(view["key"].lower())
            if cur is None:
                continue
            for s, fs in fsteps:
                ph = re.fullmatch(r"<([^<>]+)>", fs[1])
                ci = _col(t, ph.group(1)) if ph else -1
                fld = ctx.field(fs[2])
                if ci < 0 or ci >= len(row) or not row[ci].strip() or not fld or fld["name"] == ctx.key_field:
                    continue
                val = row[ci].strip()
                seen = used.setdefault(ci, set())
                if _n(val) == _n(cur.get(fld["name"], "")) or val.lower() in seen:
                    alt = _alt_value(ctx.update_pool.get(fld["name"]), val, cur.get(fld["name"], ""), seen)
                    if alt and _n(alt) != _n(val):
                        ctx.note(f'update row "{row[0] if row else ""}": "{val}" is what the record already holds / another row uses - now "{alt}"')
                        row[ci] = alt
                        val = alt
                        changed += 1
                seen.add(val.lower())
            view = _row_view(o, t, row, ctx)
            ok = not view["message"] or (re.search(r"success|updated|saved", view["message"], re.I)
                                         and not re.search(r"should not be empty", view["message"], re.I))
            if ok:
                cur.update(view["changes"])
                for c in view["cleared"]:
                    cur.pop((ctx.field(c) or {}).get("name", c), None)
    if changed:
        ctx.note(f"{changed} update value(s) replaced so every update changes the record to something new")


def _p_update_dedupe(items, ctx):
    """No two update rows with identical values (same record, same changes, same clears, same message)."""
    seen: set = set()
    removed = 0
    for o in list(_outlines(items)):
        if not _is_update_outline(o):
            continue
        for t in o.tables:
            keep = []
            for row in t.rows:
                v = _row_view(o, t, row, ctx)
                sig = (v["key"].lower(), tuple(sorted((_n(f), x.lower()) for f, x in v["changes"].items())),
                       tuple(sorted(_n(c) for c in v["cleared"])), v["message"].lower(), o.title.lower())
                if sig in seen:
                    removed += 1
                    continue
                seen.add(sig)
                keep.append(row)
            t.rows = keep
        if all(not t.rows for t in o.tables):
            _drop(items, o)
    if removed:
        ctx.note(f"{removed} update row(s) removed: same record and same values as another update row")


# =========================================================================== entry
_PASSES = (
    _p_navigation, _p_background, _p_normalize_steps, _p_vocabulary, _p_message_braces, _p_unknown_fields, _p_calculated,
    _p_dates, _p_form_state_start, _p_ensure_open, _p_options,
    _p_overlength, _p_create_clear, _p_clear_rows, _p_new_record_steps, _p_open_by_key, _p_collapse,
    _p_duplicate_outline, _p_save_form_state, _p_unique_keys, _p_edit_button, _p_delete_flow,
    _p_update_open, _p_update_key, _p_update_typed, _p_update_clear, _p_update_label_rows,
    _p_visibility, _p_open_existing, _p_list_behaviour, _p_known_records, _p_retitle_misleading,
    _p_coverage_creates, _p_drop_weak, _p_drop_pointless, _p_order_creates, _p_calculated_values, _p_update_values,
    _p_update_dedupe, _p_unused_columns, _p_cleanup, _p_keywords,
)


def fix_feature(feature_text: str, project_analysis: dict | None = None, behavior: dict | None = None,
                controls: dict | None = None):
    """Returns (feature_text, {"notes": [...]}). On any error the original feature is returned."""
    if not feature_text or "Scenario" not in feature_text:
        return feature_text, {"notes": []}
    try:
        ctx = Ctx(project_analysis, behavior, controls)
        items = parse(feature_text)
        for fn in _PASSES:
            try:
                fn(items, ctx)
            except Exception:  # noqa: BLE001  - one broken pass must not lose the others
                logger.exception("feature_fixer pass %s failed - skipped", fn.__name__)
                ctx.note(f"{fn.__name__} skipped (error, see the backend log)")
        text = render(items)
        if "Feature:" in feature_text and "Feature:" not in text:
            return feature_text, {"notes": ["feature fixer output was invalid - kept the original"]}
        return text, {"notes": ctx.notes}
    except Exception:  # noqa: BLE001
        logger.exception("feature_fixer failed - keeping the feature as it was")
        return feature_text, {"notes": ["feature fixer failed - kept the original"]}