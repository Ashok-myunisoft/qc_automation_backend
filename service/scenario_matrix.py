"""Builds the WHOLE feature file by CODE from the screen spec (formjson + rendered fields + messages) and the
behaviour facts read from the screen's own TS / HTML (service/behavior_facts.py).

Why code and not the LLM: a small model cannot be relied on to write dozens of distinct, correct rows. Here the
coverage is guaranteed by construction:

  * every outline is fully parameterised - Examples = Condition | one column per field | ExpectedResult - and no
    test value is written inside a step;
  * every Create row has its own realistic code / name (service/matrix_values.py);
  * Update / Delete / Read / List rows use records that an earlier row of the SAME file created;
  * every outline is built only from things the screen really has (fields, options, required messages, buttons,
    dialogs, list features), so a screen without a list / modal / delete simply gets fewer outlines.

Used only when USE_MATRIX_PLANNER=1 (see app.py). feature_quality / expected_results are NOT run on this output
(they would rewrite or drop rows that are already consistent); normalize_feature and the step checker still are.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from service import matrix_values, script_builder

logger = logging.getLogger(__name__)

ROW_CAP = 25
_CONFIG = Path(__file__).resolve().parent.parent / "config" / "expected_messages.json"


# --------------------------------------------------------------------------- tiny helpers
def _first(seq, default=""):
    return seq[0] if seq else default


def _confirmed(words: tuple[str, ...], default: str) -> str:
    try:
        data = json.loads(_CONFIG.read_text(encoding="utf-8"))
        for m in data.get("all") or []:
            low = str(m).lower()
            if all(w in low for w in words):
                return str(m).strip()
    except Exception:
        pass
    return default


def _tc(text: str) -> str:
    return matrix_values.clean(text)


class Outline:
    def __init__(self, title: str, steps: list[str], columns: list[str], expected: bool = True):
        self.title, self.steps, self.columns, self.expected = title, steps, list(columns), expected
        self.rows: list[dict] = []

    def add(self, condition: str, expected: str = "", **cells) -> dict:
        condition = _tc(condition)
        taken = {r["Condition"] for r in self.rows}
        base, n = condition, 1
        while condition in taken:
            n += 1
            condition = f"{base} #{n}"
        row = {"Condition": condition, "ExpectedResult": _tc(expected)}
        for c in self.columns:
            row[c] = matrix_values.clean(cells.get(c, ""))
        self.rows.append(row)
        return row

    def render(self) -> list[str]:
        out: list[str] = []
        if not self.rows:
            return out
        cols = ["Condition"] + self.columns + (["ExpectedResult"] if self.expected else [])
        chunks = [self.rows[i:i + ROW_CAP] for i in range(0, len(self.rows), ROW_CAP)]
        for part, chunk in enumerate(chunks, 1):
            title = self.title + (f" (part {part})" if len(chunks) > 1 else "")
            out.append(f'  Scenario Outline: {title} - "<Condition>"')
            prev_assert = False
            for i, s in enumerate(self.steps):
                assertion = s.startswith(("I should see", "I should not see", "I should still see", "I should no longer see",
                                          "the ", "I should")) 
                if i == 0:
                    kw = "Given" if assertion else "When"
                elif assertion and not prev_assert:
                    kw = "Then"
                else:
                    kw = "And"
                prev_assert = assertion
                out.append(f"    {kw} {s}")
            out.append("")
            out.append("    Examples:")
            widths = [max(len(c), *(len(r[c]) for r in chunk)) for c in cols]
            out.append("      | " + " | ".join(c.ljust(w) for c, w in zip(cols, widths)) + " |")
            for r in chunk:
                out.append("      | " + " | ".join(r[c].ljust(w) for c, w in zip(cols, widths)) + " |")
            out.append("")
        return out


# --------------------------------------------------------------------------- the builder
class Matrix:
    def __init__(self, spec: dict, behavior: dict | None, nav: list[str], screen_name: str,
                 business_context: dict | None = None, llm_values: dict | None = None):
        self.spec = spec
        self.b = behavior or {}
        self.lst = self.b.get("list") or {}
        self.roles = self.b.get("roles") or {}
        self.nav = nav or []
        self.screen = screen_name or spec.get("screen_name") or spec.get("formname") or "Screen"
        spec.setdefault("screen_name", self.screen)
        self.vf = matrix_values.ValueFactory(spec, business_context, llm_values)
        self.notes: list[str] = []
        self.outlines: list[Outline] = []
        self.records: list[dict] = []     # {vals, tag, saved}
        self.fields = [f for f in spec["fields"]
                       if f.get("active") and not f.get("readonly") and f.get("kind") != "grid"
                       and not f.get("record_picker")]
        self.record_pickers = [f for f in spec["fields"] if f.get("active") and f.get("record_picker")]
        msgs = spec.get("messages") or {}
        self.m_save = _first(msgs.get("save")) or "Details Saved Successfully"
        self.m_update = _first(msgs.get("update")) or _confirmed(("updated", "success"), "Details Updated Successfully")
        self.m_delete = _first(msgs.get("delete")) or _confirmed(("deleted",), "Details have been deleted successfully")
        self.m_dup = _first(msgs.get("duplicate"))
        self.dup_verified = bool(self.m_dup)
        if not self.m_dup:
            self.m_dup = "Unable to save the Details already exist in the database."
        self.keys = [f for f in self.fields if f.get("kind") == "picklist" and (f.get("key_field") or f.get("add_new"))]
        self.open_field = (self.record_pickers[0]["name"] if self.record_pickers
                           else (self.keys[0]["name"] if self.keys else ""))
        self.has_new_button = bool(self.roles.get("new")) or not self.b.get("found") \
            or any("addnew" in re.sub(r"[^a-z]", "", str(x).lower()) for x in spec.get("buttons") or [])
        self.dialogs = self.b.get("dialogs") or []
        self.confirm_form = next((d["fragment"] for d in self.dialogs if d.get("is_confirm") and d["method"] == "Delete"), "")
        self.confirm_row = next((d["fragment"] for d in self.dialogs if d.get("is_confirm") and d["method"] != "Delete"), "")
        self.confirm_any = self.confirm_form or self.confirm_row or "Are you sure you want to delete"
        self.save_branch = next((x for x in self.b.get("branches") or [] if x.get("service_call") == "formsaveservice"), {})
        self.modal_closes = bool((self.save_branch.get("on_200") or {}).get("closes_modal"))
        self.form_modal = bool(self.b.get("modal"))

    # ------------------------------------------------------------------ steps
    def set_step(self, f: dict, ph: str | None = None) -> str:
        ph = ph or f["name"]
        kind = f["kind"]
        if kind == "picklist" and (f.get("key_field") or f.get("add_new")):
            return f'I enter "<{ph}>" in the "{f["name"]}" new record field'
        sentence = script_builder._KIND_SENTENCE.get(kind) or script_builder._KIND_SENTENCE["input"]
        return sentence[1].format(v=f"<{ph}>", f=f["name"])

    def create_steps(self, fields: list[dict], save: bool = True) -> list[str]:
        steps = ["I click the Add New button"] if self.has_new_button else []
        steps += [self.set_step(f) for f in fields]
        if save:
            steps.append("I click the save button")
        return steps

    def _options(self, f: dict) -> list[str]:
        return [o["text"] for o in f.get("options", []) if not o.get("blank") and str(o.get("text", "")).strip()]

    def _blank_option(self, f: dict) -> str:
        return next((o["text"] for o in f.get("options", []) if o.get("blank") and str(o.get("text", "")).strip()), "")

    def _required(self, f: dict) -> bool:
        return bool(f.get("required"))

    # ------------------------------------------------------------------ rows / records
    def base(self, style: str = "valid") -> dict:
        return {f["name"]: self.vf.valid(f, "valid") for f in self.fields}

    def optional_empty(self, vals: dict) -> dict:
        v = dict(vals)
        for f in self.fields:
            if self._required(f):
                continue
            v[f["name"]] = "Unchecked" if f["kind"] == "checkbox" else ""
        return v

    def _record(self, vals: dict, tag: str) -> dict:
        rec = {"vals": dict(vals), "tag": tag}
        self.records.append(rec)
        return rec

    def rec_by_tag(self, tag: str) -> list[dict]:
        return [r for r in self.records if r["tag"] == tag]

    def code_of(self, rec: dict) -> str:
        return rec["vals"].get(self.open_field, "")

    def name_field(self) -> dict | None:
        for f in self.keys:
            if f["name"] != self.open_field:
                return f
        return None

    def type_field(self) -> dict | None:
        return next((f for f in self.fields if f["kind"] == "combobox" and self._options(f)), None)

    def check_field(self) -> dict | None:
        return next((f for f in self.fields if f["kind"] == "checkbox"), None)

    def text_fields(self) -> list[dict]:
        return [f for f in self.fields if f["kind"] in ("input", "textarea") or (f["kind"] == "picklist" and f in self.keys)]

    def required_msg(self, f: dict) -> str:
        return f.get("required_message") or ""

    # ------------------------------------------------------------------ outlines
    def add(self, o: Outline | None):
        if o and o.rows:
            self.outlines.append(o)

    def cols(self, fields=None) -> list[str]:
        return [f["name"] for f in (fields if fields is not None else self.fields)]

    # ---- form state / smoke
    def form_state(self):
        if not (self.b.get("found") and self.form_modal and self.roles):
            return
        new_state: list[tuple[str, str]] = []
        edit_state: list[tuple[str, str]] = []
        save = self.roles.get("save") or {}
        if save.get("title_when_true"):
            new_state += [(save.get("title_when_false") or "Save", "visible"), (save["title_when_true"], "hidden")]
            edit_state += [(save["title_when_true"], "visible"), (save.get("title_when_false") or "Save", "hidden")]
        dele = self.roles.get("delete") or {}
        if dele.get("title") and "isEditMode" in (dele.get("shown_when") or ""):
            new_state.append((dele["title"], "hidden"))
            edit_state.append((dele["title"], "visible"))
        if new_state and self.has_new_button:
            o = Outline("New record form shows the right actions",
                        ["I click the Add New button", 'the "<Action>" action should be "<State>"'], ["Action", "State"], expected=False)
            for action, state in new_state:
                o.add(f"{action} {state} for a new record", Action=action, State=state)
            o.steps.insert(1, "the form should be open")
            self.add(o)
        self._edit_state = edit_state

    def edit_form_state(self):
        edit_state = getattr(self, "_edit_state", [])
        recs = self.rec_by_tag("gen")
        if not edit_state or not recs or not self.open_field:
            return
        o = Outline("Existing record form shows the right actions",
                    [f'I open the existing record "<Record>" using the "{self.open_field}" field',
                     "the form should be open", 'the "<Action>" action should be "<State>"'],
                    ["Record", "Action", "State"], expected=False)
        for action, state in edit_state:
            o.add(f"{action} {state} for an existing record", Record=self.code_of(recs[0]), Action=action, State=state)
        self.add(o)

    # ---- create
    def create_valid(self):
        if not self.fields:
            return
        o = Outline(f"Create {self.screen}", self.create_steps(self.fields) + ["I should see a confirmation message \"<ExpectedResult>\""],
                    self.cols())
        ok = self.m_save

        def row(cond, vals, tag="gen"):
            o.add(cond, ok, **vals)
            self._record(vals, tag)

        row("All fields valid", self.base())
        row("Required fields only", self.optional_empty(self.base()))
        txt = self.text_fields()
        # short values
        v = self.base()
        for f in txt:
            if f in self.keys or f["kind"] == "input":
                v[f["name"]] = self.vf.valid(f, "short") if not self._required(f) or f["kind"] != "textarea" else v[f["name"]]
        row("Very short values", v)
        # special characters
        v = self.base()
        for f in txt:
            if self.vf.is_unique_field(f) and f["name"] != self.open_field:
                v[f["name"]] = self.vf.valid(f, "special")
            elif f["kind"] == "textarea":
                v[f["name"]] = self.vf.valid(f, "special")
        if v != self.base() or True:
            row("Special characters in name and description", v)
        # code styles
        code_f = next((f for f in self.fields if f["name"] == self.open_field), None)
        if code_f and code_f["kind"] in ("picklist", "input"):
            for style, label in (("digits", "Digits only code"), ("mixed", "Mixed case code")):
                v = self.base()
                v[code_f["name"]] = self.vf.valid(code_f, style)
                row(label, v)
        # boundaries
        for f in txt:
            if not f.get("max_length"):
                continue
            v = self.base()
            v[f["name"]] = self.vf.valid(f, "max")
            row(f"Maximum length {f['label']} ({f['max_length']} characters)", v)
        # inner spaces / long realistic descriptions
        for f in txt:
            if f["kind"] == "textarea":
                v = self.base()
                v[f["name"]] = " ".join(matrix_values.DESC_BANKS[self.vf.bank][:3])[: f.get("max_length") or 200]
                row(f"Long multi-sentence {f['label']}", v)
        # other kinds: numbers / dates / lookups
        for f in self.fields:
            if f.get("type") in ("number", "int", "decimal", "percentage") and f["kind"] == "input":
                for style, label in (("short", "minimum"), ("max", "maximum")):
                    v = self.base()
                    v[f["name"]] = self.vf.number(f, style)
                    row(f"{f['label']} {label} value", v)
        self.add(o)

    def create_required(self):
        req = [f for f in self.fields if self._required(f) and self.required_msg(f)]
        if not req:
            return
        o = Outline(f"Create {self.screen} required fields", self.create_steps(self.fields) + ["I should see a confirmation message \"<ExpectedResult>\""]
                    + (["the form should still be open"] if self.modal_closes else []), self.cols())
        for f in req:
            v = self.base()
            v[f["name"]] = ""
            o.add(f"{f['label']} left empty", self.required_msg(f), **v)
        if len(req) > 1:
            v = {f["name"]: self.base()[f["name"]] for f in self.fields}
            for f in req:
                v[f["name"]] = ""
            o.add("All required fields empty", ", ".join(self.required_msg(f) for f in req), **v)
        self.add(o)

    def create_options(self):
        tf = self.type_field()
        if not tf:
            return
        o = Outline(f"Create {self.screen} with each {tf['label']}", self.create_steps(self.fields) + ["I should see a confirmation message \"<ExpectedResult>\""], self.cols())
        for opt in self._options(tf):
            v = self.base()
            v[tf["name"]] = opt
            o.add(f"{tf['label']} {opt}", self.m_save, **v)
            rec = self._record(v, "opt")
            rec["type_text"] = opt
        self.add(o)

    def create_checkbox(self):
        cf = self.check_field()
        if not cf:
            return
        o = Outline(f"Create {self.screen} with {cf['label']} state", self.create_steps(self.fields) + ["I should see a confirmation message \"<ExpectedResult>\""], self.cols())
        for state in ("Checked", "Unchecked"):
            v = self.base()
            v[cf["name"]] = state
            o.add(f"{cf['label']} {state}", self.m_save, **v)
            self._record(v, "chk")
        self.add(o)

    def create_duplicate(self):
        gen = self.rec_by_tag("gen")
        if not gen or not self.open_field or not self.keys:
            return
        o = Outline(f"Create duplicate {self.screen}", self.create_steps(self.fields) + ["I should see a confirmation message \"<ExpectedResult>\""]
                    + (["the form should still be open"] if self.modal_closes else []), self.cols())
        v = self.base()
        v[self.open_field] = self.code_of(gen[0])
        cond = "Duplicate code" + ("" if self.dup_verified else " (standard ERP message)")
        o.add(cond, self.m_dup, **v)
        if len(gen) > 1:
            v = self.base()
            v[self.open_field] = self.code_of(gen[1]).lower() if self.code_of(gen[1]).lower() != self.code_of(gen[1]) else self.code_of(gen[1]).upper()
            o.add("Duplicate code in a different letter case (depends on database collation)", self.m_dup, **v)
        self.add(o)

    def create_then_list(self):
        if not (self.lst and self.open_field):
            return
        cf, st = self.check_field(), self.lst.get("status")
        o = Outline(f"Created {self.screen} appears in the list",
                    self.create_steps(self.fields) + ["I should see a confirmation message \"<ExpectedResult>\"",
                                                       f'I should see the record "<{self.open_field}>" in the list']
                    + (['the list row "<%s>" should show "<Status>"' % self.open_field] if (cf and st) else []),
                    self.cols() + (["Status"] if (cf and st) else []))
        for state in (("Checked", "Unchecked") if cf else ("Checked",)):
            v = self.base()
            if cf:
                v[cf["name"]] = state
            cells = dict(v)
            if cf and st:
                cells["Status"] = st["first_text"] if state == "Checked" else st["else_text"]
            o.add(f"Saved with {state if cf else 'valid data'} shows in the list", self.m_save, **cells)
            self._record(v, "list")
        self.add(o)

    # ---- read / update
    def read_view(self):
        recs = (self.rec_by_tag("gen") + self.rec_by_tag("opt"))[:6]
        if not (recs and self.open_field):
            return
        shown = [f for f in self.fields if f["kind"] != "picklist" or f in self.keys]
        steps = [f'I open the existing record "<Record>" using the "{self.open_field}" field', "the form should be open"]
        steps += [f'the "{f["name"]}" field should show "<{f["name"]}>"' for f in shown if f["name"] != self.open_field]
        o = Outline(f"Open an existing {self.screen} and check its values", steps,
                    ["Record"] + [f["name"] for f in shown if f["name"] != self.open_field], expected=False)
        for rec in recs:
            cells = {"Record": self.code_of(rec)}
            cells.update({f["name"]: rec["vals"].get(f["name"], "") for f in shown if f["name"] != self.open_field})
            o.add(f"Saved values are shown for {self.code_of(rec)}", **cells)
        self.add(o)

    def _editable(self) -> list[dict]:
        return [f for f in self.fields if f["name"] != self.open_field and f not in self.keys and f["kind"] != "picklist"]

    def update_rows(self):
        recs = self.rec_by_tag("gen")[:3] + self.rec_by_tag("opt")[:1]
        ed = self._editable()
        if not (recs and ed and self.open_field):
            return
        upd_step = "I click the Update button"
        steps = [f'I open the existing record "<Record>" using the "{self.open_field}" field'] + [self.set_step(f) for f in ed] \
            + [upd_step, "I should see a confirmation message \"<ExpectedResult>\""]
        o = Outline(f"Update {self.screen}", steps, ["Record"] + self.cols(ed))
        i = 0

        def nxt():
            nonlocal i
            r = recs[i % len(recs)]
            i += 1
            return self.code_of(r)

        for f in ed:
            if f["kind"] == "combobox":
                cur = recs[0]["vals"].get(f["name"], "")
                for opt in self._options(f):
                    if opt == cur:
                        continue
                    o.add(f"Change {f['label']} to {opt}", self.m_update, Record=nxt(), **{f["name"]: opt})
            elif f["kind"] == "checkbox":
                for state in ("Unchecked", "Checked"):
                    o.add(f"Set {f['label']} {state}", self.m_update, Record=nxt(), **{f["name"]: state})
            elif f["kind"] in ("textarea", "input"):
                o.add(f"Change {f['label']}", self.m_update, Record=nxt(), **{f["name"]: self.vf.valid(f, "valid")})
                if f.get("max_length"):
                    o.add(f"{f['label']} changed to the maximum length", self.m_update, Record=nxt(), **{f["name"]: self.vf.valid(f, "max")})
                o.add(f"{f['label']} with special characters", self.m_update, Record=nxt(), **{f["name"]: self.vf.valid(f, "special")})
        cells = {f["name"]: self.vf.valid(f, "valid") for f in ed}
        for f in ed:
            if f["kind"] == "combobox":
                opts = self._options(f)
                cells[f["name"]] = opts[-1] if opts else ""
            if f["kind"] == "checkbox":
                cells[f["name"]] = "Unchecked"
        o.add("Change every editable field", self.m_update, Record=nxt(), **cells)
        o.add("Update with no change", self.m_update, Record=nxt())
        self.add(o)

    def update_negative(self):
        recs = self.rec_by_tag("gen")[:3]
        ed = [f for f in self._editable() if self._required(f) and self.required_msg(f)]
        if not (recs and ed and self.open_field):
            return
        txt = [f for f in ed if f["kind"] in ("input", "textarea")]
        if txt:
            o = Outline(f"Update {self.screen} with a required field cleared",
                        [f'I open the existing record "<Record>" using the "{self.open_field}" field',
                         'I clear the "<ClearField>" field', "I click the Update button",
                         "I should see a confirmation message \"<ExpectedResult>\""]
                        + (["the form should still be open"] if self.modal_closes else []),
                        ["Record", "ClearField"])
            for k, f in enumerate(txt):
                o.add(f"{f['label']} cleared", self.required_msg(f), Record=self.code_of(recs[k % len(recs)]), ClearField=f["name"])
            self.add(o)
        for f in [x for x in ed if x["kind"] == "combobox" and self._blank_option(x)]:
            o = Outline(f"Update {self.screen} with {f['label']} reset to blank",
                        [f'I open the existing record "<Record>" using the "{self.open_field}" field',
                         self.set_step(f, "Blank"), "I click the Update button",
                         "I should see a confirmation message \"<ExpectedResult>\""]
                        + (["the form should still be open"] if self.modal_closes else []),
                        ["Record", "Blank"])
            o.add(f"{f['label']} set back to {self._blank_option(f)}", self.required_msg(f),
                  Record=self.code_of(recs[0]), Blank=self._blank_option(f))
            self.add(o)

    # ---- reset / close
    def reset_close(self):
        fill = [f for f in self.fields]
        if self.roles.get("reset") and self.has_new_button:
            o = Outline("Reset clears the new record form",
                        self.create_steps(fill, save=False) + ["I click the Reset button", "the form fields should be empty"],
                        self.cols(fill), expected=False)
            full = self.base()
            o.add("Reset after every field is filled", **full)
            for f in fill[:6]:
                if f["kind"] in ("input", "textarea", "picklist"):
                    o.add(f"Reset after only {f['label']} is filled", **{f["name"]: full[f["name"]]})
            self.add(o)
            recs = self.rec_by_tag("gen")[:2]
            if recs and self.open_field:
                o = Outline("Reset on an open record switches to a new record",
                            [f'I open the existing record "<Record>" using the "{self.open_field}" field', "I click the Reset button",
                             "the form fields should be empty"] + ([f'the "{self._del_title()}" action should be "hidden"'] if self._del_title() else []),
                            ["Record"], expected=False)
                for r in recs:
                    o.add(f"Reset on record {self.code_of(r)}", Record=self.code_of(r))
                self.add(o)
        if self.roles.get("close") and self.has_new_button:
            steps = self.create_steps(fill, save=False) + ["I click the Close button", "the form should be closed"]
            if self.lst and self.open_field:
                steps += [f'I search the list for "<{self.open_field}>"', f'I should not see the record "<{self.open_field}>" in the list']
            o = Outline("Close discards the new record", steps, self.cols(fill), expected=False)
            o.add("Close after every field is filled", **self.base())
            o.add("Close after only the code is filled", **({self.open_field: self.vf.valid(next(f for f in fill if f["name"] == self.open_field))} if self.open_field else {}))
            self.add(o)
            o = Outline("Close an empty new record form", ["I click the Add New button", "the form should be open", "I click the Close button", "the form should be closed"], [], expected=False)
            o.add("Open and close without typing")
            self.add(o)

    def _del_title(self) -> str:
        d = self.roles.get("delete") or {}
        return d.get("title", "") if "isEditMode" in (d.get("shown_when") or "") else ""

    # ---- delete
    def delete_rows(self):
        gen = self.rec_by_tag("gen")[3:]
        if not (gen and self.open_field):
            return
        have_list = bool(self.lst.get("row_actions"))
        have_form = bool(self.roles.get("delete"))
        pick = list(gen)
        fld = self.open_field
        msg_prompt_row = self.confirm_row or self.confirm_any
        msg_prompt_form = self.confirm_form or self.confirm_any
        if have_list and pick:
            o = Outline(f"Delete {self.screen} from the list - confirm",
                        [f'I request to delete the record "<Record>" using the "{fld}" field', 'I should see the delete prompt "<Prompt>"',
                         "I confirm the delete", "I should see a confirmation message \"<ExpectedResult>\"",
                         f'I should no longer see the record "<Record>" using the "{fld}" field'], ["Record", "Prompt"])
            for r in pick[:2]:
                o.add(f"Confirm delete of {self.code_of(r)} from the list", self.m_delete, Record=self.code_of(r), Prompt=msg_prompt_row)
            pick = pick[2:]
            self.add(o)
        if have_form and pick:
            o = Outline(f"Delete {self.screen} from the open form - confirm",
                        [f'I open the existing record "<Record>" using the "{fld}" field', "I request to delete the open record",
                         'I should see the delete prompt "<Prompt>"', "I confirm the delete",
                         "I should see a confirmation message \"<ExpectedResult>\"",
                         f'I should no longer see the record "<Record>" using the "{fld}" field'], ["Record", "Prompt"])
            for r in pick[:2]:
                o.add(f"Confirm delete of {self.code_of(r)} from the form", self.m_delete, Record=self.code_of(r), Prompt=msg_prompt_form)
            pick = pick[2:]
            self.add(o)
        keep = self.rec_by_tag("gen")[:3]
        if keep and have_list:
            o = Outline(f"Delete {self.screen} from the list - cancel",
                        [f'I request to delete the record "<Record>" using the "{fld}" field', 'I should see the delete prompt "<Prompt>"',
                         "I cancel the delete", f'I should still see the record "<Record>" using the "{fld}" field'], ["Record", "Prompt"], expected=False)
            o.add(f"Cancel delete of {self.code_of(keep[0])} from the list", Record=self.code_of(keep[0]), Prompt=msg_prompt_row)
            self.add(o)
        if keep and have_form:
            o = Outline(f"Delete {self.screen} from the open form - cancel",
                        [f'I open the existing record "<Record>" using the "{fld}" field', "I request to delete the open record",
                         'I should see the delete prompt "<Prompt>"', "I cancel the delete",
                         f'I should still see the record "<Record>" using the "{fld}" field'], ["Record", "Prompt"], expected=False)
            o.add(f"Cancel delete of {self.code_of(keep[-1])} from the form", Record=self.code_of(keep[-1]), Prompt=msg_prompt_form)
            self.add(o)

    # ---- list features
    def list_rows(self):
        if not (self.lst and self.open_field):
            return
        opt = self.rec_by_tag("opt")
        pool = self.rec_by_tag("gen")[:3] + opt
        if not pool:
            return
        fld = self.open_field
        nf = self.name_field()
        search = self.lst.get("search")
        sf = [x.lower() for x in self.b.get("search_fields") or []]
        if search:
            o = Outline(f"Search the {self.screen} list",
                        ['I search the list for "<SearchTerm>"', 'I should see the record "<Expected>" in the list'], ["SearchTerm", "Expected"], expected=False)
            r = pool[0]
            code = self.code_of(r)
            if not sf or fld.lower() in sf:
                o.add("Search by the full code", SearchTerm=code, Expected=code)
                if code.lower() != code:
                    o.add("Search by the code in lower case", SearchTerm=code.lower(), Expected=code)
                if code.upper() != code:
                    o.add("Search by the code in upper case", SearchTerm=code.upper(), Expected=code)
                o.add("Search by the first letters of the code", SearchTerm=code[:max(len(code) - 2, 2)], Expected=code)
            if nf and (not sf or nf["name"].lower() in sf):
                nm = r["vals"].get(nf["name"], "")
                if nm:
                    o.add("Search by the full name", SearchTerm=nm, Expected=code)
                    o.add("Search by the first word of the name", SearchTerm=nm.split()[0], Expected=code)
                    o.add("Search by the name in upper case", SearchTerm=nm.upper(), Expected=code)
            df = next((f for f in self.fields if f["kind"] == "textarea"), None)
            if df and (not sf or df["name"].lower() in sf):
                d = r["vals"].get(df["name"], "")
                words = [w for w in re.findall(r"[A-Za-z]{6,}", d)]
                if words:
                    o.add("Search by a word of the description", SearchTerm=words[0], Expected=code)
            for r2 in pool[1:3]:
                o.add(f"Search by the code {self.code_of(r2)}", SearchTerm=self.code_of(r2), Expected=self.code_of(r2))
            self.add(o)
            es = self.lst.get("empty_state") or {}
            if es.get("message"):
                o = Outline(f"Search the {self.screen} list with no match",
                            ['I search the list for "<SearchTerm>"', 'I should see the empty state "<Message>"'], ["SearchTerm", "Message"], expected=False)
                tag = self.vf.tag
                for cond, term in (("Code that does not exist", f"ZZ{tag}9X"), ("Special characters only", "@#$%"),
                                   ("Long text that matches nothing", f"nothing matches {tag} here")):
                    o.add(cond, SearchTerm=term, Message=es["message"])
                self.add(o)
            o = Outline(f"Clear the {self.screen} list search",
                        ['I search the list for "<SearchTerm>"', "I clear the list search", 'I should see the record "<Expected>" in the list'],
                        ["SearchTerm", "Expected"], expected=False)
            o.add("Clearing the search shows the records again", SearchTerm=f"ZZ{self.vf.tag}9X", Expected=self.code_of(pool[0]))
            self.add(o)
        # filter
        flt = self.lst.get("filter")
        tt = self.b.get("type_text") or {}
        tf = self.type_field()
        if flt and tf and tt and len(opt) >= 2:
            key_of = {o_["text"]: str(o_["key"]) for o_ in tf.get("options", []) if not o_.get("blank")}
            o = Outline(f"Filter the {self.screen} list by {tf['label']}",
                        ['I filter the list by type "<FilterType>"', 'I should see the record "<Shown>" in the list',
                         'I should not see the record "<Hidden>" in the list'], ["FilterType", "Shown", "Hidden"], expected=False)
            for r in opt:
                text = tt.get(key_of.get(r.get("type_text", ""), ""), "")
                other = next((x for x in opt if x is not r), None)
                if text and other:
                    o.add(f"Filter by {text}", FilterType=text, Shown=self.code_of(r), Hidden=self.code_of(other))
            self.add(o)
            o = Outline(f"Show all {self.screen} types in the list",
                        ['I filter the list by type "<FilterType>"', "I filter the list by type \"" + (flt.get("all_option") or "All Types") + "\"",
                         'I should see the record "<Shown>" in the list'], ["FilterType", "Shown"], expected=False)
            for r in opt[:2]:
                text = tt.get(key_of.get(r.get("type_text", ""), ""), "")
                if text:
                    o.add(f"All types after filtering by {text}", FilterType=text, Shown=self.code_of(r))
            self.add(o)
            o = Outline(f"{self.screen} list shows the {tf['label']} and status of a record",
                        [f'I search the list for "<Record>"', 'the list row "<Record>" should show "<TypeText>"'], ["Record", "TypeText"], expected=False)
            for r in opt:
                text = tt.get(key_of.get(r.get("type_text", ""), ""), "")
                if text:
                    o.add(f"{r.get('type_text')} is shown as {text}", Record=self.code_of(r), TypeText=text)
            self.add(o)
        st = self.lst.get("status")
        cf = self.check_field()
        chk = self.rec_by_tag("chk") + self.rec_by_tag("list")
        if st and cf and chk:
            o = Outline(f"{self.screen} list shows the status of a record",
                        ['I search the list for "<Record>"', 'the list row "<Record>" should show "<Status>"'], ["Record", "Status"], expected=False)
            for r in chk:
                state = r["vals"].get(cf["name"], "")
                o.add(f"{cf['label']} {state} is shown as {st['first_text'] if state == 'Checked' else st['else_text']}",
                      Record=self.code_of(r), Status=st["first_text"] if state == "Checked" else st["else_text"])
            self.add(o)
        # sort
        cols = self.lst.get("sortable_columns") or []
        if cols:
            o = Outline(f"Sort the {self.screen} list",
                        ['I sort the list by the "<Column>" column in "<Order>" order',
                         'the list should be sorted by "<Column>" in "<Order>" order'], ["Column", "Order"], expected=False)
            for c in cols:
                for order in ("ascending", "descending"):
                    o.add(f"Sort by {c['label']} {order}", Column=c["label"], Order=order)
            self.add(o)
        # views
        views = self.lst.get("views") or []
        if views:
            o = Outline(f"Switch the {self.screen} list view",
                        ['I switch to the "<View>" view', 'I should see the "<View>" view', f'I should see the record "<Record>" in the list'],
                        ["View", "Record"], expected=False)
            for v in views:
                label = "Card" if v["mode"] == "card" else "List"
                o.add(f"Switch to the {label} view", View=label, Record=self.code_of(pool[0]))
            if len(views) > 1:
                o.add("Switch back to the List view", View="List", Record=self.code_of(pool[-1]))
            self.add(o)

    def counters(self):
        counters = self.lst.get("counters") or []
        cf = self.check_field()
        st = self.lst.get("status")
        total = next((c for c in counters if "total" in c.lower()), "")
        active = next((c for c in counters if "active" in c.lower() and "in" not in c.lower()), "")
        if not (total and self.has_new_button):
            return
        steps = [f'I remember the number shown for "{total}"'] + ([f'I remember the number shown for "{active}"'] if (active and cf) else [])
        steps += self.create_steps(self.fields) + ["I should see a confirmation message \"<ExpectedResult>\"",
                                                   f'the number shown for "{total}" should have changed by "<TotalChange>"']
        cols = self.cols() + ["TotalChange"]
        if active and cf:
            steps.append(f'the number shown for "{active}" should have changed by "<ActiveChange>"')
            cols.append("ActiveChange")
        o = Outline(f"{self.screen} counters change after a save", steps, cols)
        for state in (("Checked", "Unchecked") if (active and cf) else ("Checked",)):
            v = self.base()
            if cf:
                v[cf["name"]] = state
            cells = dict(v, TotalChange="1")
            if active and cf:
                cells["ActiveChange"] = "1" if state == "Checked" else "0"
            o.add(f"Saving a record with {cf['label'] if cf else 'valid data'} {state}", self.m_save, **cells)
            self._record(v, "counter")
        self.add(o)

    # ------------------------------------------------------------------ assemble
    def build(self) -> str:
        for fn in (self.form_state, self.create_valid, self.create_required, self.create_options, self.create_checkbox,
                   self.create_duplicate, self.create_then_list, self.edit_form_state, self.read_view,
                   self.update_rows, self.update_negative, self.list_rows, self.counters, self.reset_close, self.delete_rows):
            try:
                fn()
            except Exception:
                logger.exception("scenario matrix: %s failed - that part is skipped", fn.__name__)
        nav = self.nav or [self.spec.get("module") or "Module", self.screen]
        if len(nav) <= 2:
            nav_step = f'I navigate through the following path "{nav[0]}" -> "{nav[-1]}"'
        else:
            nav_step = f'I navigate through the following path "{nav[0]}" -> "{nav[-2]}" -> "{nav[-1]}"'
        lines = [f"Feature: {self.screen}",
                 f"  As a {nav[0]} user",
                 f"  I want to maintain {self.screen} records",
                 "  So that the data stays correct and complete.",
                 "", "  Background:", f"    Given {nav_step}", ""]
        for o in self.outlines:
            lines += o.render()
        return "\n".join(lines).rstrip() + "\n"


def build_feature(spec: dict, behavior: dict | None, nav: list[str], screen_name: str,
                  business_context: dict | None = None, llm_values: dict | None = None) -> tuple[str, dict]:
    m = Matrix(spec, behavior, nav, screen_name, business_context, llm_values)
    text = m.build()
    rows = sum(len(o.rows) for o in m.outlines)
    info = {"outlines": len(m.outlines), "rows": rows, "records": len(m.records), "notes": m.notes,
            "dup_message_verified": m.dup_verified}
    return text, info


# --------------------------------------------------------------------------- coverage numbers
def coverage_summary(feature_text: str, spec: dict, behavior: dict | None) -> dict:
    """Fields, buttons, branches and list features that the feature really exercises."""
    low = (feature_text or "").lower()
    b = behavior or {}
    lst = b.get("list") or {}
    fields = [f for f in spec.get("fields", []) if f.get("active") and not f.get("readonly") and f.get("kind") != "grid"]
    used = [f["name"] for f in fields if f'"{f["name"].lower()}"' in low]
    items = {
        "add new": "i click the add new button" in low,
        "save": "i click the save button" in low,
        "update": "i click the update button" in low,
        "delete (confirm)": "i confirm the delete" in low,
        "delete (cancel)": "i cancel the delete" in low,
        "reset": "i click the reset button" in low,
        "close": "i click the close button" in low,
        "save fails -> form stays open": "the form should still be open" in low,
        "required messages": "should not be empty" in low,
        "duplicate": "duplicate" in low,
        "search": "i search the list for" in low,
        "filter": "i filter the list by type" in low,
        "sort": "i sort the list by" in low,
        "view toggle": "i switch to the" in low,
        "counters": "the number shown for" in low,
        "empty state": "i should see the empty state" in low,
    }
    applicable = {
        "search": bool(lst.get("search")), "filter": bool(lst.get("filter")), "sort": bool(lst.get("sortable_columns")),
        "view toggle": bool(lst.get("views")), "counters": bool(lst.get("counters")), "empty state": bool(lst.get("empty_state")),
        "reset": bool((b.get("roles") or {}).get("reset")), "close": bool((b.get("roles") or {}).get("close")),
        "delete (confirm)": bool((b.get("roles") or {}).get("delete") or lst.get("row_actions")),
        "delete (cancel)": bool((b.get("roles") or {}).get("delete") or lst.get("row_actions")),
        "save fails -> form stays open": bool(b.get("branches")),
    }
    covered = [k for k, v in items.items() if v and applicable.get(k, True)]
    missing = [k for k, v in items.items() if not v and applicable.get(k, True)]
    return {"fields_used": used, "fields_total": len(fields), "covered": covered, "missing": missing,
            "outlines": len(re.findall(r"^\s*Scenario Outline:", feature_text or "", re.M)),
            "rows": sum(1 for ln in (feature_text or "").splitlines()
                        if ln.strip().startswith("|") and "Condition" not in ln and "ExpectedResult" not in ln)
            + 0}