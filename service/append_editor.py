"""Deterministic editor for an existing feature file + script (the 'append' path, optimised).

Most edits never call the model:
  * TEST_DATA  -> the matching Examples cell(s) are changed in place and the table is re-aligned
  * COMMENT    -> a '#' line in the feature and a '//' line in the script, inserted by code
  * FULL_SCENARIO -> the pasted Gherkin is converted to a Scenario Outline + Examples table, checked and inserted by code
  * DESCRIBED_SCENARIO -> the model returns ONLY the new scenario block (describe_fn); code merges it in

The script changes only when a new step sentence really needs a definition (service/extended_steps.py path).
Every edit is verified: existing lines unchanged (apart from re-padding / the edited cells), the step checker does
not get worse, and the requested value / scenario is really there. A failed item leaves the files as they were and
says exactly why.
"""
from __future__ import annotations

import asyncio
import difflib
import logging
import re
from dataclasses import dataclass, field

from service import extended_steps, request_intent as ri, script_builder, step_checker, template_steps

logger = logging.getLogger(__name__)

MAX_ROWS = 25
_NEG_RE = re.compile(r"\b(?:invalid|negative|duplicate|blank|empty|missing|mandatory|required|exceed\w*|maximum|minimum|boundary|"
                     r"special|error|without|wrong|existing|zero|null)\b", re.I)
_STOP = {"scenario", "scenarios", "test", "tests", "case", "cases", "with", "that", "this", "please", "should", "where",
         "when", "have", "add", "adding", "check", "verify", "validate", "validation", "include", "create", "make",
         "want", "need", "also", "for", "the", "and", "into", "from", "show", "shows", "see", "ensure", "cover"}


def _n(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


# ---------------------------------------------------------------------------
# table cells (raw text is kept, so untouched cells are never altered)
# ---------------------------------------------------------------------------
def _raw_cells(line: str) -> list[str]:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|") and not body.endswith("\\|"):
        body = body[:-1]
    cells, cur, i = [], [], 0
    while i < len(body):
        c = body[i]
        if c == "\\" and i + 1 < len(body):
            cur.append(c + body[i + 1])
            i += 2
            continue
        if c == "|":
            cells.append("".join(cur).strip())
            cur = []
        else:
            cur.append(c)
        i += 1
    cells.append("".join(cur).strip())
    return cells


def _unesc(raw: str) -> str:
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "|": "|", "\\": "\\"}.get(m.group(1), m.group(0)), raw)


def _esc(value: str) -> str:
    return (value or "").replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def render_table(rows: list[list[str]], indent: str) -> list[str]:
    """Aligned table lines: one space either side of every cell, '|' characters lined up."""
    if not rows:
        return []
    cols = max(len(r) for r in rows)
    rows = [r + [""] * (cols - len(r)) for r in rows]
    widths = [max(len(r[c]) for r in rows) for c in range(cols)]
    return [indent + "| " + " | ".join(r[c].ljust(widths[c]) for c in range(cols)) + " |" for r in rows]


# ---------------------------------------------------------------------------
# a small structural parser (line indexes, which step_checker.parse_feature does not keep)
# ---------------------------------------------------------------------------
_SC_RE = re.compile(r"^(?P<ind>\s*)(?P<kw>Scenario\s+Outline|Scenario\s+Template|Scenario|Example)\s*:\s*(?P<title>.*)$", re.I)
_RULE_RE = re.compile(r"^\s*Rule\s*:\s*(?P<name>.*)$", re.I)
_STEP_RE = re.compile(r"^(?P<ind>\s*)(?P<kw>Given|When|Then|And|But)\s+(?P<text>\S.*)$", re.I)
_EX_RE = re.compile(r"^\s*(?:Examples|Scenarios)\s*:", re.I)

_FIELD_RES = (
    re.compile(r'in the "(?P<f>[^"<>]+)" (?:picklist|input|combobox|date|radio|checkbox|textarea|new record) field'),
    re.compile(r'the "(?P<f>[^"<>]+)" field should show'),
    re.compile(r'I clear the "(?P<f>[^"<>]+)" field'),
    re.compile(r'using the "(?P<f>[^"<>]+)" field'),
)
_PH_FIELD_RES = (
    re.compile(r'"<(?P<ph>[^<>]+)>"\s+in the\s+"(?P<f>[^"]+)"'),
    re.compile(r'the "(?P<f>[^"]+)" field should show "<(?P<ph>[^<>]+)>"'),
    re.compile(r'"<(?P<ph>[^<>]+)>"\s+.*?using the "(?P<f>[^"]+)" field'),
)


