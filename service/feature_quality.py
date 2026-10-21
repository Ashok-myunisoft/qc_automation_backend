"""Deterministic clean-up of a generated feature file.

The test-case agent writes the feature; this module then fixes the defects that
the QC team found in real runs, in plain code (no AI call), so the result does
not depend on the model following a long prompt perfectly:

  1. "X* Should not be Empty" messages use the field's visible LABEL
     ("Domain Code*"), not its data-cy name ("DomainCode*").
  2. A "field missing" row really leaves THAT field empty and fills the others,
     and the field's step + column exist even when the model left them out.
  3. Duplicate rows / duplicate scenarios are removed.
  4. Delete scenarios come last; Update / Delete use a record that an earlier
     Create row of this file saved; a Create row that saves a key a previous
     row already saved is dropped (it would fail as a duplicate).
  5. Update opens the record with a step that works on picklist screens AND on
     list screens (search box + row icons).
  6. Delete is split into two scenarios: confirm (Yes) and cancel (No), with the
     delete prompt checked before it is answered.

  7. A Create row that leaves a REQUIRED field empty expects the "X* Should not be Empty"
     messages (it never expects "Details Saved Successfully"); validation rows run first.
  8. Empty / duplicate generic scenarios (smoke test, "save with nothing" that expects success,
     update that changes nothing) are removed.
  9. Update / Delete scenarios that sit before the Rule blocks that create their records are
     moved behind them, so the file always runs Create -> Update -> Delete.
 10. Update rows expect the confirmed "Updated" message and the Yes-delete scenario checks the
     confirmed "deleted" message (both read from config/expected_messages.json, never guessed).
 11. Examples tables are re-aligned.

Everything is best-effort: refine_feature() never raises, and any step that
fails leaves the feature exactly as it was before that step.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from service import script_builder

logger = logging.getLogger(__name__)

_SCENARIO_RE = re.compile(r"^\s*(?:Scenario Outline|Scenario Template|Scenario)\s*:", re.IGNORECASE)
_OUTLINE_RE = re.compile(r"^\s*(?:Scenario Outline|Scenario Template)\s*:", re.IGNORECASE)
_BLOCK_OTHER_RE = re.compile(r"^\s*(?:Rule|Background|Feature)\s*:", re.IGNORECASE)
_EXAMPLES_RE = re.compile(r"^\s*(?:Examples|Scenarios)\s*:", re.IGNORECASE)
_TAG_RE = re.compile(r"^\s*@")
_STEP_RE = re.compile(r"^(?P<indent>\s*)(?P<kw>Given|When|Then|And|But|\*)\s+(?P<text>\S.*?)\s*$")
_FIELD_STEP_RE = re.compile(
    r'^I (?P<verb>select|enter|set) "(?P<value>[^"]*)" in the "(?P<field>[^"]*)" '
    r'(?P<kind>picklist|input|combobox|date|radio|checkbox|textarea) field$'
)
_PLACEHOLDER_ONLY_RE = re.compile(r"^<(?P<ph>[^<>]+)>$")
_EMPTY_MSG_RE = re.compile(r"^\s*(?P<name>.+?)\s*\*?\s*should not be empty\s*$", re.IGNORECASE)
_SUCCESS_RE = re.compile(r"success|saved|created|updated", re.IGNORECASE)
_PROMPT_RE = re.compile(r"do you|are you sure|want to|confirm|\?\s*$", re.IGNORECASE)

_SAVE_STEPS = {"I click the save button", "I click the Update button"}
_ADD_STEP = "I click the Add New button"
_DELETE_STEP = "I click the Delete button"


def _n(text) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


# ---------------------------------------------------------------------------
# model
# ---------------------------------------------------------------------------
class Outline:
    def __init__(self) -> None:
        self.tags: list[str] = []
        self.title_line = ""
        self.indent = "  "
        self.steps: list[list[str]] = []      # [indent, keyword, text]
        self.examples_line = ""
        self.table_indent = "      "
        self.header: list[str] = []
        self.rows: list[list[str]] = []
        self.simple = True                    # False -> never touched
        self.modified = False
        self.raw: list[str] = []

    # ---- helpers ----
    def col(self, name: str) -> int:
        for i, h in enumerate(self.header):
            if h.strip().lower() == name.strip().lower():
                return i
        return -1

    @property
    def expected_col(self) -> int:
        return self.col("ExpectedResult")

    def step_texts(self) -> list[str]:
        return [s[2] for s in self.steps]

    def has_step(self, text: str) -> bool:
        return text in self.step_texts()

    @property
    def is_delete(self) -> bool:
        return self.has_step(_DELETE_STEP)

    @property
    def is_create(self) -> bool:
        return self.has_step(_ADD_STEP)

    @property
    def has_save(self) -> bool:
        return any(t in _SAVE_STEPS for t in self.step_texts())

    def field_steps(self):
        """[(step index, verb, value, field, kind)] for every field step."""
        out = []
        for i, (_ind, _kw, text) in enumerate(self.steps):
            m = _FIELD_STEP_RE.match(text)
            if m:
                out.append((i, m.group("verb"), m.group("value"), m.group("field"), m.group("kind")))
        return out

    def field_columns(self) -> dict[str, tuple[str, str]]:
        """{norm(field): (placeholder column, field)} for steps whose value is one placeholder."""
        cols: dict[str, tuple[str, str]] = {}
        for _i, _v, value, field, _k in self.field_steps():
            m = _PLACEHOLDER_ONLY_RE.match(value)
            if m and self.col(m.group("ph")) >= 0:
                cols.setdefault(_n(field), (m.group("ph"), field))
        return cols

    def render(self) -> list[str]:
        if not self.modified:
            return list(self.raw)
        out = list(self.tags)
        out.append(self.title_line)
        for ind, kw, text in self.steps:
            out.append(f"{ind}{kw} {text}")
        out.append("")
        out.append(self.examples_line)
        widths = [0] * len(self.header)
        for row in [self.header] + self.rows:
            for i, cell in enumerate(row):
                if i < len(widths):
                    widths[i] = max(widths[i], len(cell))
        for row in [self.header] + self.rows:
            cells = [(row[i] if i < len(row) else "").ljust(widths[i]) for i in range(len(widths))]
            out.append(self.table_indent + "| " + " | ".join(cells) + " |")
        out.append("")
        return out


def _split_cells(line: str) -> list[str]:
    inner = line.strip()
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|"):
        inner = inner[:-1]
    return [c.strip() for c in re.split(r"(?<!\\)\|", inner)]


def _parse(feature_text: str):
    """-> list of items: ("raw", [lines]) or ("outline", Outline)."""
    lines = feature_text.split("\n")
    items: list = []
    raw: list[str] = []
    i, n = 0, len(lines)

    def flush():
        nonlocal raw
        if raw:
            items.append(("raw", raw))
            raw = []

    def starts_block(idx: int) -> bool:
        if idx >= n:
            return False
        if _SCENARIO_RE.match(lines[idx]) or _BLOCK_OTHER_RE.match(lines[idx]):
            return True
        if _TAG_RE.match(lines[idx]):
            j = idx
            while j < n and _TAG_RE.match(lines[j]):
                j += 1
            return j < n and bool(_SCENARIO_RE.match(lines[j]))
        return False

    while i < n:
        j = i
        while j < n and _TAG_RE.match(lines[j]):
            j += 1
        if j < n and _OUTLINE_RE.match(lines[j]) and (j == i or starts_block(i)):
            flush()
            o = Outline()
            start = i
            o.tags = lines[i:j]
            o.title_line = lines[j]
            o.indent = re.match(r"^\s*", lines[j]).group(0)
            k = j + 1
            while k < n and not starts_block(k):
                k += 1
            body = lines[j + 1:k]
            o.raw = lines[start:k]
            _parse_body(o, body)
            items.append(("outline", o))
            i = k
            continue
        raw.append(lines[i])
        i += 1
        # a plain Scenario (no Examples) or any other block is kept raw
    flush()
    return items


def _parse_body(o: Outline, body: list[str]) -> None:
    seen_examples = False
    table_started = False
    for line in body:
        st = line.strip()
        if not st:
            continue
        if _EXAMPLES_RE.match(line):
            if seen_examples:
                o.simple = False
            seen_examples = True
            o.examples_line = line
            continue
        if st.startswith("|"):
            if not seen_examples:
                o.simple = False
                continue
            cells = _split_cells(line)
            if not table_started:
                o.header = cells
                o.table_indent = re.match(r"^\s*", line).group(0)
                table_started = True
            else:
                o.rows.append(cells)
            continue
        m = _STEP_RE.match(line)
        if m and not seen_examples:
            o.steps.append([m.group("indent"), m.group("kw"), m.group("text")])
            continue
        # comments, doc strings, text we do not understand: leave this outline alone
        o.simple = False
    if not (seen_examples and o.header and o.steps):
        o.simple = False
    if any(len(r) != len(o.header) for r in o.rows):
        o.simple = False
    if any('"' in c for r in o.rows for c in r):
        o.simple = False


def _render(items) -> str:
    out: list[str] = []
    for kind, obj in items:
        out.extend(obj if kind == "raw" else obj.render())
    text = "\n".join(out)
    # no more than one blank line in a row
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


# ---------------------------------------------------------------------------
# analysis labels
# ---------------------------------------------------------------------------
def labels_from_analysis(project_analysis: dict | None) -> dict[str, str]:
    """{base data-cy name: visible label} for every verified field."""
    out: dict[str, str] = {}
    try:
        for page in script_builder._pages(project_analysis or {}):
            fields = []
            for form in page.get("forms") or []:
                if isinstance(form, dict):
                    fields.extend(f for f in form.get("fields") or [] if isinstance(f, dict))
            for loop in page.get("loops") or []:
                if isinstance(loop, dict):
                    fields.extend(f for f in loop.get("fields") or [] if isinstance(f, dict))
            for field in fields:
                data_cy = script_builder._verified_data_cy(field)
                label = str(field.get("label") or "").replace("*", "").strip()
                if not data_cy or not label or label.lower() == "unknown":
                    continue
                base, _kind = script_builder._split_data_cy(data_cy, field)
                out.setdefault(base, label)
    except Exception:
        logger.exception("could not read field labels from the analysis")
    return out


def _exact_required_messages(project_analysis) -> dict[str, str]:
    """Screen spec only (USE_SCREEN_SPEC): the real required-field message of a field whose label has
    no '*' (the frontend shows '<label> Should not be Empty' - the '*' is part of the label text).
    Empty when there is no spec, so nothing changes."""
    out: dict[str, str] = {}
    try:
        spec = (project_analysis or {}).get("screen_spec") or {}
        for label_key, message in (spec.get("messages_by_label") or {}).items():
            out[label_key] = message                  # written in the screen's own code
        for f in spec.get("fields") or []:
            if f.get("active") and f.get("required_message") and not str(f.get("label_text", "")).endswith("*"):
                out.setdefault(_n(f.get("label", "")), f["required_message"])
    except Exception:
        logger.exception("could not read exact required messages from the screen spec")
    return out


class _Fields:
    """Maps a name or label used in a message to the field's base data-cy name."""

    def __init__(self, controls: dict, labels: dict[str, str]):
        self.labels = labels
        self.exact: dict[str, str] = {}
        self.by_norm: dict[str, str] = {}
        for base in (controls or {}):
            self.by_norm[_n(base)] = base
        for base, label in labels.items():
            if base in (controls or {}):
                self.by_norm.setdefault(_n(label), base)

    def field_of(self, name: str) -> str | None:
        return self.by_norm.get(_n(name))

    def label_of(self, base: str) -> str | None:
        return self.labels.get(base)

    def required_message(self, label: str) -> str:
        return self.exact.get(_n(label)) or f"{label}* Should not be Empty"


