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
  SCRIPT    a change to the Cypress script itself ("change the selector of the Amount field", "add a wait before save").
            The model returns exact find / replace operations; CODE checks every one (the text must exist once, the
            result must still be valid JavaScript, the step checker must not get worse) before anything is applied.
  CLARIFY   the request cannot be done without one missing detail: the box asks one short question, nothing is changed.

Value changes, comment lines and pasted scenarios keep using service/append_editor.py (no model needed).
Every function here is a pure helper or takes the model call as an argument, so it can be tested without a model.
"""
from __future__ import annotations

import asyncio
import difflib
import json
import logging
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from service import append_editor as ae

logger = logging.getLogger(__name__)

EDIT, DEEPEN, QUESTION = "EDIT", "DEEPEN", "QUESTION"
SCRIPT, CLARIFY = "SCRIPT", "CLARIFY"
_INTENTS = (EDIT, DEEPEN, QUESTION, SCRIPT, CLARIFY)
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
_SCRIPT_RE = re.compile(r"\b(?:script|cypress|step\s+definitions?|selectors?|locators?|cy\.\w+)\b|\.js\b", re.I)
_FEATURE_WORDS_RE = re.compile(r"\b(?:scenarios?|rows?|examples?|test\s?data|test\s?cases?|coverage)\b", re.I)
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
    if _SCRIPT_RE.search(t) and not _FEATURE_WORDS_RE.search(t):
        return [{"intent": SCRIPT, "text": t}]
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


async def route(text: str, route_fn=None, history: str = "") -> list[dict]:
    """-> ordered jobs [{"intent": EDIT|SCRIPT|QUESTION|CLARIFY|DEEPEN, "text": "..."}]. Never raises, never returns an empty list.
    EDIT jobs run first, then SCRIPT, then QUESTION / CLARIFY, and DEEPEN last (so a deepen run builds on the edited files).
    `history` is the recent chat of this screen (see chat_text); the router uses it to understand "do the same for ..."."""
    t = (text or "").strip()
    jobs: list[dict] = []
    if route_fn is not None:
        try:
            data = _json_obj(await (route_fn(t, history) if history else route_fn(t)))
            for j in (data or {}).get("jobs") or []:
                intent = str((j or {}).get("intent") or "").upper()
                jtext = str((j or {}).get("text") or "").strip()
                if intent in _INTENTS and jtext:
                    jobs.append({"intent": intent, "text": jtext})
        except Exception:  # noqa: BLE001
            logger.warning("refine router failed - using the keyword fallback", exc_info=True)
    if not jobs:
        jobs = fallback_jobs(t)
    # a deepen wish the model missed (e.g. "not satisfied") must never be lost
    if looks_like_deepen(t) and not any(j["intent"] == DEEPEN for j in jobs) and not t.endswith("?"):
        jobs.append({"intent": DEEPEN, "text": t})
    order = {EDIT: 0, SCRIPT: 1, QUESTION: 2, CLARIFY: 2, DEEPEN: 3}
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

# ---------------------------------------------------------------------------
# 6) chat memory of the box (kept in the session, per screen) - lets "do the same for Quantity" work
# ---------------------------------------------------------------------------
_CHAT_KEEP = 12


def result_summary(applied: list[str], skipped: list[str], answers: list[str]) -> str:
    """One short line describing what a message led to (stored as the 'result' of that chat turn)."""
    bits: list[str] = []
    if applied:
        bits.append("changed: " + "; ".join(a[:140] for a in applied[:4]))
    if skipped:
        bits.append("could not do: " + "; ".join(a[:140] for a in skipped[:3]))
    if answers:
        bits.append("replied: " + answers[0][:240])
    return " | ".join(bits) or "nothing was changed"


def push_chat(session: dict, key: str, user_text: str, result: str) -> None:
    turns = session.setdefault("refine_chat", {}).setdefault(key, [])
    turns.append({"user": (user_text or "").strip()[:500], "result": (result or "").strip()[:500]})
    del turns[:-_CHAT_KEEP]


def chat_text(session: dict, key: str, turns: int = 6) -> str:
    """The last few turns of this screen's box as plain text ('' when there are none)."""
    rows = ((session.get("refine_chat") or {}).get(key) or [])[-turns:]
    return "\n".join(f"USER: {r['user']}\nRESULT: {r['result']}" for r in rows)