@dataclass
class Table:
    examples_idx: int
    header_idx: int
    row_idxs: list[int]
    indent: str


@dataclass
class Block:
    start: int            # first line (tags included)
    title_idx: int
    end: int              # exclusive; trailing blanks / comments excluded
    title: str
    is_outline: bool
    rule: str | None
    indent: str
    steps: list[tuple[int, str, str, str]] = field(default_factory=list)   # (idx, indent, keyword, text)
    tables: list[Table] = field(default_factory=list)


def parse_blocks(lines: list[str]) -> list[Block]:
    titles = [i for i, l in enumerate(lines) if _SC_RE.match(l)]
    starts = []
    for ti in titles:
        s = ti
        while s > 0 and lines[s - 1].strip().startswith("@"):
            s -= 1
        starts.append(s)
    rule_idx = [(i, _RULE_RE.match(l).group("name").strip()) for i, l in enumerate(lines) if _RULE_RE.match(l)]
    blocks: list[Block] = []
    for k, ti in enumerate(titles):
        nxt = starts[k + 1] if k + 1 < len(titles) else len(lines)
        for ri_, _ in rule_idx:
            if ti < ri_ < nxt:
                nxt = ri_
                break
        end = nxt
        while end > starts[k] and (not lines[end - 1].strip() or lines[end - 1].strip().startswith("#")):
            end -= 1
        m = _SC_RE.match(lines[ti])
        rule = None
        for ri_, name in rule_idx:
            if ri_ < ti:
                rule = name
        b = Block(starts[k], ti, end, m.group("title").strip(), m.group("kw").lower() != "scenario" and m.group("kw").lower() != "example",
                  rule, m.group("ind"))
        i = ti + 1
        while i < end:
            sm = _STEP_RE.match(lines[i])
            if sm:
                b.steps.append((i, sm.group("ind"), sm.group("kw"), sm.group("text").strip()))
            elif _EX_RE.match(lines[i]):
                j = i + 1
                while j < end and (not lines[j].strip() or lines[j].strip().startswith(("#", "@"))):
                    j += 1
                if j < end and lines[j].strip().startswith("|"):
                    rows = []
                    h = j
                    j += 1
                    while j < end and lines[j].strip().startswith("|"):
                        rows.append(j)
                        j += 1
                    b.tables.append(Table(i, h, rows, re.match(r"\s*", lines[h]).group(0)))
                    i = j - 1
            i += 1
        blocks.append(b)
    return blocks


def _fields_of(block: Block) -> set[str]:
    out = set()
    for _, _, _, text in block.steps:
        for rx in _FIELD_RES:
            for m in rx.finditer(text):
                out.add(m.group("f"))
    return out


def known_fields(feature: str, script: str | None) -> set[str]:
    names: set[str] = set()
    for b in parse_blocks((feature or "").split("\n")):
        names |= _fields_of(b)
    try:
        names |= set((script_builder.controls_from_script(script) or {}).keys())
    except Exception:  # noqa: BLE001
        pass
    return names


def _col_fields(block: Block) -> dict[str, str]:
    """placeholder (column name) -> field it fills, read from the steps."""
    out: dict[str, str] = {}
    for _, _, _, text in block.steps:
        for rx in _PH_FIELD_RES:
            for m in rx.finditer(text):
                out.setdefault(m.group("ph"), m.group("f"))
    return out


def _cond_idx(header: list[str]) -> int:
    for i, h in enumerate(header):
        if _n(_unesc(h)) == "condition":
            return i
    return 0