# ---------------------------------------------------------------------------
# step 1 - message labels
# ---------------------------------------------------------------------------
def _split_parts(expected: str) -> list[str]:
    return [p.strip() for p in re.split(r"\s*[,;]\s*", expected or "") if p.strip()]


def _fix_labels(outlines: list[Outline], fields: _Fields, report: dict) -> None:
    changed = 0
    for o in outlines:
        if not o.simple or o.expected_col < 0:
            continue
        ec = o.expected_col
        for row in o.rows:
            parts = _split_parts(row[ec])
            if not parts:
                continue
            new_parts, any_change = [], False
            for p in parts:
                m = _EMPTY_MSG_RE.match(p)
                base = fields.field_of(m.group("name")) if m else None
                label = fields.label_of(base) if base else None
                if m and label:
                    fixed = fields.required_message(label)
                    if fixed != p:
                        any_change = True
                    new_parts.append(fixed)
                else:
                    new_parts.append(p)
            if any_change:
                row[ec] = ", ".join(new_parts) if len(new_parts) > 1 else new_parts[0]
                o.modified = True
                changed += 1
    if changed:
        report["notes"].append(f"{changed} validation message(s) now use the field label shown on the screen")


# ---------------------------------------------------------------------------
# step 2 - "missing field" rows
# ---------------------------------------------------------------------------
def _row_missing(o: Outline, row: list[str], fields: _Fields):
    """Set of base field names a validation row says are empty, or None."""
    ec = o.expected_col
    if ec < 0:
        return None
    parts = _split_parts(row[ec])
    if not parts:
        return None
    missing = set()
    for p in parts:
        m = _EMPTY_MSG_RE.match(p)
        base = fields.field_of(m.group("name")) if m else None
        if not base:
            return None
        missing.add(base)
    return missing


