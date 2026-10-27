"""Extended step vocabulary: which feature steps have no definition yet, and the guarded
assembly of the agent-written definitions into the code-built script (spec 02 part B)."""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from service import step_checker

logger = logging.getLogger(__name__)
INSERT_MARK = "// ---- extended step definitions (agent-written, guarded) ----"

EXTENDED_VOCAB = '''I enter "<Value>" in the "<Field>" new record field    (key picklist of a NEW record: types a new value)
I clear the "<Field>" field
the form should be open / the form should still be open / the form should be closed
the form fields should be empty
the "<Action>" action should be "<visible or hidden>"
the "<Field>" field should show "<Value>"    (Value "(not empty)" only checks that the field is not empty; use it for calculated fields whose rule is unknown)
I click the Reset button
I click the Close button
I open the existing record "<Record>" using the "<Field>" field
I request to delete the record "<Record>" using the "<Field>" field
I request to delete the open record
I should see the delete prompt "<Text>"
I confirm the delete / I cancel the delete
I should still see the record "<Record>" using the "<Field>" field
I should no longer see the record "<Record>" using the "<Field>" field
I search the list for "<Term>" / I clear the list search
I should see the record "<Record>" in the list / I should not see the record "<Record>" in the list
the list row "<Record>" should show "<Text>"
I filter the list by type "<Value>"
I sort the list by the "<Column>" column in "<Order>" order / the list should be sorted by "<Column>" in "<Order>" order
I switch to the "<View>" view / I should see the "<View>" view
I should see the empty state "<Message>"
I remember the number shown for "<Label>" / the number shown for "<Label>" should have changed by "<Delta>"'''

BASE_VOCAB = '''I navigate through the following path "<Module>" -> "<Screen>"
I navigate through the following path "<Module>" -> "<Menu>" -> "<Screen>"
I click the Add New button
I click the save button
I click the Update button
I click the Edit button
I click the Delete button
I select "<Value>" in the "<Field>" picklist field
I enter "<Value>" in the "<Field>" input field
I select "<Value>" in the "<Field>" combobox field
I enter "<Value>" in the "<Field>" date field    (Value: today, today+N or today-N, never a literal date)
I select "<Value>" in the "<Field>" radio field
I set "<Checked or Unchecked>" in the "<Field>" checkbox field
I enter "<Value>" in the "<Field>" textarea field
I should see a confirmation message "<ExpectedResult>"'''


def step_vocabulary() -> str:
    return BASE_VOCAB + "\n" + EXTENDED_VOCAB


_Q = r'"[^"]*"'
_VOCAB_PATTERNS = [re.compile(p) for p in (
    r'I navigate through the following path ' + _Q + r'(?: -> ' + _Q + r'){1,2}',
    r'I click the (?:Add New|save|Update|Edit|Delete|Reset|Close) button',
    r'I (?:select|enter|set) ' + _Q + r' in the ' + _Q + r' (?:picklist|input|combobox|date|radio|checkbox|textarea|new record) field',
    r'I should see a confirmation message ".*"',
    r'I clear the ' + _Q + r' field',
    r'the form should (?:be open|still be open|be closed)',
    r'the form fields should be empty',
    r'the ' + _Q + r' action should be ' + _Q,
    r'the ' + _Q + r' field should show ' + _Q,
    r'I open the existing record ' + _Q + r' using the ' + _Q + r' field',
    r'I request to delete the record ' + _Q + r' using the ' + _Q + r' field',
    r'I request to delete the open record',
    r'I should see the delete prompt ' + _Q,
    r'I (?:confirm|cancel) the delete',
    r'I should (?:still|no longer) see the record ' + _Q + r' using the ' + _Q + r' field',
    r'I search the list for ' + _Q, r'I clear the list search',
    r'I should (?:not )?see the record ' + _Q + r' in the list',
    r'the list row ' + _Q + r' should show ' + _Q,
    r'I filter the list by type ' + _Q,
    r'I sort the list by the ' + _Q + r' column in ' + _Q + r' order',
    r'the list should be sorted by ' + _Q + r' in ' + _Q + r' order',
    r'I switch to the ' + _Q + r' view', r'I should see the ' + _Q + r' view',
    r'I should see the empty state ' + _Q,
    r'I remember the number shown for ' + _Q,
    r'the number shown for ' + _Q + r' should have changed by ' + _Q,
)]