# ---------------------------------------------------------------------------
# edit: change values in Examples cells
# ---------------------------------------------------------------------------
def set_values(feature: str, fld: str, new_value: str, old_value: str = "", target: str = "") -> tuple[str, list[dict], str]:
    """Returns (new_feature, changes, reason_if_nothing_changed)."""
    lines = feature.split("\n")
    blocks = parse_blocks(lines)
    nf, tgt = _n(fld), _n(target)
    new_value = (new_value or "").replace('"', "")
    by_title = [b for b in blocks if tgt and tgt in _n(b.title)]
    cand = by_title or blocks

    def columns(strict: bool) -> list[tuple[Block, Table, int]]:
        found = []
        for b in cand:
            cf = _col_fields(b)
            for t in b.tables:
                header = _raw_cells(lines[t.header_idx])
                for ci, h in enumerate(header):
                    hn, fn = _n(_unesc(h)), _n(cf.get(_unesc(h), ""))
                    if hn == nf or (fn and fn == nf):
                        found.append((b, t, ci))
                    elif not strict and len(nf) >= 4 and ((nf in hn) or (hn and len(hn) >= 4 and hn in nf) or (fn and (nf in fn or fn in nf))):
                        found.append((b, t, ci))
        return found

    cols = columns(True) or columns(False)
    if not cols:
        return feature, [], f'no Examples column or step field named "{fld}" was found' + (f' in a scenario about "{target}"' if target else "")

    changes: list[dict] = []
    touched: dict[int, tuple[Table, list[list[str]]]] = {}
    for b, t, ci in cols:
        header = _raw_cells(lines[t.header_idx])
        ctab = touched.setdefault(t.header_idx, (t, [header] + [_raw_cells(lines[r]) for r in t.row_idxs]))[1]
        cond_i = _cond_idx(header)
        for ri_, row_idx in enumerate(t.row_idxs, start=1):
            row = ctab[ri_]
            if ci >= len(row):
                continue
            cur = _unesc(row[ci])
            cond = _unesc(row[cond_i]) if cond_i < len(row) else ""
            if old_value:
                if cur != old_value and cur.lower() != old_value.lower():
                    continue
            else:
                if not cur:
                    continue                                   # never fill a cell that was left empty on purpose
                if by_title:
                    pass
                elif tgt:
                    if tgt not in _n(cond):
                        continue
                elif _NEG_RE.search(cond):
                    continue                                   # negative / boundary rows keep their special value
            if cur == new_value:
                continue
            row[ci] = _esc(new_value)
            changes.append({"scenario": b.title, "condition": cond, "column": _unesc(header[ci]), "old": cur, "new": new_value})
    if not changes:
        why = (f'no cell of "{fld}" holds the value "{old_value}"' if old_value
               else f'"{fld}" already has "{new_value}" in every row that can be changed' if any(True for _ in cols)
               else "nothing to change")
        return feature, [], why
    for t, rows in touched.values():
        lines[t.header_idx:t.row_idxs[-1] + 1 if t.row_idxs else t.header_idx + 1] = render_table(rows, t.indent)
    return "\n".join(lines), changes, ""


# ---------------------------------------------------------------------------
# edit: comment lines (never executable)
# ---------------------------------------------------------------------------
def insert_comment(feature: str, script: str, text: str, target: str = "") -> tuple[str, str, str]:
    """Returns (feature, script, note). '#' lines go in the feature, '//' lines in the script."""
    parts = [p.strip() for p in re.split(r"[\r\n]+", text or "") if p.strip()]
    if not parts:
        return feature, script, "the comment text was empty"
    lines = feature.split("\n")
    existing = {l.strip() for l in lines}
    f_new = [f"# {p}" for p in parts if f"# {p}" not in existing]
    note = ""
    if f_new:
        blocks = parse_blocks(lines)
        at, indent = len(lines), ""
        tgt = _n(target)
        pick = next((b for b in blocks if tgt and tgt in _n(b.title)), None)
        if target and pick is None:
            note = f'no scenario named "{target}" - the comment was put above the first scenario'
        pick = pick or (blocks[0] if blocks else None)
        if pick is not None:
            at, indent = pick.start, pick.indent
        lines[at:at] = [indent + l for l in f_new]
        feature = "\n".join(lines)
    s_lines = (script or "").split("\n")
    s_existing = {l.strip() for l in s_lines}
    s_new = [f"// {p}" for p in parts if f"// {p}" not in s_existing]
    if s_new:
        at = 1 if s_lines and s_lines[0].startswith("//") else 0   # right under the QC-AUTO-GENERATED marker line
        s_lines[at:at] = s_new
        script = "\n".join(s_lines)
    if not f_new and not s_new:
        return feature, script, "that comment is already there"
    return feature, script, note