def _donor_values(outlines: list[Outline]) -> dict[str, str]:
    """First real value used for each field in a row that was expected to save."""
    donor: dict[str, str] = {}
    for o in outlines:
        if not o.simple or o.expected_col < 0:
            continue
        cols = o.field_columns()
        for row in o.rows:
            if not _SUCCESS_RE.search(row[o.expected_col] or ""):
                continue
            for _key, (ph, field) in cols.items():
                v = row[o.col(ph)].strip()
                if v:
                    donor.setdefault(field, v)
    return donor


def _fix_missing_rows(outlines: list[Outline], fields: _Fields, controls: dict, report: dict) -> None:
    donor = _donor_values(outlines)
    donor_n = {_n(k): v for k, v in donor.items()}
    fixed_rows = added_steps = 0
    for o in outlines:
        if not o.simple or o.is_delete or o.expected_col < 0 or not o.has_save:
            continue
        per_row = [_row_missing(o, r, fields) for r in o.rows]
        if not any(per_row):
            continue
        mandatory = set().union(*[m for m in per_row if m])          # fields this outline proves mandatory
        cols = o.field_columns()

        # a mandatory field that has no step / column yet: add them (only when a valid value is known)
        save_idx = next((i for i, t in enumerate(o.step_texts()) if t in _SAVE_STEPS), None)
        for base in sorted(mandatory):
            if _n(base) in cols or save_idx is None:
                continue
            info = controls.get(base) or {}
            kind = info.get("kind")
            if kind not in script_builder._KIND_SENTENCE or _n(base) not in donor_n:
                continue
            # the new column goes right before ExpectedResult, the new step right before Save
            ec = o.expected_col
            o.header.insert(ec, base)
            for r in o.rows:
                r.insert(ec, "")
            sentence = script_builder._KIND_SENTENCE[kind][1].format(v=f"<{base}>", f=base)
            indent = o.steps[save_idx][0]
            kw = "And" if o.steps[save_idx][1] in ("And", "But", "*") or save_idx > 0 else "When"
            o.steps.insert(save_idx, [indent, kw, sentence])
            save_idx += 1
            cols = o.field_columns()
            o.modified = True
            added_steps += 1

        ec = o.expected_col
        for row in o.rows:
            missing = _row_missing(o, row, fields)
            if missing is None:
                continue
            for key, (ph, field) in cols.items():
                ci = o.col(ph)
                base = fields.field_of(field) or field
                if base in missing:
                    if row[ci] != "":
                        row[ci] = ""
                        o.modified = True
                        fixed_rows += 1
                elif base in mandatory and row[ci].strip() == "" and _n(base) in donor_n:
                    row[ci] = donor_n[_n(base)]
                    o.modified = True
                    fixed_rows += 1
    if fixed_rows or added_steps:
        report["notes"].append(
            f"validation rows corrected: {fixed_rows} cell(s) set / filled, {added_steps} missing field step(s) added")