# ---------------------------------------------------------------------------
# 7) what the answering model sees (questions): the real file text, not only the overview
# ---------------------------------------------------------------------------
_ANSWER_FEATURE_LIMIT = 30000
_ANSWER_SCRIPT_LIMIT = 14000


def answer_context(feature: str, script: str, question: str) -> dict:
    """{"overview", "feature", "script", "fields", "messages"} - all plain text. The complete feature / script when they
    are small enough, otherwise the parts that look related to the question."""
    ctx = edit_context(feature, script, question)
    fe = ctx["text"] if len(ctx["text"]) <= _ANSWER_FEATURE_LIMIT else ctx["text"][:_ANSWER_FEATURE_LIMIT]
    sc = script_context(script, question, _ANSWER_SCRIPT_LIMIT)["text"]
    fields = "\n".join(f"- {n}: {h}" for n, h in (ctx.get("fields") or {}).items())
    messages = "\n".join(f"- {m}" for m in (ctx.get("messages") or []))
    return {"overview": overview(feature, script), "feature": fe, "script": sc, "fields": fields, "messages": messages}


# ---------------------------------------------------------------------------
# 8) script edit: the model returns exact find / replace operations, code checks and applies them
# ---------------------------------------------------------------------------
_SCRIPT_FULL_LIMIT = 28000
MAX_SCRIPT_OPS = 12
_STEP_DEF_RE = re.compile(r"^\s*(?:Given|When|Then|And|But)\s*\(", re.M)
_FEATURE_STEP_RE = re.compile(r"^\s*(?:Given|When|Then|And|But)\s+(.+?)\s*$")


def feature_steps(feature: str, limit: int = 80) -> list[str]:
    """Every distinct step sentence the feature uses (the script has to keep defining all of them)."""
    seen: dict[str, None] = {}
    for line in (feature or "").replace("\r\n", "\n").split("\n"):
        m = _FEATURE_STEP_RE.match(line)
        if m:
            seen.setdefault(m.group(1), None)
    return list(seen)[:limit]


def script_context(script: str, instruction: str, limit: int = _SCRIPT_FULL_LIMIT) -> dict:
    """The whole script when it is small, else its first lines plus the blocks that look related to the request."""
    text = (script or "").replace("\r\n", "\n")
    if len(text) <= limit:
        return {"text": text, "note": "the complete script"}
    blocks = [b for b in re.split(r"\n\s*\n", text) if b.strip()]
    head = "\n".join(text.split("\n")[:40])
    want = _words(instruction)
    order = sorted(range(len(blocks)), key=lambda i: -len(want & _words(blocks[i])))
    keep: list[int] = []
    size = len(head)
    for i in order:
        if size + len(blocks[i]) > limit:
            continue
        keep.append(i)
        size += len(blocks[i])
        if len(keep) >= 12:
            break
    shown = head + "\n\n// ... (parts of the script that look unrelated are left out) ...\n\n" + "\n\n".join(blocks[i] for i in sorted(keep))
    return {"text": shown[:limit], "note": "the start of the script and the blocks most related to the request (the script has more)"}


@dataclass
class ScriptOp:
    op: str
    find: str = ""
    text: str = ""


def parse_script_ops(raw: str) -> tuple[str, list[ScriptOp], str]:
    """-> (summary, ops, problem). `problem` is set when the answer cannot be used at all."""
    t = (raw or "").strip()
    if t.upper().startswith("ERROR:"):
        return "", [], t[6:].strip()[:300] or "the model could not do this"
    data = _json_obj(t)
    if not isinstance(data, dict):
        return "", [], "the answer was not valid JSON - answer with ONLY the JSON object"
    ops: list[ScriptOp] = []
    for o in data.get("operations") or []:
        if not isinstance(o, dict):
            continue
        kind = str(o.get("op") or "").lower()
        if kind not in ("replace", "insert_after", "insert_before", "append"):
            continue
        new = o["replace"] if "replace" in o else o.get("text", "")
        ops.append(ScriptOp(kind, str(o.get("find") or "").strip("\n"), str(new if new is not None else "").strip("\n")))
    if not ops:
        return str(data.get("summary") or ""), [], str(data.get("reason") or "no operations were returned")
    return str(data.get("summary") or ""), ops, ""