# ---------------------------------------------------------------------------
# scenario specs: parse (pasted / model text), convert, validate, render, insert
# ---------------------------------------------------------------------------
@dataclass
class OutlineSpec:
    tags: list[str]
    title: str
    steps: list[tuple[str, str]]      # (keyword, text)
    header: list[str]                 # raw cells
    rows: list[list[str]]             # raw cells


_VAL_STEP_RE = re.compile(r'^(?P<pre>I\s+(?:select|enter|set)\s+)"(?P<val>[^"]*)"(?P<mid>\s+in\s+the\s+)"(?P<fld>[^"]+)"'
                          r'(?P<post>\s+(?:picklist|input|combobox|date|radio|checkbox|textarea|new\s+record)\s+field)$', re.I)
_CONF_RE = re.compile(r'^(?P<pre>I\s+should\s+see\s+a\s+confirmation\s+message\s+)"(?P<val>.*)"$', re.I)
_BTN_RE = re.compile(r"^I\s+click\s+the\s+(add\s+new|save|update|edit|delete)\s+button$", re.I)
_BTN = {"add new": "Add New", "save": "save", "update": "Update", "edit": "Edit", "delete": "Delete"}


def _canon_step(text: str) -> str:
    m = _BTN_RE.match(text.strip())
    if not m:
        return text.strip()
    key = " ".join(m.group(1).lower().split())
    return "I click the " + _BTN[key] + " button"


def parse_specs(text: str) -> tuple[list[OutlineSpec], list[str]]:
    """Scenario / Scenario Outline blocks in `text` -> outline specs (a plain Scenario is converted)."""
    problems: list[str] = []
    lines = (text or "").replace("\r\n", "\n").split("\n")
    specs: list[OutlineSpec] = []
    for b in parse_blocks(lines):
        tags = [t for l in lines[b.start:b.title_idx] for t in l.split() if t.startswith("@")]
        steps = [(kw.capitalize(), _canon_step(tx)) for _, _, kw, tx in b.steps]
        if not steps:
            problems.append(f'scenario "{b.title}" has no steps')
            continue
        if b.is_outline:
            if not b.tables:
                problems.append(f'scenario outline "{b.title}" has no Examples table')
                continue
            header = _raw_cells(lines[b.tables[0].header_idx])
            rows = [_raw_cells(lines[r]) for t in b.tables if _raw_cells(lines[t.header_idx]) == header for r in t.row_idxs]
            if any(_raw_cells(lines[t.header_idx]) != header for t in b.tables):
                problems.append(f'scenario outline "{b.title}" has Examples tables with different columns - use one table')
                continue
            specs.append(OutlineSpec(tags, b.title, steps, header, rows))
        else:
            header, row, new_steps, used = ["Condition"], [b.title or "Requested scenario"], [], {}
            has_conf = False
            for kw, tx in steps:
                m, c = _VAL_STEP_RE.match(tx), _CONF_RE.match(tx)
                if m:
                    name = m.group("fld")
                    used[name] = used.get(name, 0) + 1
                    col = name if used[name] == 1 else f"{name}{used[name]}"
                    header.append(col)
                    row.append(_esc(m.group("val")))
                    new_steps.append((kw, f'{m.group("pre")}"<{col}>"{m.group("mid")}"{m.group("fld")}"{m.group("post")}'))
                elif c:
                    has_conf = True
                    row.append(_esc(c.group("val")))
                    header.append("ExpectedResult")
                    new_steps.append((kw, f'{c.group("pre")}"<ExpectedResult>"'))
                else:
                    new_steps.append((kw, tx))
            if has_conf and header[-1] != "ExpectedResult":      # keep ExpectedResult as the last column
                i = header.index("ExpectedResult")
                header.append(header.pop(i))
                row.append(row.pop(i))
            specs.append(OutlineSpec(tags, f'{b.title or "Requested scenario"} "<Condition>"', new_steps, header, [row]))
    if not specs and not problems:
        problems.append("no Scenario / Scenario Outline was found in the text")
    return specs, problems


def _style(lines: list[str], prefer: "Block | None" = None) -> dict:
    cands = ([prefer] if prefer is not None else []) + parse_blocks(lines)
    for b in cands:
        if b.is_outline and b.tables and b.steps:
            return {"t": b.indent, "s": b.steps[0][1], "e": re.match(r"\s*", lines[b.tables[0].examples_idx]).group(0),
                    "r": b.tables[0].indent}
    return {"t": "  ", "s": "    ", "e": "    ", "r": "      "}