# ---------------------------------------------------------------------------
# step 3 - duplicates
# ---------------------------------------------------------------------------
def _signature(o: Outline, row: list[str]) -> tuple:
    cols = o.field_columns()
    values = tuple(sorted((_n(f), row[o.col(ph)].strip().lower()) for _k, (ph, f) in cols.items()))
    fixed = tuple(t for t in o.step_texts() if not _FIELD_STEP_RE.match(t))
    expected = row[o.expected_col].strip().lower() if o.expected_col >= 0 else ""
    return (fixed, values, expected)


def _dedupe(items, report: dict) -> None:
    seen: set = set()
    dropped_rows = dropped_outlines = 0
    survivors = []
    for kind, obj in items:
        if kind != "outline" or not obj.simple:
            survivors.append((kind, obj))
            continue
        o = obj
        keep = []
        for row in o.rows:
            sig = _signature(o, row)
            if sig in seen:
                dropped_rows += 1
                continue
            seen.add(sig)
            keep.append(row)
        if len(keep) != len(o.rows):
            o.rows = keep
            o.modified = True
        if not o.rows:
            dropped_outlines += 1
            continue
        survivors.append((kind, o))
    items[:] = survivors
    if dropped_rows:
        report["notes"].append(
            f"removed {dropped_rows} duplicate row(s) and {dropped_outlines} scenario(s) that tested the same thing twice")


# ---------------------------------------------------------------------------
# step 4 - order and records
# ---------------------------------------------------------------------------
def _is_rule(item) -> bool:
    return item[0] == "raw" and any(_BLOCK_OTHER_RE.match(l) and l.strip().lower().startswith("rule") for l in item[1])


def _delete_last(items, report: dict) -> None:
    """Inside every section (the part before the first Rule, and each Rule), move delete
    scenarios behind the others, keeping the relative order."""
    moved = 0
    bounds = [i for i, it in enumerate(items) if _is_rule(it)] + [len(items)]
    sections = [(0, bounds[0])] + [(bounds[k], bounds[k + 1]) for k in range(len(bounds) - 1)]
    for a, b in sections:
        slots = [i for i in range(a, b) if items[i][0] == "outline"]
        outs = [items[i][1] for i in slots]
        ordered = [o for o in outs if not (o.simple and o.is_delete)] + [o for o in outs if o.simple and o.is_delete]
        if [id(o) for o in ordered] != [id(o) for o in outs]:
            moved += 1
            for slot, o in zip(slots, ordered):
                items[slot] = ("outline", o)
    if moved:
        report["notes"].append("delete scenarios moved after the create / update scenarios")


def _key_info(o: Outline):
    """(column index, field) of the lookup field: the first field step, when its value is a placeholder."""
    fs = o.field_steps()
    if not fs:
        return None
    _i, _verb, value, field, _kind = fs[0]
    m = _PLACEHOLDER_ONLY_RE.match(value)
    if not m or o.col(m.group("ph")) < 0:
        return None
    return o.col(m.group("ph")), field


def _already_rewritten(o: Outline) -> bool:
    return any(t.startswith("I open the existing record") or t.startswith("I request to delete the record")
               for t in o.step_texts())


def _is_update(o: Outline) -> bool:
    return (o.simple and not o.is_create and not o.is_delete and o.has_save
            and not _already_rewritten(o) and _key_info(o) is not None)