def _apply_script_op(script: str, op: ScriptOp, allow_delete: bool) -> tuple[str, str, str]:
    """-> (new_script, 'ok' | 'skip', message). The anchor text must exist exactly once."""
    if op.op == "append":
        if not op.text.strip():
            return script, "skip", "append needs the text to add"
        n = len(op.text.split("\n"))
        return script.rstrip("\n") + "\n\n" + op.text + "\n", "ok", f"{n} line(s) added at the end of the script"
    find = op.find
    if not find.strip():
        return script, "skip", f"{op.op} needs `find` = exact text that already exists in the script"
    text = op.text
    chosen = None
    for cand in ([find] + ([find.strip()] if find.strip() != find else [])):
        if script.count(cand) >= 1:
            chosen = cand
            break
    if chosen is None:
        return script, "skip", ("the `find` text was not found in the script - copy it exactly, character for character, "
                                f"from THE SCRIPT shown (first line: {find.split(chr(10))[0][:80]!r})")
    if script.count(chosen) > 1:
        return script, "skip", (f"the `find` text appears {script.count(chosen)} times - include more of the surrounding "
                                f"lines so it matches exactly one place (first line: {chosen.split(chr(10))[0][:80]!r})")
    if chosen is not find:
        text = text.strip()
    i = script.index(chosen)
    if op.op == "replace":
        if not allow_delete and len(_STEP_DEF_RE.findall(chosen)) > len(_STEP_DEF_RE.findall(text)):
            return script, "skip", "this removes a step definition - only do that when the user clearly asks to remove / delete it"
        new = script[:i] + text + script[i + len(chosen):]
        return new, "ok", f"script block changed ({len(chosen.split(chr(10)))} -> {len(text.split(chr(10)))} line(s))"
    if not text.strip():
        return script, "skip", f"{op.op} needs the text to insert"
    n = len(text.split("\n"))
    if op.op == "insert_after":
        end = i + len(chosen)
        nl = script.find("\n", end)
        end = len(script) if nl < 0 else nl
        return script[:end] + "\n" + text + script[end:], "ok", f"{n} line(s) added to the script"
    start = script.rfind("\n", 0, i) + 1                      # insert_before: at the start of the anchor's line
    return script[:start] + text + "\n" + script[start:], "ok", f"{n} line(s) added to the script"


def _bracket_balance(src: str) -> tuple[int, int, int]:
    """Net count of ( [ { outside strings / comments (only used when node is not available; compared before vs after)."""
    out = {"(": 0, "[": 0, "{": 0}
    close = {")": "(", "]": "[", "}": "{"}
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c in "\"'`":
            j = i + 1
            while j < n and src[j] != c:
                if src[j] == "\\":
                    j += 1
                elif src[j] == "\n" and c != "`":
                    break
                j += 1
            i = j + 1
            continue
        if src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c in out:
            out[c] += 1
        elif c in close:
            out[close[c]] -= 1
        i += 1
    return out["("], out["["], out["{"]


def js_syntax_error(src: str) -> str | None:
    """None when the script parses (or when no way to check exists). Uses `node --check` when node is installed."""
    node = shutil.which("node")
    if not node:
        return None
    try:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "check.mjs"
            p.write_text(src, encoding="utf-8")
            r = subprocess.run([node, "--check", str(p)], capture_output=True, text=True, timeout=20)
    except Exception:  # noqa: BLE001
        return None
    if r.returncode == 0:
        return None
    lines = [l.strip() for l in (r.stderr or "").splitlines() if l.strip()]
    pick = next((l for l in lines if "SyntaxError" in l), lines[-1] if lines else "invalid JavaScript")
    return "JavaScript syntax error: " + pick[:200]