def render_spec(spec: OutlineSpec, st: dict) -> list[str]:
    out = []
    if spec.tags:
        out.append(st["t"] + " ".join(spec.tags))
    out.append(f'{st["t"]}Scenario Outline: {spec.title}')
    out += [f'{st["s"]}{kw} {tx}' for kw, tx in spec.steps]
    out.append(f'{st["e"]}Examples:')
    out += render_table([spec.header] + spec.rows, st["r"])
    return out


def _title_key(title: str) -> str:
    return _n(re.sub(r'"?<[^<>]*>"?', "", title or ""))


def validate_specs(specs: list[OutlineSpec], feature: str, script: str) -> tuple[list[str], list[str]]:
    """(problems, unknown_fields)."""
    problems: list[str] = []
    unknown: list[str] = []
    known = {_n(f) for f in known_fields(feature, script)}
    for sp in specs:
        ph_steps = {m for _, tx in sp.steps for m in re.findall(r"<([^<>]+)>", tx)}
        hdr = [_unesc(h) for h in sp.header]
        for ph in sorted(ph_steps - set(hdr)):
            problems.append(f'"{sp.title}": the step placeholder <{ph}> has no Examples column')
        if len(sp.rows) > MAX_ROWS:
            problems.append(f'"{sp.title}": {len(sp.rows)} Examples rows (max {MAX_ROWS})')
        if not sp.rows:
            problems.append(f'"{sp.title}": the Examples table has no rows')
        for r in sp.rows:
            if len(r) != len(sp.header):
                problems.append(f'"{sp.title}": an Examples row has {len(r)} cells, the header has {len(sp.header)}')
                break
        for _, tx in sp.steps:
            if not extended_steps.in_vocabulary(tx):
                problems.append(f'step is not in the fixed step vocabulary: "{tx}"')
            for rx in _FIELD_RES:
                for m in rx.finditer(tx):
                    f = m.group("f")
                    if known and _n(f) not in known and f not in unknown:
                        unknown.append(f)
                        problems.append(f'the field "{f}" does not exist on this screen')
    return problems, unknown


def _unique_title(title: str, taken: set[str]) -> str:
    base, k, cand = title, 2, title
    while _title_key(cand) in taken:
        cand = re.sub(r'\s*(?="?<Condition>"?$)', f" ({k}) ", base, count=1).replace("  ", " ") if "<" in base else f"{base} ({k})"
        k += 1
        if k > 50:
            break
    return cand


def insert_specs(feature: str, specs: list[OutlineSpec]) -> tuple[str, list[str]]:
    """Insert after the last scenario of the Rule block whose fields overlap the new scenarios most (else the file end)."""
    lines = feature.rstrip("\n").split("\n")
    blocks = parse_blocks(lines)
    taken = {_title_key(b.title) for b in blocks}
    titles = []
    for sp in specs:
        sp.title = _unique_title(sp.title, taken)
        taken.add(_title_key(sp.title))
        titles.append(sp.title)
    new_fields = {_n(m.group("f")) for sp in specs for _, tx in sp.steps for rx in _FIELD_RES for m in rx.finditer(tx)}
    at, best = len(lines), None
    if blocks:
        best = max(blocks, key=lambda b: (len({_n(f) for f in _fields_of(b)} & new_fields), b.start))
        same = [b for b in blocks if b.rule == best.rule]
        at = max(b.end for b in same)
    st = _style(lines, best)
    chunk: list[str] = []
    for sp in specs:
        chunk += [""] + render_spec(sp, st)
    lines[at:at] = chunk
    return "\n".join(lines) + "\n", titles


# ---------------------------------------------------------------------------
# verification
# ---------------------------------------------------------------------------
def _norm_line(l: str) -> str:
    return re.sub(r"\s*(?<!\\)\|\s*", "|", l.strip()) if l.strip().startswith("|") else l.rstrip()


def check_preserved(old: str, new: str) -> list[str]:
    """Existing lines must be unchanged. Allowed: inserted lines, and table rows whose padding / cells were edited."""
    a, b = [_norm_line(l) for l in old.split("\n")], [_norm_line(l) for l in new.split("\n")]
    problems: list[str] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag in ("equal", "insert"):
            continue
        if tag == "replace" and (i2 - i1) == (j2 - j1) and all(x.startswith("|") for x in a[i1:i2]) and all(x.startswith("|") for x in b[j1:j2]):
            continue
        problems.append(f"existing line(s) around line {i1 + 1} would change: {a[i1][:70]!r}")
        break
    return problems