def _records(items, report: dict) -> None:
    outlines = [obj for kind, obj in items if kind == "outline" and obj.simple]
    # key fields = the lookup field of every update / delete scenario
    # (a delete scenario can have no placeholder lookup field: _key_info is None -> not a keyed scenario)
    key_fields = {_n(ki[1]) for ki in (_key_info(o) for o in outlines if _is_update(o) or o.is_delete)
                  if ki is not None}
    if not key_fields:
        return

    created: dict[str, list[str]] = {}       # norm(field) -> values in the order they were saved
    deleted: dict[str, set[str]] = {}
    dropped = remapped = 0

    def available(fkey: str) -> list[str]:
        return [v for v in created.get(fkey, []) if v.lower() not in deleted.get(fkey, set())]

    new_items = []
    for kind, obj in items:
        if kind != "outline" or not obj.simple:
            new_items.append((kind, obj))
            continue
        o = obj
        ec = o.expected_col
        if o.is_create and ec >= 0:
            cols = o.field_columns()
            keep = []
            for row in o.rows:
                ok = _SUCCESS_RE.search(row[ec] or "") is not None and "should not be empty" not in row[ec].lower()
                if ok:
                    dup = False
                    for fk in key_fields:
                        if fk in cols:
                            v = row[o.col(cols[fk][0])].strip()
                            if v and v.lower() in {x.lower() for x in created.get(fk, [])}:
                                dup = True
                    if dup:
                        dropped += 1
                        continue
                    for fk in key_fields:
                        if fk in cols:
                            v = row[o.col(cols[fk][0])].strip()
                            if v:
                                created.setdefault(fk, []).append(v)
                keep.append(row)
            if len(keep) != len(o.rows):
                o.rows = keep
                o.modified = True
            if o.rows:
                new_items.append((kind, o))
            continue

        if (_is_update(o) or o.is_delete) and created and _key_info(o) is not None:
            ci, field = _key_info(o)
            fk = _n(field)
            if fk not in created:
                new_items.append((kind, o))
                continue
            keep = []
            for row in o.rows:
                v = row[ci].strip()
                avail = available(fk)
                if v.lower() not in {a.lower() for a in avail}:
                    if not avail:
                        dropped += 1
                        continue
                    row[ci] = avail[0]
                    o.modified = True
                    remapped += 1
                    v = avail[0]
                if o.is_delete and ec >= 0 and not _PROMPT_RE.search(row[ec] or "") and _SUCCESS_RE.search(row[ec] or ""):
                    deleted.setdefault(fk, set()).add(v.lower())
                elif o.is_delete and ec >= 0 and _PROMPT_RE.search(row[ec] or ""):
                    # the delete flow is split later; its confirm scenario deletes this record
                    deleted.setdefault(fk, set()).add(v.lower())
                keep.append(row)
            if len(keep) != len(o.rows):
                o.rows = keep
                o.modified = True
            if o.rows:
                new_items.append((kind, o))
            continue
        new_items.append((kind, o))
    items[:] = new_items
    if dropped or remapped:
        report["notes"].append(
            f"record flow checked: {remapped} update / delete row(s) pointed at a record this file saved, "
            f"{dropped} row(s) removed (duplicate create, or no saved record to use)")


# ---------------------------------------------------------------------------
# step 5 + 6 - update opens the record, delete is split in confirm / cancel
# ---------------------------------------------------------------------------
def _rewrite_update(o: Outline, controls: dict, report: dict) -> bool:
    ki = _key_info(o)
    if not ki:
        return False
    _ci, field = ki
    info = controls.get(field) or {}
    first = o.field_steps()[0]
    if info.get("kind") != "picklist" or first[4] != "picklist":
        return False
    idx = first[0]
    ph = _PLACEHOLDER_ONLY_RE.match(first[2]).group("ph")
    o.steps[idx][2] = f'I open the existing record "<{ph}>" using the "{field}" field'
    o.modified = True
    return True


def _title_base(o: Outline) -> str:
    t = re.sub(r"^\s*(?:Scenario Outline|Scenario Template)\s*:\s*", "", o.title_line)
    t = re.sub(r'\s*-?\s*"?<Condition>"?\s*$', "", t).strip()
    return t.rstrip(" -")