def in_vocabulary(step_text: str) -> bool:
    """True when the sentence is one of the vocabulary sentences (placeholders like <Value> count as text)."""
    t = (step_text or "").strip()
    return any(rx.fullmatch(t) for rx in _VOCAB_PATTERNS)


def split_by_vocabulary(sentences: list[str]) -> tuple[list[str], list[str]]:
    """(in the vocabulary, not in it). Only the first may get a definition: no definition is ever written to make an
    invalid sentence pass - the feature is repaired instead."""
    ok = [s for s in sentences if in_vocabulary(s)]
    return ok, [s for s in sentences if s not in ok]


def undefined_steps(feature_text: str, script_text: str) -> list[str]:
    """Distinct step sentences of the feature (with <Placeholders> kept) that no definition in the script matches."""
    defs = step_checker.parse_step_definitions(script_text)
    seen: dict[str, None] = {}
    for sc in step_checker.parse_feature(feature_text):
        for st in list(getattr(sc, "steps", []) or []) + list(getattr(sc, "background_steps", []) or []):
            text = getattr(st, "text", "") or ""
            probe = re.sub(r"<[^<>]+>", "x", text)
            if text and not any(d.matches(probe) or d.matches(text) for d in defs):
                seen.setdefault(text, None)
    return list(seen)


_FORBIDDEN = (
    (re.compile(r"\blocalStorage\b|\bsessionStorage\b"), "uses browser storage"),
    (re.compile(r"\bcy\.request\b|\bfetch\s*\(|XMLHttpRequest|\bcy\.intercept\b"), "makes network calls"),
    (re.compile(r"\brequire\s*\("), "uses require"),
    (re.compile(r"^\s*import\s", re.M), "has an import"),
    (re.compile(r"\bbeforeEach\s*\("), "redefines beforeEach"),
)


def clean_block(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*[ \t]*\r?\n", "", t)
        t = re.sub(r"\r?\n```\s*$", "", t)
    return t.strip()


def syntax_check(script_text: str) -> tuple[bool | None, str]:
    """True/False from `node --check` on a temp .mjs copy; None when node is not available."""
    node = shutil.which("node")
    if not node:
        return None, "node not available - syntax check skipped"
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "check.mjs"
        p.write_text(script_text, encoding="utf-8")
        r = subprocess.run([node, "--check", str(p)], capture_output=True, text=True, timeout=60)
        return r.returncode == 0, (r.stderr or "").strip()[:600]


def assemble(base: str, block: str, expected_sentences: list[str]) -> tuple[str | None, list[str]]:
    """Inserts the block after the last base definition. Returns (script or None, problems)."""
    block = clean_block(block)
    problems: list[str] = []
    if not block:
        return None, ["the agent returned no code"]
    for rx, why in _FORBIDDEN:
        if rx.search(block):
            problems.append(why)
    assembled = base.rstrip("\n") + "\n\n" + INSERT_MARK + "\n" + block + "\n"
    if not step_checker.qc_template_intact(base, assembled):
        problems.append("the QC template was altered")
    base_defs = step_checker.parse_step_definitions(base)
    new_defs = step_checker.parse_step_definitions(assembled)
    exprs = [d.expression for d in new_defs]
    dups = {e for e in exprs if exprs.count(e) > 1}
    if dups:
        problems.append("duplicate step expressions: " + "; ".join(sorted(dups))[:200])
    added = len(new_defs) - len(base_defs)
    if added < 1:
        problems.append("no new step definition was parsed from the block")
    still = undefined_steps_from_sentences(expected_sentences, assembled)
    if still:
        problems.append("no definition for: " + "; ".join(still[:6]))
    ok, msg = syntax_check(assembled)
    if ok is False:
        problems.append("syntax error: " + msg)
    elif ok is None:
        logger.warning("extended steps: %s", msg)
    return (None if problems else assembled), problems


def undefined_steps_from_sentences(sentences: list[str], script: str) -> list[str]:
    defs = step_checker.parse_step_definitions(script)
    out = []
    for s in sentences:
        probe = re.sub(r"<[^<>]+>", "x", s)
        if not any(d.matches(probe) or d.matches(s) for d in defs):
            out.append(s)
    return out