async def _script_problem(feature: str, old: str, new: str, syntax_check=None) -> str:
    """'' when the edited script is acceptable, else a plain reason (sent back to the model)."""
    if not new.strip():
        return "the script would be empty"
    check = syntax_check or js_syntax_error
    old_err = await asyncio.to_thread(check, old)
    if old_err is None:                                   # only judge syntax when the original script was valid
        new_err = await asyncio.to_thread(check, new)
        if new_err:
            return new_err
        if shutil.which("node") is None and _bracket_balance(new) != _bracket_balance(old):
            return "the brackets / braces no longer balance"
    before, after = ae._critical(feature, old), ae._critical(feature, new)
    if before is not None and after is not None and after > before:
        detail = ""
        try:
            fresh = [f for f in ae.step_checker.check_steps(feature, new) if f.severity == ae.step_checker.CRITICAL]
            old_keys = {(f.kind, f.detail) for f in ae.step_checker.check_steps(feature, old)}
            fresh = [f for f in fresh if (f.kind, f.detail) not in old_keys][:3]
            detail = " (" + "; ".join(f"{f.kind}: {f.detail[:70]}" for f in fresh) + ")" if fresh else ""
        except Exception:  # noqa: BLE001
            pass
        return f"the step checker found {after - before} new problem(s) after the change{detail} - every step the feature uses must stay defined"
    return ""


async def freeform_script_edit(feature: str, script: str, instruction: str, edit_fn, syntax_check=None,
                               max_attempts: int = 3) -> ae.ApplyResult:
    """edit_fn(instruction, context, previous_error) -> raw model text (JSON). All operations of one answer are applied
    together and then checked as a whole (syntax + step checker); if anything fails the whole answer is rejected, the
    exact reason goes back to the model (up to max_attempts) and the script stays untouched until an answer passes."""
    res = ae.ApplyResult(feature, script)
    allow_delete = bool(_DELETE_RE.search(instruction))
    steps = feature_steps(feature)
    err = ""
    failed: list[str] = []
    for _attempt in range(max_attempts):
        ctx = script_context(res.script, instruction)
        ctx["steps"] = steps
        res.model_calls += 1
        try:
            raw = await edit_fn(instruction, ctx, err)
        except Exception as e:  # noqa: BLE001
            failed = [f"the model call failed ({type(e).__name__})"]
            break
        _summary, ops, problem = parse_script_ops(raw)
        if problem:
            failed = [problem]
            err = problem
            if "valid JSON" in problem or "no operations" in problem:
                continue
            break                                         # the model said it cannot do this - do not retry
        failed = []
        working = res.script
        done: list[str] = []
        for op in ops[:MAX_SCRIPT_OPS]:
            try:
                working2, kind, msg = _apply_script_op(working, op, allow_delete)
            except Exception as e:  # noqa: BLE001
                logger.exception("script op crashed")
                working2, kind, msg = working, "skip", f"unexpected problem ({type(e).__name__}: {e})"
            if kind == "ok":
                working = working2
                done.append(msg)
            else:
                failed.append(f'{op.op} "{(op.find or op.text).split(chr(10))[0][:50]}": {msg}')
        if done and working != res.script:
            why = await _script_problem(res.feature, res.script, working, syntax_check)
            if why:
                failed.append(f"the edited script was rejected: {why}")
                done = []
            else:
                res.script = working
                res.applied += [f"script: {m}" for m in done]
        if not failed:
            break
        err = ("These script operations were rejected:\n- " + "\n- ".join(failed)
               + ("\nNo change was applied from your last answer. Fix the problems and answer again with the same JSON format."
                  if not done else
                  "\nThe operations that passed are already applied to the script you now see. Fix ONLY the rejected ones."))
    res.skipped += failed
    return res


# ---------------------------------------------------------------------------
# 9) "what changed" - a compact unified diff of each file for the box
# ---------------------------------------------------------------------------
def make_diff(old: str, new: str, context: int = 2, max_lines: int = 400) -> list[str]:
    a = (old or "").replace("\r\n", "\n").split("\n")
    b = (new or "").replace("\r\n", "\n").split("\n")
    if a == b:
        return []
    out: list[str] = []
    for line in difflib.unified_diff(a, b, lineterm="", n=context):
        if line.startswith("---") or line.startswith("+++"):
            continue
        out.append(line[:300])
        if len(out) >= max_lines:
            out.append("... (more changes are not shown)")
            break
    return out


def diff_payload(before: dict, holder: dict) -> dict:
    """{"feature": [...], "script": [...]} - only the files that really changed."""
    d: dict[str, list[str]] = {}
    f = make_diff(before.get("feature") or "", holder.get("feature") or "")
    s = make_diff(before.get("script") or "", holder.get("script") or "")
    if f:
        d["feature"] = f
    if s:
        d["script"] = s
    return d