def _split_delete(o: Outline, controls: dict) -> list[Outline] | None:
    ki = _key_info(o)
    if not ki or o.expected_col < 0:
        return None
    ci, field = ki
    info = controls.get(field) or {}
    first = o.field_steps()[0]
    if info.get("kind") != "picklist" or first[4] != "picklist":
        return None
    key_ph = _PLACEHOLDER_ONLY_RE.match(first[2]).group("ph")
    ec = o.expected_col
    cond_c = o.col("Condition")

    prompt = success = ""
    for row in o.rows:
        e = row[ec].strip()
        if not e:
            continue
        if _PROMPT_RE.search(e) and not prompt:
            prompt = e
        elif not _PROMPT_RE.search(e) and not success:
            success = e
    confirmed_delete = _pick_message(("delet",))
    if confirmed_delete:
        success = confirmed_delete        # the message the QC team confirmed beats the model's guess
    base = _title_base(o)
    indent, ti = o.indent, o.table_indent
    step_indent = o.steps[0][0] if o.steps else indent + "  "

    def make(kind: str) -> Outline:
        n = Outline()
        n.simple, n.modified = True, True
        n.indent, n.table_indent = indent, ti
        n.tags = list(o.tags)
        n.title_line = f'{indent}Scenario Outline: {base} - {kind} - "<Condition>"'
        n.examples_line = f"{indent}  Examples:" if not o.examples_line else o.examples_line
        return n

    # navigation-free steps: the background already navigated
    def steps_for(confirm: bool) -> list[list[str]]:
        st = [[step_indent, "When", f'I request to delete the record "<{key_ph}>" using the "{field}" field']]
        if prompt:
            st.append([step_indent, "Then", 'I should see the delete prompt "<PromptMessage>"'])
        if confirm:
            st.append([step_indent, "When", "I confirm the delete"])
            if success:
                # checking the message also closes its pop-up, so the next step is never blocked
                st.append([step_indent, "Then", 'I should see a confirmation message "<ExpectedResult>"'])
            st.append([step_indent, "Then" if not success else "And",
                       f'I should no longer see the record "<{key_ph}>" using the "{field}" field'])
        else:
            st.append([step_indent, "When", "I cancel the delete"])
            st.append([step_indent, "Then", f'I should still see the record "<{key_ph}>" using the "{field}" field'])
        return st

    rows_keys: list[tuple[str, str]] = []
    for row in o.rows:
        key = row[ci].strip()
        cond = row[cond_c].strip() if cond_c >= 0 else "delete"
        if key:
            rows_keys.append((cond, key))
    if not rows_keys:
        return None

    cancel = make("cancel (No)")
    cancel.steps = steps_for(False)
    cancel.header = ["Condition", key_ph] + (["PromptMessage"] if prompt else [])
    cancel.rows = [[f"{rows_keys[0][0]} - answer No, record kept", rows_keys[0][1]] + ([prompt] if prompt else [])]

    confirm = make("confirm (Yes)")
    confirm.steps = steps_for(True)
    confirm.header = ["Condition", key_ph] + (["PromptMessage"] if prompt else []) + (["ExpectedResult"] if success else [])
    confirm.rows = [[f"{c} - answer Yes, record deleted", k] + ([prompt] if prompt else []) + ([success] if success else [])
                    for c, k in rows_keys]
    return [cancel, confirm]


def _rewrite_records(items, controls: dict, report: dict) -> None:
    upd = dele = 0
    new_items = []
    for kind, obj in items:
        if kind == "outline" and obj.simple:
            if _is_update(obj):
                if _rewrite_update(obj, controls, report):
                    upd += 1
                    _use_update_message(obj)
            elif obj.is_delete:
                pair = _split_delete(obj, controls)
                if pair:
                    dele += 1
                    for k, p in enumerate(pair):
                        if k:
                            new_items.append(("raw", [""]))
                        new_items.append(("outline", p))
                    continue
        new_items.append((kind, obj))
    items[:] = new_items
    if upd:
        report["notes"].append(f"{upd} update scenario(s) now open the record by search / picklist")
    if dele:
        report["notes"].append(f"{dele} delete scenario(s) split into confirm (Yes) and cancel (No)")


# ---------------------------------------------------------------------------
# confirmed messages (config/expected_messages.json, key "all")
# ---------------------------------------------------------------------------
_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "expected_messages.json"
_VALIDATION_RE = re.compile(r"should not be empty", re.IGNORECASE)


def _confirmed_messages() -> list[str]:
    """Messages the QC team confirmed on the real application (raw text)."""
    try:
        data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        return [m.strip() for m in (data.get("all") or []) if isinstance(m, str) and m.strip()]
    except Exception:
        return []


def _pick_message(words: tuple[str, ...]) -> str:
    for m in _confirmed_messages():
        low = m.lower()
        if "success" in low and all(w in low for w in words):
            return m
    return ""


# ---------------------------------------------------------------------------
# step 2b - required fields left empty on a Create row
# ---------------------------------------------------------------------------
def required_from_analysis(project_analysis: dict | None) -> set[str]:
    """Base names of fields the analysis marks required (required flag, or a * on the label)."""
    out: set[str] = set()
    try:
        for page in script_builder._pages(project_analysis or {}):
            fields = []
            for form in page.get("forms") or []:
                if isinstance(form, dict):
                    fields.extend(f for f in form.get("fields") or [] if isinstance(f, dict))
            for field in fields:
                data_cy = script_builder._verified_data_cy(field)
                if not data_cy:
                    continue
                if field.get("required") is True or "*" in str(field.get("label") or ""):
                    base, _kind = script_builder._split_data_cy(data_cy, field)
                    out.add(base)
    except Exception:
        logger.exception("could not read required fields from the analysis")
    return out


_EMPTYISH_COND_RE = re.compile(r"empty|missing|without|blank|no value|omit", re.IGNORECASE)


def _mandatory_fields(outlines: list[Outline], fields: _Fields, required: set[str]) -> set[str]:
    mandatory = {b for b in required if b in fields.labels or fields.by_norm.get(_n(b)) == b}
    for o in outlines:
        if not o.simple or o.expected_col < 0:
            continue
        for row in o.rows:
            m = _row_missing(o, row, fields)
            if m:
                mandatory |= m
    return {b for b in mandatory if fields.label_of(b)}


def _is_validation_row(o: Outline, row: list[str]) -> bool:
    return bool(o.expected_col >= 0 and _VALIDATION_RE.search(row[o.expected_col] or ""))