def _critical(feature: str, script: str) -> int | None:
    try:
        return step_checker.critical_count(step_checker.check_steps(feature, script))
    except Exception:  # noqa: BLE001
        return None


def _described_present(feature: str, description: str) -> bool:
    words = [w for w in re.findall(r"[a-z0-9]+", description.lower()) if len(w) >= 4 and w not in _STOP]
    if not words:
        return False
    lines = feature.split("\n")
    for b in parse_blocks(lines):
        text = b.title.lower()
        for t in b.tables:
            hdr = _raw_cells(lines[t.header_idx])
            ci = _cond_idx(hdr)
            for r in t.row_idxs:
                cells = _raw_cells(lines[r])
                if ci < len(cells):
                    text += " " + cells[ci].lower()
        text = re.sub(r"[^a-z0-9 ]", " ", text)
        hit = sum(1 for w in words if w[:6] in text)
        if hit / len(words) >= 0.6:
            return True
    return False


def _value_present(feature: str, fld: str, value: str) -> bool | None:
    """True / False, or None when the field has no column at all."""
    lines = feature.split("\n")
    nf = _n(fld)
    seen = False
    for b in parse_blocks(lines):
        cf = _col_fields(b)
        for t in b.tables:
            header = _raw_cells(lines[t.header_idx])
            for ci, h in enumerate(header):
                if _n(_unesc(h)) == nf or _n(cf.get(_unesc(h), "")) == nf:
                    seen = True
                    for r in t.row_idxs:
                        cells = _raw_cells(lines[r])
                        if ci < len(cells) and _unesc(cells[ci]) == value:
                            return True
    return False if seen else None


def _full_present(feature: str, gherkin: str) -> bool:
    specs, _ = parse_specs(gherkin)
    if not specs:
        return False
    lines = feature.split("\n")
    have = {_title_key(b.title) for b in parse_blocks(lines)}
    conds = set()
    for b in parse_blocks(lines):
        for t in b.tables:
            ci = _cond_idx(_raw_cells(lines[t.header_idx]))
            for r in t.row_idxs:
                c = _raw_cells(lines[r])
                if ci < len(c):
                    conds.add(_n(_unesc(c[ci])))
    return all(_title_key(s.title) in have or _n(_unesc(s.rows[0][0])) in conds for s in specs if s.rows)


# ---------------------------------------------------------------------------
# step definitions (only when a new sentence really needs one)
# ---------------------------------------------------------------------------
async def _ensure_definitions(old_feature: str, new_feature: str, script: str, define_fn) -> tuple[str, list[str]]:
    before = set(extended_steps.undefined_steps(old_feature, script))
    new_missing = [s for s in extended_steps.undefined_steps(new_feature, script) if s not in before]
    if not new_missing:
        return script, []
    ok, not_vocab = extended_steps.split_by_vocabulary(new_missing)
    if not_vocab:
        return script, [f'step is not in the fixed step vocabulary: "{s}"' for s in not_vocab]
    problems: list[str] = []
    try:
        block, covered = template_steps.build_block({}, ok, None)
        if block:
            assembled, probs = await asyncio.to_thread(extended_steps.assemble, script, block, covered)
            if assembled:
                script = assembled
            else:
                problems += probs
    except Exception:  # noqa: BLE001
        logger.exception("fixed step templates failed in the append editor")
    left = extended_steps.undefined_steps_from_sentences(ok, script)
    if left and define_fn is not None:
        for start in range(0, len(left), 4):
            batch, err = left[start:start + 4], ""
            for _ in (1, 2):
                try:
                    block = await define_fn(batch, script, err)
                    assembled, probs = await asyncio.to_thread(extended_steps.assemble, script, block, batch)
                except Exception as e:  # noqa: BLE001
                    assembled, probs = None, [str(e)]
                if assembled:
                    script = assembled
                    break
                err = "; ".join(probs)
            else:
                problems.append(f"a step definition could not be written: {err[:160]}")
    left = extended_steps.undefined_steps_from_sentences(ok, script)
    if left:
        problems.append("no step definition for: " + "; ".join(left[:4]))
    return script, problems


