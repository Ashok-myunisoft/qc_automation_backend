"""Universal "Refine this screen" engine.

The box accepts ANY request about the feature / script / test data of one screen. A request is split into jobs:

  EDIT      any change to the existing files that is not a plain value / comment / pasted scenario:
            "add boundary tests for Amount", "rename scenario 2", "more rows in the negative scenario",
            "remove the duplicate delete scenario". The model returns block-level operations (add / replace /
            delete one scenario); CODE validates and applies each one (known fields only, fixed step vocabulary,
            rows never silently lost, step checker not worse, step definitions added when needed).
  DEEPEN    "not satisfied", "generate again", "more coverage", "more test data": the screen is generated again
            in a thorough mode (app.py) and the new scenarios / data rows are MERGED INTO the existing files
            (merge_new_feature below). Nothing that already exists is thrown away.
  QUESTION  "what does this cover?", "which fields have no scenario?": answered in the box, files untouched.

Value changes, comment lines and pasted scenarios keep using service/append_editor.py (no model needed).
Every function here is a pure helper or takes the model call as an argument, so it can be tested without a model.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from service import append_editor as ae

logger = logging.getLogger(__name__)

EDIT, DEEPEN, QUESTION = "EDIT", "DEEPEN", "QUESTION"
FAST = "FAST"          # handled by service/append_editor.py without any model (value change, comment, pasted scenario)
MAX_ROWS_PER_TABLE = 60
_CONTEXT_FULL_LIMIT = 24000
_SNAPSHOT_LIMIT = 12


# ---------------------------------------------------------------------------
# 1) routing
# ---------------------------------------------------------------------------
_DEEPEN_RE = re.compile(
    r"\b(?:re-?generate|generate\s+(?:it\s+|them\s+|this\s+)?again|do\s+it\s+again|try\s+again|"
    r"not\s+(?:satisfied|happy|ok|okay|enough|good|sufficient)|unsatisfied|dissatisfied|"
    r"(?:more|better|wider|deeper|full|complete|extra|additional|increase)\s+(?:the\s+)?(?:test\s+)?"
    r"(?:coverage|scenarios?|test\s?cases?|test\s?data|data|detail\w*|depth)|"
    r"coverage\s+(?:is\s+)?(?:low|poor|less|not|missing|incomplete)|missing\s+(?:scenarios?|coverage|test\s?cases?)|"
    r"improve\s+(?:the\s+)?(?:coverage|feature|test\s?data)|very\s+detailed|in\s+detail|thorough\w*)\b", re.I)
_QUESTION_START_RE = re.compile(
    r"^\s*(?:what|which|why|how\s+many|how|does|do|is|are|can\s+you\s+(?:tell|explain|list|show)|explain|list|"
    r"tell\s+me|summari[sz]e|describe)\b", re.I)
_DELETE_RE = re.compile(r"\b(?:remove|delete|drop|discard|get\s+rid|take\s+out|eliminate)\b", re.I)


def looks_like_deepen(text: str) -> bool:
    return bool(_DEEPEN_RE.search(text or ""))


def fallback_jobs(text: str) -> list[dict]:
    """Used only when the routing model is unavailable or answers badly."""
    t = (text or "").strip()
    if t.endswith("?") and _QUESTION_START_RE.match(t):
        return [{"intent": QUESTION, "text": t}]
    if looks_like_deepen(t):
        return [{"intent": DEEPEN, "text": t}]
    return [{"intent": EDIT, "text": t}]


def _json_obj(raw: str):
    t = (raw or "").strip()
    t = re.sub(r"^```[a-zA-Z]*[ \t]*\r?\n", "", t)
    t = re.sub(r"\r?\n```\s*$", "", t)
    a, b = t.find("{"), t.rfind("}")
    if a < 0 or b <= a:
        return None
    try:
        return json.loads(t[a:b + 1])
    except Exception:  # noqa: BLE001
        return None


async def route(text: str, route_fn=None) -> list[dict]:
    """-> ordered jobs [{"intent": EDIT|DEEPEN|QUESTION, "text": "..."}]. Never raises, never returns an empty list.
    EDIT / QUESTION jobs run first and DEEPEN last (so a deepen run builds on the edited files)."""
    t = (text or "").strip()
    jobs: list[dict] = []
    if route_fn is not None:
        try:
            data = _json_obj(await route_fn(t))
            for j in (data or {}).get("jobs") or []:
                intent = str((j or {}).get("intent") or "").upper()
                jtext = str((j or {}).get("text") or "").strip()
                if intent in (EDIT, DEEPEN, QUESTION) and jtext:
                    jobs.append({"intent": intent, "text": jtext})
        except Exception:  # noqa: BLE001
            logger.warning("refine router failed - using the keyword fallback", exc_info=True)
    if not jobs:
        jobs = fallback_jobs(t)
    # a deepen wish the model missed (e.g. "not satisfied") must never be lost
    if looks_like_deepen(t) and not any(j["intent"] == DEEPEN for j in jobs) and not t.endswith("?"):
        jobs.append({"intent": DEEPEN, "text": t})
    order = {EDIT: 0, QUESTION: 1, DEEPEN: 2}
    return sorted(jobs, key=lambda j: order[j["intent"]])


# ---------------------------------------------------------------------------
# 2) undo (snapshots live in the session, so users can step back without anyone)
# ---------------------------------------------------------------------------
def _key(holder: dict) -> str:
    r = holder.get("resolved")
    return getattr(r, "feature_path", None) or "single"


def capture(holder: dict) -> dict:
    """The state to go back to. Take it BEFORE changing anything, push it only when something really changed."""
    return {"feature": holder.get("feature"), "script": holder.get("script"),
            "pushed": holder.get("pushed"), "modified": holder.get("modified")}


def push_snapshot(session: dict, holder: dict, snap: dict) -> None:
    stack = session.setdefault("refine_undo", {}).setdefault(_key(holder), [])
    stack.append(snap)
    del stack[:-_SNAPSHOT_LIMIT]


def can_undo(session: dict, holder: dict) -> bool:
    return bool((session.get("refine_undo") or {}).get(_key(holder)))


def undo(session: dict, holder: dict) -> bool:
    stack = (session.get("refine_undo") or {}).get(_key(holder)) or []
    if not stack:
        return False
    snap = stack.pop()
    holder["feature"], holder["script"] = snap["feature"], snap["script"]
    holder["pushed"] = snap.get("pushed")
    if snap.get("modified") is None:
        holder.pop("modified", None)
    else:
        holder["modified"] = snap["modified"]
    return True


# ---------------------------------------------------------------------------
# 3) reading a feature: overview (questions) and context (edits)
# ---------------------------------------------------------------------------
def _rows_of(lines: list[str], b: ae.Block) -> list[list[str]]:
    out: list[list[str]] = []
    for t in b.tables:
        out += [[ae._unesc(c) for c in ae._raw_cells(lines[r])] for r in t.row_idxs]
    return out


def overview(feature: str, script: str = "", limit: int = 14000) -> str:
    """Compact text description of the feature: every scenario, its row count and the first conditions."""
    lines = (feature or "").replace("\r\n", "\n").split("\n")
    blocks = ae.parse_blocks(lines)
    total_rows = 0
    parts: list[str] = []
    for i, b in enumerate(blocks, 1):
        rows = _rows_of(lines, b)
        total_rows += len(rows)
        hdr = ""
        if b.tables:
            hdr = ", ".join(ae._unesc(c) for c in ae._raw_cells(lines[b.tables[0].header_idx]))
        conds = []
        if b.tables:
            ci = ae._cond_idx(ae._raw_cells(lines[b.tables[0].header_idx]))
            for r in b.tables[0].row_idxs[:6]:
                c = ae._raw_cells(lines[r])
                if ci < len(c):
                    conds.append(ae._unesc(c[ci])[:50])
        parts.append(f"{i}. {b.title}  [rule: {b.rule or '-'}; {len(rows)} data row(s); columns: {hdr or '-'}]"
                     + (f"\n     rows: {' | '.join(conds)}" if conds else ""))
    ctx = ae.build_describe_context(feature, script or "")
    head = (f"{len(blocks)} scenario(s), {total_rows} data row(s) in total.\n"
            f"Fields used: {', '.join(ctx['fields']) or '(none found)'}\n")
    return (head + "\n".join(parts))[:limit]


def _words(text: str) -> set[str]:
    return {w[:6] for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) >= 3 and w not in ae._STOP}


def edit_context(feature: str, script: str, instruction: str) -> dict:
    """What the editing model sees: the whole feature when it is small, else the header, every title and only the
    scenarios that look related to the instruction."""
    text = (feature or "").replace("\r\n", "\n")
    lines = text.split("\n")
    blocks = ae.parse_blocks(lines)
    ctx = ae.build_describe_context(feature, script or "")
    titles = [b.title for b in blocks]
    if len(text) <= _CONTEXT_FULL_LIMIT or not blocks:
        shown = text
        note = "the complete feature file"
    else:
        want = _words(instruction)
        scored = sorted(blocks, key=lambda b: -len(want & _words(b.title + " " + " ".join(" ".join(r) for r in _rows_of(lines, b)[:8]))))
        keep = sorted(scored[:8], key=lambda b: b.start)
        head = "\n".join(lines[:blocks[0].start]).rstrip()
        shown = head + "\n\n" + "\n\n".join("\n".join(lines[b.start:b.end]) for b in keep)
        note = "the feature header and the scenarios most related to the request (the file has more - see the titles list)"
    return {"text": shown, "note": note, "titles": titles, "fields": ctx["fields"], "messages": ctx["messages"]}


# ---------------------------------------------------------------------------
# 4) free-form edit: model returns block operations, code validates and applies each
# ---------------------------------------------------------------------------
@dataclass
class Op:
    op: str
    title: str = ""
    gherkin: str = ""


def parse_ops(raw: str) -> tuple[str, list[Op], str]:
    """-> (summary, ops, problem). `problem` is set when the answer cannot be used at all."""
    t = (raw or "").strip()
    if t.upper().startswith("ERROR:"):
        return "", [], t[6:].strip()[:300] or "the model could not do this"
    data = _json_obj(t)
    if not isinstance(data, dict):
        return "", [], "the answer was not valid JSON - answer with ONLY the JSON object"
    ops: list[Op] = []
    for o in data.get("operations") or []:
        if not isinstance(o, dict):
            continue
        kind = str(o.get("op") or "").lower()
        if kind in ("add", "replace", "delete"):
            ops.append(Op(kind, str(o.get("title") or "").strip(), str(o.get("gherkin") or "").strip("\n")))
    if not ops:
        return str(data.get("summary") or ""), [], str(data.get("reason") or "no operations were returned")
    return str(data.get("summary") or ""), ops, ""


def _find_block(lines: list[str], title: str):
    blocks = ae.parse_blocks(lines)
    key = ae._title_key(title)
    if not key:
        return None
    for b in blocks:
        if ae._title_key(b.title) == key:
            return b
    cand = [b for b in blocks if key in ae._title_key(b.title) or ae._title_key(b.title) in key]
    if len(cand) == 1:
        return cand[0]
    want = _words(title)
    best, score = None, 0.0
    for b in blocks:
        have = _words(b.title)
        if want and have:
            s = len(want & have) / max(len(want), len(have))
            if s > score:
                best, score = b, s
    return best if score >= 0.8 else None


def _row_count(feature: str, title: str) -> int:
    lines = feature.split("\n")
    b = _find_block(lines, title)
    return len(_rows_of(lines, b)) if b else 0


async def _apply_replace(res: ae.ApplyResult, op: Op, define_fn, allow_delete: bool) -> tuple[str, str]:
    feature, script = res.feature, res.script
    lines = feature.split("\n")
    b = _find_block(lines, op.title)
    if b is None:
        return "skip", f'no scenario titled "{op.title[:70]}" exists - use the exact title from the list'
    specs, probs = ae.parse_specs(op.gherkin)
    if probs or len(specs) != 1:
        return "skip", "; ".join(probs) or "a replace needs exactly one Scenario Outline"
    spec = specs[0]
    probs, unknown = ae.validate_specs(specs, feature, script)
    probs = [p for p in probs if "Examples rows (max" not in p or len(spec.rows) > MAX_ROWS_PER_TABLE]
    if probs:
        if unknown:
            return "skip", "needs a field this screen does not have: " + ", ".join(unknown)
        return "skip", "; ".join(probs[:4])
    old_rows = len(_rows_of(lines, b))
    if len(spec.rows) < old_rows and not allow_delete:
        return "skip", (f'the replacement of "{b.title[:50]}" has {len(spec.rows)} data row(s) but the scenario had {old_rows} - '
                        f"rows are never removed unless you ask for it; keep all existing rows and add the new ones")
    if not spec.tags:
        spec.tags = [t for l in lines[b.start:b.title_idx] for t in l.split() if t.startswith("@")]
    new_block = ae.render_spec(spec, ae._style(lines, b))
    new_f = "\n".join(lines[:b.start] + new_block + lines[b.end:])
    new_s, dprobs = await ae._ensure_definitions(feature, new_f, script, define_fn)
    if dprobs:
        return "skip", "; ".join(dprobs[:3])
    before, after = ae._critical(feature, script), ae._critical(new_f, new_s)
    if before is not None and after is not None and after > before:
        return "skip", f"the step checker found {after - before} new problem(s) after the change"
    res.feature, res.script = new_f, new_s
    extra = " (+ step definition(s) added)" if new_s != script else ""
    return "ok", f'scenario "{b.title[:60]}" updated: {old_rows} -> {len(spec.rows)} data row(s){extra}'


async def _apply_delete(res: ae.ApplyResult, op: Op, allow_delete: bool) -> tuple[str, str]:
    if not allow_delete:
        return "skip", "a scenario is only deleted when you ask for it clearly (remove / delete)"
    lines = res.feature.split("\n")
    b = _find_block(lines, op.title)
    if b is None:
        return "skip", f'no scenario titled "{op.title[:70]}" exists'
    end = b.end
    if end < len(lines) and not lines[end].strip():
        end += 1
    new_f = "\n".join(lines[:b.start] + lines[end:])
    before, after = ae._critical(res.feature, res.script), ae._critical(new_f, res.script)
    if before is not None and after is not None and after > before:
        return "skip", "the step checker found new problem(s) after the deletion"
    res.feature = new_f
    return "ok", f'scenario "{b.title[:60]}" removed'


async def freeform_edit(feature: str, script: str, instruction: str, edit_fn, define_fn=None,
                        max_attempts: int = 3) -> ae.ApplyResult:
    """edit_fn(instruction, context, previous_error) -> raw model text (JSON). Failed operations are sent back to the
    model with their exact reason (up to max_attempts); operations that passed stay applied."""
    res = ae.ApplyResult(feature, script)
    allow_delete = bool(_DELETE_RE.search(instruction))
    err = ""
    failed: list[str] = []
    for attempt in range(1, max_attempts + 1):
        ctx = edit_context(res.feature, res.script, instruction)
        res.model_calls += 1
        try:
            raw = await edit_fn(instruction, ctx, err)
        except Exception as e:  # noqa: BLE001
            failed = [f"the model call failed ({type(e).__name__})"]
            break
        summary, ops, problem = parse_ops(raw)
        if problem:
            failed = [problem]
            err = problem
            if "valid JSON" in problem or "no operations" in problem:
                continue
            break                      # the model said it cannot do this - do not retry
        failed = []
        for op in ops:
            try:
                if op.op == "add":
                    specs, probs = ae.parse_specs(op.gherkin)
                    if probs:
                        kind, msg = "skip", "; ".join(probs)
                    else:
                        kind, msg = await ae._insert_specs(res, specs, define_fn, "scenario added")
                elif op.op == "replace":
                    kind, msg = await _apply_replace(res, op, define_fn, allow_delete)
                else:
                    kind, msg = await _apply_delete(res, op, allow_delete)
            except Exception as e:  # noqa: BLE001
                logger.exception("refine op crashed")
                kind, msg = "skip", f"unexpected problem ({type(e).__name__}: {e})"
            if kind == "ok":
                res.applied.append(msg)
            else:
                failed.append(f'{op.op} "{(op.title or op.gherkin.split(chr(10))[0])[:50]}": {msg}')
        if not failed:
            break
        err = ("These operations were rejected by the checker:\n- " + "\n- ".join(failed)
               + ("\nThe operations that passed are already applied to the file you now see." if res.applied else "")
               + "\nFix ONLY the rejected ones and answer again with the same JSON format.")
    res.skipped += failed
    return res


# ---------------------------------------------------------------------------
# 5) deepen: directive for the generator + merge of the new output into the existing files
# ---------------------------------------------------------------------------
def _used_values(feature: str, limit: int = 40) -> list[str]:
    lines = (feature or "").replace("\r\n", "\n").split("\n")
    seen: dict[str, None] = {}
    for b in ae.parse_blocks(lines):
        for t in b.tables:
            hdr = [ae._unesc(h) for h in ae._raw_cells(lines[t.header_idx])]
            cols = [i for i, h in enumerate(hdr) if re.search(r"code|name|number|\bid\b|title", h, re.I)]
            for r in t.row_idxs:
                cells = ae._raw_cells(lines[r])
                for ci in cols:
                    if ci < len(cells):
                        v = ae._unesc(cells[ci]).strip()
                        if v and not v.startswith("<"):
                            seen.setdefault(v, None)
    return list(seen)[:limit]


def deepen_directive(feature: str, user_text: str) -> str:
    lines = (feature or "").replace("\r\n", "\n").split("\n")
    titles = [re.sub(r'\s*"?<Condition>"?\s*$', "", b.title) for b in ae.parse_blocks(lines)][:60]
    used = _used_values(feature)
    focus = ""
    if (user_text or "").strip() and not re.fullmatch(r"(?i)\W*(?:please\W*)?(?:re-?generate|generate\s+again|try\s+again|not\s+satisfied)\W*", user_text.strip()):
        focus = (f'\nTHE USER\'S OWN WORDS (follow these first, they decide what to add): "{user_text.strip()[:600]}"')
    return (
        "COVERAGE DEEPENING REQUEST (mandatory): the QC user reviewed the current tests and is NOT satisfied with the "
        "coverage or the test data. Write a much more thorough set than a normal run:\n"
        "- For EVERY field on the screen cover: a valid value, empty / mandatory, over the maximum length, wrong format, "
        "boundary values, duplicates and special characters wherever the field type allows them.\n"
        "- Give every scenario many Examples rows (6 to 12 distinct, realistic data cases, never the same value twice); use "
        "past, present and future dates; use every option that a picklist / combobox / radio really has.\n"
        "- Cover every button and flow that exists: add, edit / update, delete (confirm and cancel), cancel, clear, search / "
        "filter / sort, list and grid checks, calculated fields, dependent fields, status changes and every message that exists.\n"
        "- Scenarios that already exist are listed below. Do NOT repeat them: write DIFFERENT additional scenarios and "
        "additional data cases for the same areas.\n"
        "- Every record code / name you create must be NEW - different from every value in the list of used values."
        + focus
        + ("\nEXISTING SCENARIO TITLES:\n- " + "\n- ".join(titles) if titles else "")
        + ("\nVALUES ALREADY USED IN THE FILE (do not reuse for new records): " + ", ".join(used) if used else ""))


def _row_key(cells: list[str]) -> str:
    return "|".join(re.sub(r"\s+", " ", c).strip().lower() for c in cells)


async def merge_new_feature(res: ae.ApplyResult, new_feature: str, define_fn=None) -> dict:
    """Merges the scenarios of a freshly generated feature into res.feature (existing text untouched):
       * same title + same columns  -> only the Examples rows that are not there yet are appended
       * new title                  -> the scenario is inserted (validated, step definitions added when needed)
    Returns {"scenarios": n, "rows": n, "skipped": [..]} - res.feature / res.script are updated in place."""
    stats = {"scenarios": 0, "rows": 0, "skipped": []}
    specs, probs = ae.parse_specs(new_feature)
    stats["skipped"] += [f"generated scenario ignored: {p}" for p in probs]
    for spec in specs:
        lines = res.feature.split("\n")
        existing = next((b for b in ae.parse_blocks(lines) if ae._title_key(b.title) == ae._title_key(spec.title)), None)
        if existing is not None:
            tbl = existing.tables[0] if len(existing.tables) == 1 else None
            if tbl is not None and [ae._unesc(c).lower() for c in ae._raw_cells(lines[tbl.header_idx])] == \
                    [ae._unesc(c).lower() for c in spec.header]:
                have = {_row_key([ae._unesc(c) for c in ae._raw_cells(lines[r])]) for r in tbl.row_idxs}
                room = MAX_ROWS_PER_TABLE - len(tbl.row_idxs)
                fresh = []
                for r in spec.rows:
                    k = _row_key([ae._unesc(c) for c in r])
                    if k not in have and len(r) == len(spec.header):
                        have.add(k)
                        fresh.append(r)
                fresh = fresh[:max(room, 0)]
                if not fresh:
                    continue
                old_rows = [ae._raw_cells(lines[r]) for r in tbl.row_idxs]
                table = ae.render_table([ae._raw_cells(lines[tbl.header_idx])] + old_rows + fresh, tbl.indent)
                lines[tbl.header_idx:tbl.row_idxs[-1] + 1 if tbl.row_idxs else tbl.header_idx + 1] = table
                res.feature = "\n".join(lines)
                stats["rows"] += len(fresh)
                res.applied.append(f'{len(fresh)} new data row(s) added to "{existing.title[:50]}"')
                continue
        kind, msg = await ae._insert_specs(res, [spec], define_fn, "scenario added")
        if kind == "ok":
            stats["scenarios"] += 1
            stats["rows"] += len(spec.rows)
            res.applied.append(f'new scenario "{spec.title[:60]}" ({len(spec.rows)} row(s))')
        else:
            stats["skipped"].append(f'new scenario "{spec.title[:50]}" not added: {msg[:140]}')
    return stats