def _fix_required_empty(outlines: list[Outline], fields: _Fields, required: set[str], report: dict) -> None:
    mandatory = _mandatory_fields(outlines, fields, required)
    if not mandatory:
        return
    fixed = 0
    for o in outlines:
        if not o.simple or not o.is_create or not o.has_save or o.is_delete or o.expected_col < 0:
            continue
        ec = o.expected_col
        cols = o.field_columns()
        cond_c = o.col("Condition")
        bare = not o.field_steps()
        for row in o.rows:
            if _is_validation_row(o, row) or not _SUCCESS_RE.search(row[ec] or ""):
                continue
            wording_empty = cond_c >= 0 and bool(_EMPTYISH_COND_RE.search(row[cond_c] or ""))
            empty = []
            for base in sorted(mandatory):
                key = _n(base)
                if key in cols:
                    if row[o.col(cols[key][0])].strip() == "":
                        empty.append(base)
                elif bare or wording_empty:
                    empty.append(base)       # no step fills it: it stays empty
            if not empty:
                continue
            row[ec] = ", ".join(fields.required_message(fields.label_of(b)) for b in empty)
            if cond_c >= 0 and not wording_empty:
                names = " and ".join(fields.label_of(b) for b in empty)
                row[cond_c] = f"Save with {names} empty"
            o.modified = True
            fixed += 1
    if fixed:
        report["notes"].append(f"{fixed} row(s) that leave a required field empty now expect the validation message(s)")


def _all_required_empty_row_exists(outlines: list[Outline], fields: _Fields, mandatory: set[str]) -> bool:
    for o in outlines:
        if not o.simple or not o.is_create or not o.field_steps():
            continue
        cols = o.field_columns()
        for row in o.rows:
            if not _is_validation_row(o, row):
                continue
            if all(_n(b) in cols and row[o.col(cols[_n(b)][0])].strip() == "" for b in mandatory):
                return True
    return False


# ---------------------------------------------------------------------------
# step 3b - generic scenarios that test nothing
# ---------------------------------------------------------------------------
_CONFIRM_PREFIX = "I should see a confirmation message"


def _drop_empty_scenarios(items, fields: _Fields, required: set[str], report: dict) -> None:
    outlines = [obj for kind, obj in items if kind == "outline" and obj.simple]
    mandatory = _mandatory_fields(outlines, fields, required)
    full_update_exists = any(
        _is_update(o) and len(o.field_steps()) > 1 for o in outlines)
    covered = bool(mandatory) and _all_required_empty_row_exists(outlines, fields, mandatory)
    dropped = 0
    survivors = []
    for kind, obj in items:
        if kind != "outline" or not obj.simple:
            survivors.append((kind, obj))
            continue
        o = obj
        texts = o.step_texts()
        ec = o.expected_col
        drop = False
        # smoke / "nothing happens" scenario: only a confirmation step, empty expected result
        if texts and all(t.startswith(_CONFIRM_PREFIX) for t in texts) and ec >= 0 \
                and all(not r[ec].strip() for r in o.rows):
            drop = True
        # "Add + Save with nothing filled": its all-empty case is already a row elsewhere
        elif o.is_create and o.has_save and not o.field_steps() and covered \
                and ec >= 0 and all(_is_validation_row(o, r) for r in o.rows):
            drop = True
        # an update that changes nothing, while a real update scenario exists
        elif _is_update(o) and len(o.field_steps()) == 1 and full_update_exists:
            drop = True
        if drop:
            dropped += 1
            continue
        survivors.append((kind, o))
    items[:] = survivors
    if dropped:
        report["notes"].append(f"{dropped} empty / repeated generic scenario(s) removed")


def _validation_rows_first(outlines: list[Outline], report: dict) -> None:
    """Inside a Create table the rows that expect a validation error go first: nothing is saved
    yet, so a picklist value cannot load an existing record and turn the row into an update."""
    moved = 0
    for o in outlines:
        if not o.simple or not o.is_create or o.expected_col < 0:
            continue
        first = [r for r in o.rows if _is_validation_row(o, r)]
        rest = [r for r in o.rows if not _is_validation_row(o, r)]
        if first and rest and (first + rest) != o.rows:
            o.rows = first + rest
            o.modified = True
            moved += 1
    if moved:
        report["notes"].append("validation rows placed before the rows that save a record")


# ---------------------------------------------------------------------------
# step 4b - record scenarios before the creating Rule blocks
# ---------------------------------------------------------------------------
def _move_record_scenarios(items, report: dict) -> None:
    rule_idx = [i for i, it in enumerate(items) if _is_rule(it)]
    if not rule_idx:
        return
    first_rule = rule_idx[0]
    def saves_record(o: Outline) -> bool:
        return o.is_create and o.expected_col >= 0 and any(
            _SUCCESS_RE.search(r[o.expected_col] or "") and not _is_validation_row(o, r) for r in o.rows)

    pre_has_create = any(k == "outline" and o.simple and saves_record(o) for k, o in items[:first_rule])
    later_has_create = any(k == "outline" and o.simple and saves_record(o) for k, o in items[first_rule:])
    if pre_has_create or not later_has_create:
        return
    moving = [(k, o) for k, o in items[:first_rule]
              if k == "outline" and o.simple and (_is_update(o) or o.is_delete)]
    if not moving:
        return
    ids = {id(o) for _k, o in moving}
    kept = [(k, o) for k, o in items if not (k == "outline" and id(o) in ids)]
    kept.append(("raw", [""]))
    for k, o in moving:
        kept.append((k, o))
        kept.append(("raw", [""]))
    items[:] = kept
    report["notes"].append(f"{len(moving)} update / delete scenario(s) moved behind the scenarios that create their records")