# ---------------------------------------------------------------------------
# context for the model (described scenarios): small, never the whole files
# ---------------------------------------------------------------------------
def build_describe_context(feature: str, script: str) -> dict:
    lines = feature.split("\n")
    blocks = parse_blocks(lines)
    fields: dict[str, str] = {}
    for b in blocks:
        for _, _, _, tx in b.steps:
            for rx in _FIELD_RES[:1]:
                for m in rx.finditer(tx):
                    fields.setdefault(m.group("f"), re.sub(r'"<[^<>]*>"', '"<Value>"', re.sub(r'"[^"]*" in the "', '"<Value>" in the "', tx)))
    try:
        for name, info in (script_builder.controls_from_script(script) or {}).items():
            fields.setdefault(name, f'kind: {info.get("kind")}' if isinstance(info, dict) else "")
    except Exception:  # noqa: BLE001
        pass
    messages: list[str] = []
    for b in blocks:
        for t in b.tables:
            hdr = [_unesc(h) for h in _raw_cells(lines[t.header_idx])]
            if "ExpectedResult" in hdr:
                ci = hdr.index("ExpectedResult")
                for r in t.row_idxs:
                    c = _raw_cells(lines[r])
                    if ci < len(c) and _unesc(c[ci]) and _unesc(c[ci]) not in messages:
                        messages.append(_unesc(c[ci]))
    example = ""
    for b in blocks:
        if b.is_outline and b.tables:
            example = "\n".join(lines[b.start:min(b.end, b.start + 40)])
            break
    return {"fields": fields, "titles": [b.title for b in blocks][:80], "messages": messages[:40], "example": example}


# ---------------------------------------------------------------------------
# the orchestrator
# ---------------------------------------------------------------------------
@dataclass
class ApplyResult:
    feature: str
    script: str
    applied: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    present: list[str] = field(default_factory=list)     # already satisfied (only_missing mode)
    model_calls: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.applied)

    def summary(self) -> str:
        return "; ".join(self.applied) or "nothing was changed"


_ORDER = {ri.FULL_SCENARIO: 0, ri.DESCRIBED_SCENARIO: 1, ri.TEST_DATA: 2, ri.COMMENT: 3}


def _label(it: ri.Item) -> str:
    first = (it.raw.strip().split("\n") or [""])[0].strip()
    return f'"{first[:70]}"'


async def apply_items(feature: str, script: str, items: list[ri.Item], *, describe_fn=None, define_fn=None,
                      only_missing: bool = False) -> ApplyResult:
    """Applies every item. `only_missing=True` (right after generation) first checks whether the item is already
    satisfied and only fixes what is not. describe_fn(description, context, previous_error) -> scenario text;
    define_fn(sentences, script, previous_error) -> JS block."""
    res = ApplyResult(feature, script)
    for it in sorted([i for i in items if not i.unclear], key=lambda i: _ORDER.get(i.type, 9)):
        if it.type == ri.PRIORITY_HINT:
            res.skipped.append(f"{_label(it)}: a priority hint only steers generation - nothing to change in existing files")
            continue
        try:
            msg = await _apply_one(res, it, describe_fn, define_fn, only_missing)
        except Exception as e:  # noqa: BLE001
            logger.exception("append editor crashed on %s", it.raw[:60])
            msg = ("skip", f"unexpected problem ({type(e).__name__}: {e})")
        kind, text = msg
        if kind == "ok":
            res.applied.append(f"{_label(it)}: {text}")
        elif kind == "present":
            res.present.append(f"{_label(it)}: already present - {text}")
        else:
            res.skipped.append(f"{_label(it)}: {text}")
    return res


