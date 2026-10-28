"""Reads the user's free-text request and splits it into typed items.

Item types
  FULL_SCENARIO      pasted Gherkin (inserted near-verbatim, converted to Scenario Outline + Examples)
  DESCRIBED_SCENARIO "add a negative amount scenario" / "I need a test case for ..." (written by the model)
  TEST_DATA          "use SD-050 for DomainCode", "change email from a@x.com to b@x.com"
  PRIORITY_HINT      "focus on validation" (only steers generation)
  COMMENT            "add a comment ..." -> a '#' line in the feature and a '//' line in the script.
                     Never an executable step, never a command; always inserted by code.

Detection is deterministic first (Gherkin keywords, "change X to Y", "comment ...").
The model is asked only for pieces that stay ambiguous, and only when a model_fn is given.
An unclear piece is reported with its own text (see Plan.unclear_messages) - never a bare error.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field, asdict

logger = logging.getLogger(__name__)

FULL_SCENARIO = "FULL_SCENARIO"
DESCRIBED_SCENARIO = "DESCRIBED_SCENARIO"
TEST_DATA = "TEST_DATA"
PRIORITY_HINT = "PRIORITY_HINT"
COMMENT = "COMMENT"
ALL_TYPES = (FULL_SCENARIO, DESCRIBED_SCENARIO, TEST_DATA, PRIORITY_HINT, COMMENT)


@dataclass
class Item:
    type: str
    raw: str
    value: str = ""        # FULL: the Gherkin | DESCRIBED: the description | TEST_DATA: new value | COMMENT: the text
    field: str = ""        # TEST_DATA: field / column name
    old_value: str = ""    # TEST_DATA: value to replace, when the user named it
    target: str = ""       # scenario title fragment the item is about (TEST_DATA / COMMENT)
    unclear: str = ""      # set when the piece could not be classified

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Plan:
    items: list[Item] = field(default_factory=list)

    def of(self, *types: str) -> list[Item]:
        return [i for i in self.items if i.type in types and not i.unclear]

    @property
    def unclear(self) -> list[Item]:
        return [i for i in self.items if i.unclear]

    @property
    def actionable(self) -> list[Item]:
        """Everything the editor can apply to a feature file (priority hints only steer generation)."""
        return self.of(FULL_SCENARIO, DESCRIBED_SCENARIO, TEST_DATA, COMMENT)

    def unclear_messages(self) -> list[str]:
        return [f'Could not tell what this part of your request means: "{i.raw.strip()[:160]}" - {i.unclear}. '
                f'Write it as a scenario, a data change ("use X for Field") or a comment ("add a comment ...").'
                for i in self.unclear]

    def priority_text(self, default: str) -> str:
        """The part of the request that may travel through every tab / pass call: priority hints only
        (and any unclear piece, which is harmless as a hint). Hard requirements never go in here."""
        hints = [i.raw.strip() for i in self.items if i.type == PRIORITY_HINT or i.unclear]
        if not hints:
            return default
        return f"{default} Priorities from the user: " + " ".join(hints)

    def requirements_text(self) -> str:
        """The hard requirements block for the test case prompt (added ONCE per screen)."""
        lines: list[str] = []
        n = 0
        for i in self.of(FULL_SCENARIO):
            n += 1
            lines.append(f"{n}. SCENARIO TO INCLUDE - write exactly this scenario, converted to a Scenario Outline with an "
                         f"aligned Examples table and the fixed step vocabulary (keep its wording and values):\n{i.value.strip()}")
        for i in self.of(DESCRIBED_SCENARIO):
            n += 1
            lines.append(f"{n}. SCENARIO TO INCLUDE - the user asked for: \"{i.value.strip()}\". Write it as its own Scenario "
                         f"Outline (or as extra Examples rows of an existing outline when it is only one more data case) and "
                         f"put the user's key words in its title.")
        for i in self.of(TEST_DATA):
            n += 1
            scope = f" in the scenario about \"{i.target}\"" if i.target else " in the Examples rows that fill that field with a normal value"
            old = f" (instead of \"{i.old_value}\")" if i.old_value else ""
            lines.append(f"{n}. TEST DATA - use the value \"{i.value}\"{old} for the field \"{i.field}\"{scope}.")
        if not lines:
            return ""
        return ("HARD REQUIREMENTS FROM THE USER (mandatory - each one must appear in the output exactly once):\n"
                + "\n".join(lines)
                + "\nA requirement may use ONLY fields that exist in the Project Analysis. If a requirement names a field, "
                  "page or rule that does not exist there, skip that requirement - never invent it.")

    def to_dict(self) -> dict:
        return {"items": [i.to_dict() for i in self.items]}

    def summary(self) -> str:
        counts: dict[str, int] = {}
        for i in self.items:
            key = "UNCLEAR" if i.unclear else i.type
            counts[key] = counts.get(key, 0) + 1
        return ", ".join(f"{v} {k.lower()}" for k, v in counts.items()) or "nothing"


# ---------------------------------------------------------------------------
# 1) split the text: Gherkin blocks first, then prose pieces
# ---------------------------------------------------------------------------
_OPEN_RE = re.compile(
    r"^\s*(?:Feature\s*:|Background\s*:|Rule\s*:|Scenario(?:\s+Outline|\s+Template)?\s*:|Example\s*:|@\S+|"
    r"(?:Given|When|Then)\s+(?:I|the|a|an)\b)", re.I)
_CONT_RE = re.compile(
    r"^\s*(?:(?:Given|When|Then|And|But)\s+\S|\|.*\||Examples?\s*:|Scenarios\s*:|@\S+|#.*|\"\"\".*|```.*|"
    r"Feature\s*:|Background\s*:|Rule\s*:|Scenario(?:\s+Outline|\s+Template)?\s*:)", re.I)


def split_gherkin(text: str) -> tuple[list[str], str]:
    """(gherkin blocks, remaining prose)."""
    lines = (text or "").replace("\r\n", "\n").split("\n")
    blocks: list[str] = []
    prose: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        if _OPEN_RE.match(lines[i]):
            j = i
            last_good = i
            while j < n:
                line = lines[j]
                if not line.strip():
                    j += 1
                    continue
                if _CONT_RE.match(line):
                    last_good = j
                    j += 1
                    continue
                break
            block = "\n".join(lines[i:last_good + 1]).strip("\n")
            if re.search(r"^\s*(?:Scenario|Example\s*:|(?:Given|When|Then)\s)", block, re.I | re.M):
                blocks.append(block)
            else:  # only tags / a header: not a scenario
                prose.extend(lines[i:last_good + 1])
            i = last_good + 1
        else:
            prose.append(lines[i])
            i += 1
    return blocks, "\n".join(prose)


_BULLET_RE = re.compile(r"^\s*(?:[-*•]+|\d+[.)])\s+")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Za-z])")
_JOIN_RE = re.compile(r"\s+(?:and\s+also|and\s+then|also|plus)\s+|,\s*(?:and\s+)?(?=(?:add|change|use|set|update|comment|focus|make|include|put|insert|replace)\b)|"
                      r"\s+and\s+(?=(?:add|change|use|set|update|put|insert|include|replace)\s)", re.I)


def split_prose(prose: str) -> list[str]:
    pieces: list[str] = []
    for line in (prose or "").split("\n"):
        line = _BULLET_RE.sub("", line).strip()
        if not line:
            continue
        for part in line.split(";"):
            for sent in _SENTENCE_RE.split(part.strip()):
                for p in _JOIN_RE.split(sent):
                    p = (p or "").strip()
                    if p:
                        pieces.append(p)
    return pieces


# ---------------------------------------------------------------------------
# 2) classify one prose piece
# ---------------------------------------------------------------------------
def _unq(s: str) -> str:
    s = (s or "").strip()
    s = s.rstrip(" ,;")
    if len(s) > 1 and s[-1] == "." and not re.search(r"\.\w", s[-3:]):  # sentence full stop, not part of a value
        s = s[:-1]
    s = s.strip()
    if len(s) >= 2 and s[0] in "\"'`" and s[-1] == s[0]:
        s = s[1:-1]
    return s.strip()


def _clean_field(s: str) -> str:
    s = _unq(s)
    s = re.sub(r"\s+(?:field|column|value|input|data)$", "", s, flags=re.I)
    s = re.sub(r"^(?:the|a|an)\s+", "", s, flags=re.I)
    return _unq(s)


_TARGET_RE = re.compile(
    r"\s*\b(?:above|before|on|for|in|under|at|inside|within)\s+(?:the\s+)?[\"']?"
    r"(?P<t>(?:(?!\b(?:in|on|for|above|before|under|at|inside|within)\b)[^\"'])+?)[\"']?\s+"
    r"(?:scenario|scenarios|test\s?case|outline)\b", re.I)


def _last_target(text: str):
    found = None
    for m in _TARGET_RE.finditer(text):
        found = m
    return found


_SCEN_RE = re.compile(r"\b(?:scenarios?|test[\s-]?cases?|testcases?|use[\s-]?cases?)\b", re.I)
_COMMENT_WORD_RE = re.compile(r"\bcomments?\b(?!\s+(?:field|box|column|input|textarea|text\s?area|section))", re.I)
_ADD_VERB_RE = re.compile(r"^(?:please\s+)?(?:also\s+)?(?:can\s+you\s+|could\s+you\s+)?(?:add|include|cover|verify|validate|check|test|ensure|create|write|need|want)\b", re.I)
_GENERAL_RE = re.compile(r"^(?:please\s+)?generate\b|\b(?:this|the|whole|entire|full)\s+screen\b", re.I)
_PRIORITY_RE = re.compile(r"\b(?:focus|prioriti[sz]e|emphasi[sz]e|concentrate|more\s+(?:tests|scenarios|coverage)|mainly|especially|first|order|importance|important|critical)\b", re.I)

_DATA_RES = (
    re.compile(r"^(?:please\s+)?(?:change|update|set|replace|modify|edit|make)\s+(?:the\s+)?(?:value\s+of\s+)?(?:input\s+)?"
               r"(?P<field>[^=]+?)\s*(?:(?:value|data)\s+)?(?:from\s+(?P<old>.+?)\s+)?(?:to|as|=)\s+(?P<new>.+)$", re.I),
    re.compile(r"^(?:please\s+)?use\s+(?P<new>.+?)\s+(?:for|in|as|on)\s+(?:the\s+)?(?P<field>.+?)(?:\s+(?:field|column|value))?$", re.I),
    re.compile(r"^(?:please\s+)?(?:enter|put|type|give|supply|provide)\s+(?P<new>.+?)\s+(?:in|into|for|as)\s+(?:the\s+)?"
               r"(?P<field>.+?)(?:\s+(?:field|column))?$", re.I),
    re.compile(r"^(?P<field>[A-Za-z][\w .\-]{0,40}?)\s*(?:=|:)\s*(?P<new>\S.*)$"),
)


def _classify_piece(piece: str) -> Item:
    raw = piece.strip()
    text = re.sub(r"^(?:(?:also|and|then|please|plus)\s+)+", "", raw, flags=re.I)

    # a line that starts with # or // is a comment
    m = re.match(r"^(?:#|//)\s*(.+)$", text)
    if m:
        return Item(COMMENT, raw, value=m.group(1).strip())

    target = ""
    tm = _last_target(text)

    # ---- COMMENT: "add a comment ... " (never a step, never a command) ----
    if _COMMENT_WORD_RE.search(text) and (re.match(r"^(?:please\s+)?(?:also\s+)?(?:add|put|insert|write|leave|include|place|comment)\b", text, re.I)
                                           or re.match(r"^comments?\b", text, re.I)):
        body = text
        if tm:
            target = tm.group("t").strip()
            body = text[:tm.start()] + text[tm.end():]
        q = re.search(r"[\"'“](.+?)[\"'”]", body)
        if q:
            ctext = q.group(1).strip()
        else:
            cm = re.search(r"\bcomments?\b(?:\s+line)?(?:\s+(?:saying|that\s+says|which\s+says|as|with\s+text|like|reading|about))?\s*[:\-]?\s*(.*)$",
                           body, re.I)
            ctext = _unq(cm.group(1)) if cm else ""
            ctext = re.sub(r"^(?:that|saying|says)\s+", "", ctext, flags=re.I)
        if not ctext:
            return Item(COMMENT, raw, unclear="no text was given for the comment line")
        return Item(COMMENT, raw, value=ctext.replace("\n", " ").strip(), target=target)

    body = text
    if tm:
        target = tm.group("t").strip()
        body = (text[:tm.start()] + text[tm.end():]).strip()

    # ---- DESCRIBED_SCENARIO: names a scenario / test case ----
    if _SCEN_RE.search(body):
        # "change amount to 5 in the create scenario" was cut above, so a remaining scenario word means "add one"
        desc = re.sub(r"^(?:please\s+)?(?:also\s+)?(?:can\s+you\s+|could\s+you\s+)?", "", raw, flags=re.I).strip()
        return Item(DESCRIBED_SCENARIO, raw, value=desc)

    # ---- TEST_DATA ----
    if not _ADD_VERB_RE.match(body) or re.match(r"^(?:please\s+)?(?:enter|put|type|give|supply|provide)\b", body, re.I):
        for rx in _DATA_RES:
            dm = rx.match(body)
            if not dm:
                continue
            fld, new = _clean_field(dm.group("field")), _unq(dm.group("new"))
            old = _unq(dm.groupdict().get("old") or "")
            if fld and new and len(fld.split()) <= 5:
                return Item(TEST_DATA, raw, value=new, field=fld, old_value=old, target=target)

    # ---- a general sentence such as "Generate tests for this screen" is only context for generation ----
    if _GENERAL_RE.search(body):
        return Item(PRIORITY_HINT, raw)

    # ---- add / verify / test ... without the word "scenario" ----
    if _ADD_VERB_RE.match(body):
        desc = re.sub(r"^(?:please\s+)?(?:also\s+)?", "", raw, flags=re.I).strip()
        return Item(DESCRIBED_SCENARIO, raw, value=desc)

    if _PRIORITY_RE.search(body):
        return Item(PRIORITY_HINT, raw)

    return Item(PRIORITY_HINT, raw, unclear="it is not clearly a scenario, a test-data change or a comment")


def parse(text: str) -> Plan:
    """Deterministic parse. Pieces that stay unclear carry .unclear (see classify() for the model fallback)."""
    plan = Plan()
    blocks, prose = split_gherkin(text or "")
    for b in blocks:
        plan.items.append(Item(FULL_SCENARIO, b, value=b))
    for piece in split_prose(prose):
        plan.items.append(_classify_piece(piece))
    return plan


# ---------------------------------------------------------------------------
# 3) model fallback, only for what stays unclear
# ---------------------------------------------------------------------------
_CACHE: dict[str, Plan] = {}


def _fallback_prompt(pieces: list[str]) -> str:
    return (
        "A QA engineer typed short requests about the test files of ONE screen. For each numbered request decide its type:\n"
        "  DESCRIBED_SCENARIO = asks for a new scenario / test case (value = the request, short)\n"
        "  TEST_DATA = change or supply one value (field = the field name, value = the new value, old_value = the value it replaces if named, "
        "target = the scenario it is about if named)\n"
        "  COMMENT = wants a comment line added (value = the comment text, target = the scenario if named)\n"
        "  PRIORITY_HINT = only an emphasis for generation, nothing to add\n"
        "  UNCLEAR = cannot tell (reason = what is unclear)\n"
        "Return ONLY a JSON array: [{\"index\":0,\"type\":\"...\",\"value\":\"\",\"field\":\"\",\"old_value\":\"\",\"target\":\"\",\"reason\":\"\"}]\n\n"
        + "\n".join(f"{i}. {p}" for i, p in enumerate(pieces)))


async def classify(text: str, model_fn=None) -> Plan:
    """parse(), then ONE model call for every still-unclear piece (when model_fn is given).
    model_fn: async (prompt: str) -> str. Any model problem leaves the piece unclear; nothing raises."""
    key = (text or "").strip()
    if key in _CACHE:
        return _CACHE[key]
    plan = parse(key)
    bad = [(idx, it) for idx, it in enumerate(plan.items) if it.unclear and it.type != COMMENT]
    if bad and model_fn is not None:
        try:
            reply = await model_fn(_fallback_prompt([it.raw for _, it in bad]))
            m = re.search(r"\[.*\]", reply or "", re.S)
            data = json.loads(m.group(0)) if m else []
            for row in data if isinstance(data, list) else []:
                if not isinstance(row, dict) or not isinstance(row.get("index"), int) or not 0 <= row["index"] < len(bad):
                    continue
                pos, old = bad[row["index"]]
                t = str(row.get("type") or "").upper()
                if t == "UNCLEAR":
                    old.unclear = str(row.get("reason") or old.unclear)[:160]
                    continue
                if t not in ALL_TYPES or t == FULL_SCENARIO:
                    continue
                new = Item(t, old.raw, value=str(row.get("value") or ""), field=str(row.get("field") or ""),
                           old_value=str(row.get("old_value") or ""), target=str(row.get("target") or ""))
                if t == DESCRIBED_SCENARIO and not new.value:
                    new.value = old.raw
                if t == TEST_DATA and not (new.field and new.value):
                    old.unclear = "the field or the new value is missing"
                    continue
                if t == COMMENT and not new.value:
                    old.unclear = "no text was given for the comment line"
                    continue
                plan.items[pos] = new
        except Exception:  # noqa: BLE001
            logger.warning("request classifier: model fallback failed - unclear pieces stay unclear", exc_info=True)
    if len(_CACHE) > 200:
        _CACHE.clear()
    _CACHE[key] = plan
    return plan