# ---------------------------------------------------------------------------
# step 7 - confirmed update message + table alignment
# ---------------------------------------------------------------------------
def _use_update_message(o: Outline) -> int:
    msg = _pick_message(("updated",))
    if not msg or o.expected_col < 0:
        return 0
    n = 0
    for row in o.rows:
        e = row[o.expected_col]
        if re.search(r"saved successfully", e or "", re.IGNORECASE) and e.strip() != msg:
            row[o.expected_col] = msg
            n += 1
    if n:
        o.modified = True
    return n


def _merge_split_deletes(items, report: dict) -> None:
    """One cancel (No) and one confirm (Yes) scenario per file: delete scenarios that came from
    several places (top level and Rule blocks) are merged into the first one's table."""
    first_of: dict = {}
    drop: set[int] = set()
    merged = 0
    for idx, (kind, o) in enumerate(items):
        if kind != "outline" or not o.simple or len(o.header) < 2:
            continue
        if not any(t.startswith("I request to delete the record") for t in o.step_texts()):
            continue
        sig = (tuple(o.step_texts()), tuple(h.strip().lower() for h in o.header))
        if sig not in first_of:
            first_of[sig] = idx
            continue
        keeper = items[first_of[sig]][1]
        have = {r[1].strip().lower() for r in keeper.rows}
        for r in o.rows:
            if r[1].strip().lower() not in have and r[1].strip():
                keeper.rows.append(r)
                have.add(r[1].strip().lower())
        keeper.modified = True
        drop.add(idx)
        merged += 1
    if drop:
        items[:] = [it for i, it in enumerate(items) if i not in drop]
        report["notes"].append(f"{merged} repeated delete scenario(s) merged into one confirm (Yes) / one cancel (No)")


def _table_aligned(o: Outline) -> bool:
    bars = None
    for line in o.raw:
        if line.strip().startswith("|"):
            pos = [i for i, ch in enumerate(line) if ch == "|"]
            if bars is None:
                bars = pos
            elif pos != bars:
                return False
    return True


def _align_tables(items, report: dict) -> None:
    fixed = 0
    for kind, obj in items:
        if kind == "outline" and obj.simple and not obj.modified and not _table_aligned(obj):
            obj.modified = True
            fixed += 1
    if fixed:
        report["notes"].append(f"{fixed} Examples table(s) re-aligned")


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
def refine_feature(feature_text: str, project_analysis: dict | None, controls: dict | None):
    """Returns (feature_text, report). Never raises; on any problem the original text is returned."""
    report = {"notes": [], "changed": False}
    if not feature_text or "Scenario Outline" not in feature_text:
        return feature_text, report
    controls = controls or {}
    crlf = "\r\n" in feature_text
    original_text = feature_text
    if crlf:
        feature_text = feature_text.replace("\r\n", "\n")
    try:
        items = _parse(feature_text)
        fields = _Fields(controls, labels_from_analysis(project_analysis))
        fields.exact = _exact_required_messages(project_analysis)

        def outlines():
            return [obj for kind, obj in items if kind == "outline" and obj.simple]

        required = required_from_analysis(project_analysis)

        for name, fn in (
            ("labels", lambda: _fix_labels(outlines(), fields, report)),
            ("required empty", lambda: _fix_required_empty(outlines(), fields, required, report)),
            ("empty scenarios", lambda: _drop_empty_scenarios(items, fields, required, report)),
            ("missing rows", lambda: _fix_missing_rows(outlines(), fields, controls, report)),
            ("duplicates", lambda: _dedupe(items, report)),
            ("validation first", lambda: _validation_rows_first(outlines(), report)),
            ("move records", lambda: _move_record_scenarios(items, report)),
            ("order", lambda: _delete_last(items, report)),
            ("records", lambda: _records(items, report)),
            ("update / delete", lambda: _rewrite_records(items, controls, report)),
            ("merge deletes", lambda: _merge_split_deletes(items, report)),
            ("alignment", lambda: _align_tables(items, report)),
        ):
            try:
                fn()
            except Exception:
                logger.exception("feature refinement step '%s' failed - skipped", name)

        text = _render(items)
        if feature_text.endswith("\n") and not text.endswith("\n"):
            text += "\n"
        if "Feature:" not in text or text.count("Scenario Outline") < 1:
            logger.warning("feature refinement produced an unusable result - keeping the original")
            return original_text, {"notes": [], "changed": False}
        if crlf:
            text = text.replace("\n", "\r\n")
        report["changed"] = text != original_text
        return text, report
    except Exception:
        logger.exception("feature refinement failed - keeping the feature as generated")
        return original_text, {"notes": [], "changed": False}