async def _apply_one(res: ApplyResult, it: ri.Item, describe_fn, define_fn, only_missing: bool) -> tuple[str, str]:
    feature, script = res.feature, res.script

    def commit(new_f: str, new_s: str, why: str) -> tuple[str, str]:
        probs = check_preserved(feature, new_f)
        if probs:
            return "skip", "; ".join(probs) + " - kept the original"
        before, after = _critical(feature, script), _critical(new_f, new_s)
        if before is not None and after is not None and after > before:
            return "skip", f"the step checker found {after - before} new problem(s) after the change - kept the original"
        res.feature, res.script = new_f, new_s
        return "ok", why

    # ---------------- comment ----------------
    if it.type == ri.COMMENT:
        f2, s2, note = insert_comment(feature, script, it.value, it.target)
        if f2 == feature and s2 == script:
            return "present", note
        kind, text = commit(f2, s2, "comment line added" + (f" ({note})" if note else ""))
        return kind, text

    # ---------------- test data ----------------
    if it.type == ri.TEST_DATA:
        if only_missing:
            pres = _value_present(feature, it.field, it.value)
            if pres is True:
                return "present", f'"{it.value}" is used for {it.field}'
            if pres is None:
                return "skip", f'the field "{it.field}" is not on this screen - requirement skipped'
        f2, changes, why = set_values(feature, it.field, it.value, it.old_value, it.target)
        if not changes:
            return "skip", why
        if _value_present(f2, it.field, it.value.replace('"', "")) is not True:
            return "skip", "the new value could not be verified in the Examples table - kept the original"
        kind, text = commit(f2, script, f'{len(changes)} cell(s) of "{changes[0]["column"]}" set to "{changes[0]["new"]}" '
                                          f'(e.g. {re.sub(chr(34) + "?<Condition>" + chr(34) + "?", "", changes[0]["scenario"]).strip()[:40]} / {changes[0]["condition"][:30]})')
        return kind, text

    # ---------------- full scenario ----------------
    if it.type == ri.FULL_SCENARIO:
        if only_missing and _full_present(feature, it.value):
            return "present", "the scenario is in the feature"
        specs, probs = parse_specs(it.value)
        if probs:
            return "skip", "; ".join(probs)
        return await _insert_specs(res, specs, define_fn, "scenario added")

    # ---------------- described scenario ----------------
    if it.type == ri.DESCRIBED_SCENARIO:
        if only_missing and _described_present(feature, it.value):
            return "present", "a matching scenario is in the feature"
        if describe_fn is None:
            return "skip", "no model is available to write the scenario"
        ctx, err, last = build_describe_context(feature, script), "", ""
        for attempt in (1, 2):
            res.model_calls += 1
            text = await describe_fn(it.value, ctx, err)
            if (text or "").strip().upper().startswith("ERROR:"):
                return "skip", "the model could not write it: " + text.strip()[6:].strip()[:200]
            specs, probs = parse_specs(text)
            unknown: list[str] = []
            if not probs:
                probs, unknown = validate_specs(specs, feature, script)
            if not probs:
                kind, msg = await _insert_specs(res, specs, define_fn, "scenario written by the model and added")
                if kind == "ok" or attempt == 2:
                    return kind, msg
                err = msg
                last = msg
                continue
            err = "; ".join(probs)
            last = err
            if attempt == 1:
                continue
        if last and "does not exist on this screen" in last:
            return "skip", "needs a field this screen does not have (" + last[:200] + ") - skipped"
        return "skip", "could not be written in a valid form after one retry: " + last[:220]
    return "skip", "unsupported item"


async def _insert_specs(res: ApplyResult, specs: list[OutlineSpec], define_fn, ok_text: str) -> tuple[str, str]:
    feature, script = res.feature, res.script
    probs, unknown = validate_specs(specs, feature, script)
    if probs:
        if unknown:
            return "skip", "needs a field this screen does not have: " + ", ".join(unknown) + " - skipped"
        return "skip", "; ".join(probs[:4])
    new_f, titles = insert_specs(feature, specs)
    new_s, dprobs = await _ensure_definitions(feature, new_f, script, define_fn)
    if dprobs:
        return "skip", "; ".join(dprobs[:3]) + " - kept the original"
    pb = check_preserved(feature, new_f)
    if pb:
        return "skip", "; ".join(pb) + " - kept the original"
    before, after = _critical(feature, script), _critical(new_f, new_s)
    if before is not None and after is not None and after > before:
        return "skip", f"the step checker found {after - before} new problem(s) - kept the original"
    lines = new_f.split("\n")
    if not all(any(_title_key(b.title) == _title_key(t) for b in parse_blocks(lines)) for t in titles):
        return "skip", "the new scenario could not be found after insertion - kept the original"
    res.feature, res.script = new_f, new_s
    extra = " (+ step definition(s) added to the script)" if new_s != script else ""
    return "ok", f'{ok_text}: {"; ".join(t[:60] for t in titles)}{extra}'