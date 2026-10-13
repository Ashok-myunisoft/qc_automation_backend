"""Deterministic step checker for a generated Cucumber .feature + step-definition .js.

No AI, no network, standard library only. It answers the questions the AI
validate agent keeps getting wrong:

  undefined_step           a feature step matches no step definition
  ambiguous_step           a feature step matches several definitions, or the
                           same expression is defined twice in the script
  quote_in_cell            an Examples value used in a step contains a double
                           quote (the step text becomes  ""Value""  and matches
                           no {string})
  empty_picklist_value     an empty value reaches a "... picklist field" step
                           (the QC picklist template calls .type(""), which
                           Cypress rejects)
  missing_example_column   a <Placeholder> in a step has no Examples column
  inconsistent_examples_row  an Examples row has a different number of cells
                           than its header (Cucumber refuses to parse the file)

Matching follows cucumber-js: a Cucumber expression must match the WHOLE step
text, and {string} is a double- or single-quoted string with backslash escapes.

Public API
----------
check_steps(feature_text, script_text) -> list[Finding]
findings_to_validation_entries(findings) -> list[dict]   (validate-agent shape)
has_critical(findings) / critical_count(findings)
qc_template_intact(original_script, new_script) -> bool
count_outlines(feature_text) -> int
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, asdict

CRITICAL = "critical"
MAJOR = "major"
MINOR = "minor"

# Findings whose fix belongs in the feature file vs. the script.
FIX_FEATURE = "feature"
FIX_SCRIPT = "script"


@dataclass
class Finding:
    severity: str            # "critical" | "major" | "minor"
    kind: str                # see module docstring
    detail: str              # the step text or Examples row involved
    scenario: str = ""       # scenario / outline title it was found in
    fix_in: str = ""         # "feature" | "script" — which agent should repair it
    hint: str = ""           # short suggestion, used in repair prompts and the UI

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Script side: find Given/When/Then definitions
# ---------------------------------------------------------------------------

def _strip_js_comments(src: str) -> str:
    """Removes // and /* */ comments, leaving string/template literals alone."""
    out: list[str] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c in "\"'`":
            j = i + 1
            while j < n and src[j] != c:
                if src[j] == "\\":
                    j += 1
                elif src[j] == "\n" and c != "`":  # unterminated quote: stop at EOL
                    break
                j += 1
            out.append(src[i:j + 1])
            i = j + 1
        elif src.startswith("//", i):
            j = src.find("\n", i)
            if j == -1:
                break
            i = j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            if j == -1:
                break
            i = j + 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


_JS_SIMPLE_ESCAPES = {"n": "\n", "t": "\t", "r": "\r"}


def _js_unescape(raw: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(raw):
        c = raw[i]
        if c == "\\" and i + 1 < len(raw):
            nxt = raw[i + 1]
            if nxt == "u" and re.fullmatch(r"[0-9a-fA-F]{4}", raw[i + 2:i + 6] or ""):
                out.append(chr(int(raw[i + 2:i + 6], 16)))
                i += 6
                continue
            out.append(_JS_SIMPLE_ESCAPES.get(nxt, nxt))
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


_STEP_CALL_RE = re.compile(r"\b(?:Given|When|Then|defineStep)\s*\(\s*")


def _read_quoted(src: str, pos: int) -> tuple[str, int] | None:
    q = src[pos]
    j = pos + 1
    while j < len(src) and src[j] != q:
        if src[j] == "\\":
            j += 1
        j += 1
    if j >= len(src):
        return None
    return src[pos + 1:j], j + 1


def _read_regex_literal(src: str, pos: int) -> tuple[str, str, int] | None:
    j = pos + 1
    in_class = False
    while j < len(src):
        c = src[j]
        if c == "\\":
            j += 2
            continue
        if c == "[":
            in_class = True
        elif c == "]":
            in_class = False
        elif c == "/" and not in_class:
            break
        elif c == "\n":
            return None
        j += 1
    if j >= len(src):
        return None
    body = src[pos + 1:j]
    k = j + 1
    while k < len(src) and src[k].isalpha():
        k += 1
    return body, src[j + 1:k], k


_PARAM_REGEX = {
    "string": r"""(?:"[^"\\]*(?:\\.[^"\\]*)*"|'[^'\\]*(?:\\.[^'\\]*)*')""",
    "int": r"(?:-?\d+|\d+)",
    "float": r"(?:[+-]?(?:\d+(?:\.\d*)?|\.\d+))",
    "bigdecimal": r"(?:[+-]?(?:\d+(?:\.\d*)?|\.\d+))",
    "word": r"[^\s]+",
    "": r".*",
    "any": r".*",
}


def _token_to_regex(token: str) -> str:
    """One whitespace-free chunk of a Cucumber expression: handles \\x escapes,
    (optional) text and alternative/words."""
    alternatives: list[str] = [""]
    i = 0
    while i < len(token):
        c = token[i]
        if c == "\\" and i + 1 < len(token):
            alternatives[-1] += re.escape(token[i + 1])
            i += 2
        elif c == "/":
            alternatives.append("")
            i += 1
        elif c == "(":
            close = token.find(")", i + 1)
            if close == -1:
                alternatives[-1] += re.escape(c)
                i += 1
            else:
                alternatives[-1] += "(?:" + re.escape(token[i + 1:close]) + ")?"
                i = close + 1
        else:
            alternatives[-1] += re.escape(c)
            i += 1
    if len(alternatives) == 1:
        return alternatives[0]
    return "(?:" + "|".join(alternatives) + ")"


def _literal_to_regex(text: str) -> str:
    parts = re.split(r"(\s+)", text)
    return "".join(re.escape(p) if p.isspace() else _token_to_regex(p) for p in parts if p)


def expression_to_regex(expr: str) -> re.Pattern:
    """Cucumber expression -> compiled regex (use .fullmatch)."""
    pieces = re.split(r"(?<!\\)\{([^{}]*)\}", expr)
    out: list[str] = []
    for idx, piece in enumerate(pieces):
        if idx % 2 == 0:
            out.append(_literal_to_regex(piece))
        else:
            out.append(_PARAM_REGEX.get(piece, r".*"))
    return re.compile("".join(out), re.DOTALL)


@dataclass
class StepDef:
    expression: str          # text as written (or /regex/ source)
    pattern: re.Pattern
    is_regex: bool = False

    def matches(self, text: str) -> bool:
        if self.is_regex:
            return self.pattern.search(text) is not None
        return self.pattern.fullmatch(text) is not None


def parse_step_definitions(script_text: str) -> list[StepDef]:
    src = _strip_js_comments(script_text or "")
    defs: list[StepDef] = []
    for m in _STEP_CALL_RE.finditer(src):
        pos = m.end()
        if pos >= len(src):
            continue
        first = src[pos]
        if first in "\"'`":
            read = _read_quoted(src, pos)
            if not read:
                continue
            raw, _ = read
            if first == "`" and "${" in raw:
                continue  # cannot be resolved statically
            expr = _js_unescape(raw)
            try:
                defs.append(StepDef(expr, expression_to_regex(expr)))
            except re.error:
                continue
        elif first == "/":
            read = _read_regex_literal(src, pos)
            if not read:
                continue
            body, flags, _ = read
            try:
                defs.append(StepDef(
                    f"/{body}/{flags}",
                    re.compile(body, re.IGNORECASE if "i" in flags else 0),
                    is_regex=True,
                ))
            except re.error:
                continue
    return defs


# ---------------------------------------------------------------------------
# Feature side: scenarios, steps, Examples tables
# ---------------------------------------------------------------------------

_KEYWORD_RE = re.compile(r"^(?:Given|When|Then|And|But|\*)\s+", re.IGNORECASE)
_STEP_LINE_RE = re.compile(r"^(?:Given|When|Then|And|But|\*)\s+\S", re.IGNORECASE)
_OUTLINE_RE = re.compile(r"^Scenario\s+(?:Outline|Template)\s*:\s*(.*)$", re.IGNORECASE)
_SCENARIO_RE = re.compile(r"^(?:Scenario|Example)\s*:\s*(.*)$", re.IGNORECASE)
_BACKGROUND_RE = re.compile(r"^Background\s*:", re.IGNORECASE)
_EXAMPLES_RE = re.compile(r"^(?:Examples|Scenarios)\s*:", re.IGNORECASE)
_RULE_RE = re.compile(r"^Rule\s*:", re.IGNORECASE)
_FEATURE_RE = re.compile(r"^Feature\s*:", re.IGNORECASE)
_PLACEHOLDER_RE = re.compile(r"<([^<>]+)>")
_PICKLIST_RE = re.compile(r"\bpicklist\s+field\b", re.IGNORECASE)


@dataclass
class _Step:
    text: str
    line: int


@dataclass
class _ExamplesTable:
    header: list[str]
    rows: list[list[str]]
    row_lines: list[int]


@dataclass
class _Scenario:
    name: str
    line: int
    is_outline: bool
    is_background: bool
    steps: list[_Step]
    tables: list[_ExamplesTable]


def _split_cells(line: str) -> list[str]:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|") and not body.endswith("\\|"):
        body = body[:-1]
    cells, cur, i = [], [], 0
    while i < len(body):
        c = body[i]
        if c == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            cur.append({"n": "\n", "|": "|", "\\": "\\"}.get(nxt, c + nxt))
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


def parse_feature(feature_text: str) -> list[_Scenario]:
    scenarios: list[_Scenario] = []
    current: _Scenario | None = None
    table: _ExamplesTable | None = None
    in_examples = False
    in_docstring: str | None = None

    for lineno, raw in enumerate((feature_text or "").splitlines(), start=1):
        line = raw.strip()
        if in_docstring:
            if line.startswith(in_docstring):
                in_docstring = None
            continue
        if line.startswith('"""') or line.startswith("```"):
            in_docstring = line[:3]
            continue
        if not line or line.startswith("#") or line.startswith("@"):
            continue

        if _FEATURE_RE.match(line) or _RULE_RE.match(line):
            current, table, in_examples = None, None, False
            continue
        if _BACKGROUND_RE.match(line):
            current = _Scenario("Background", lineno, False, True, [], [])
            scenarios.append(current)
            table, in_examples = None, False
            continue
        m = _OUTLINE_RE.match(line)
        if m:
            current = _Scenario(m.group(1).strip(), lineno, True, False, [], [])
            scenarios.append(current)
            table, in_examples = None, False
            continue
        m = _SCENARIO_RE.match(line)
        if m:
            current = _Scenario(m.group(1).strip(), lineno, False, False, [], [])
            scenarios.append(current)
            table, in_examples = None, False
            continue
        if _EXAMPLES_RE.match(line):
            in_examples, table = True, None
            continue
        if line.startswith("|"):
            if in_examples and current is not None:
                cells = _split_cells(line)
                if table is None:
                    table = _ExamplesTable(cells, [], [])
                    current.tables.append(table)
                else:
                    table.rows.append(cells)
                    table.row_lines.append(lineno)
            continue  # a step's own data table: ignored
        if current is not None and _STEP_LINE_RE.match(line):
            in_examples = False
            current.steps.append(_Step(_KEYWORD_RE.sub("", line, count=1).strip(), lineno))
    return scenarios


def count_outlines(feature_text: str) -> int:
    return sum(1 for s in parse_feature(feature_text) if s.is_outline)


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------

def _normalise_for_similarity(text: str) -> str:
    text = re.sub(r'"[^"]*"|\'[^\']*\'', '"x"', text)
    text = re.sub(r"<[^<>]+>", '"x"', text)
    return re.sub(r"\s+", " ", text).strip()


def _definition_sample(expr: str) -> str:
    return re.sub(r"\{[^{}]*\}", '"x"', expr)


def _closest_definition(step_text: str, defs: list[StepDef]) -> tuple[StepDef | None, float, bool]:
    """Returns (closest def, similarity 0..1, differs_only_in_case)."""
    target = _normalise_for_similarity(step_text)
    best: StepDef | None = None
    best_ratio = 0.0
    only_case = False
    for d in defs:
        if d.is_regex:
            continue
        cand = _normalise_for_similarity(_definition_sample(d.expression))
        ratio = difflib.SequenceMatcher(None, target.lower(), cand.lower()).ratio()
        if ratio > best_ratio:
            best, best_ratio = d, ratio
            only_case = target != cand and target.lower() == cand.lower()
    return best, best_ratio, only_case


class _Collector:
    """Collects findings, merging repeats of the same defect."""

    def __init__(self) -> None:
        self._items: dict[tuple, Finding] = {}
        self._counts: dict[tuple, int] = {}

    def add(self, key: tuple, finding: Finding) -> None:
        if key in self._items:
            self._counts[key] += 1
            return
        self._items[key] = finding
        self._counts[key] = 1

    def result(self) -> list[Finding]:
        out = []
        for key, f in self._items.items():
            extra = self._counts[key] - 1
            if extra > 0:
                f.detail += f"  (+{extra} more occurrence(s))"
            out.append(f)
        order = {CRITICAL: 0, MAJOR: 1, MINOR: 2}
        out.sort(key=lambda f: order.get(f.severity, 3))
        return out


def check_steps(feature_text: str, script_text: str) -> list[Finding]:
    col = _Collector()
    defs = parse_step_definitions(script_text)
    scenarios = parse_feature(feature_text)

    if not defs:
        if any(s.steps for s in scenarios):
            return [Finding(
                MAJOR, "no_step_definitions",
                "no Given/When/Then step definitions could be read from the script",
                fix_in=FIX_SCRIPT,
                hint="step matching was skipped because no definitions were found",
            )]
        return []

    # Same expression defined more than once -> Cucumber "Ambiguous step".
    by_expr: dict[str, int] = {}
    for d in defs:
        by_expr[d.expression] = by_expr.get(d.expression, 0) + 1
    duplicate_exprs = {e for e, n in by_expr.items() if n > 1}
    for expr in sorted(duplicate_exprs):
        col.add(("dup", expr), Finding(
            CRITICAL, "ambiguous_step",
            f'step expression defined {by_expr[expr]} times in the script: "{expr}"',
            fix_in=FIX_SCRIPT,
            hint="keep exactly one definition of this expression",
        ))

    def check_text(sc: _Scenario, template: str, text: str) -> None:
        matched = [d for d in defs if d.matches(text)]
        if not matched:
            # Only a pure letter-case / spacing difference is safe to fix in the
            # feature. A different word ("Update" vs "Delete") is a different
            # action: never reword it into another step - the script needs a
            # definition for it instead.
            closest, ratio, only_case = _closest_definition(text, defs)
            if closest is not None and only_case:
                hint = (f'closest defined step is "{closest.expression}" (differs only in letter case)'
                        " - reword the feature step to match it exactly")
                fix_in = FIX_FEATURE
            else:
                hint = ("no step definition matches this text - add one whose expression matches it exactly "
                        "(do not reword the step into a different action)")
                fix_in = FIX_SCRIPT
            col.add(("undef", template), Finding(
                CRITICAL, "undefined_step", text, scenario=sc.name, fix_in=fix_in, hint=hint,
            ))
            return
        if len(matched) > 1 and len({d.expression for d in matched}) > 1:
            exprs = " | ".join(sorted({d.expression for d in matched}))
            col.add(("ambig", template), Finding(
                CRITICAL, "ambiguous_step", text, scenario=sc.name, fix_in=FIX_SCRIPT,
                hint=f"matches several different definitions: {exprs}",
            ))
        # several copies of one identical expression were already reported as a duplicate

    for sc in scenarios:
        tables = sc.tables
        header_names: set[str] = set()
        for t in tables:
            header_names.update(t.header)

        # Examples tables with ragged rows break the whole file in Cucumber.
        for t in tables:
            for row, ln in zip(t.rows, t.row_lines):
                if len(row) != len(t.header):
                    col.add(("ragged", sc.name, ln), Finding(
                        CRITICAL, "inconsistent_examples_row",
                        f"line {ln}: {len(row)} cell(s) but the header has {len(t.header)}: | " + " | ".join(row) + " |",
                        scenario=sc.name, fix_in=FIX_FEATURE,
                        hint="every Examples row needs the same number of cells as its header",
                    ))

        for step in sc.steps:
            placeholders = _PLACEHOLDER_RE.findall(step.text)
            is_picklist = bool(_PICKLIST_RE.search(step.text))

            if sc.is_outline and placeholders:
                for ph in dict.fromkeys(placeholders):
                    if ph not in header_names:
                        where = "no Examples table" if not tables else "Examples columns: " + ", ".join(sorted(header_names))
                        col.add(("nocol", sc.name, ph), Finding(
                            MAJOR, "missing_example_column",
                            f'<{ph}> used in step "{step.text}" has no matching Examples column ({where})',
                            scenario=sc.name, fix_in=FIX_FEATURE,
                            hint=f'add a "{ph}" column to the Examples table or fix the placeholder name',
                        ))

            rows: list[dict[str, str] | None] = []
            if sc.is_outline and tables and placeholders:
                for t in tables:
                    for r_idx, row in enumerate(t.rows, start=1):
                        if len(row) != len(t.header):
                            continue  # already reported as ragged
                        rows.append(dict(zip(t.header, row)) | {"__row__": str(r_idx)})
            else:
                rows.append(None)  # step has nothing to substitute

            if sc.is_outline and placeholders and not rows and tables:
                continue  # Examples with no data rows: nothing runs

            for row in rows:
                text = step.text
                tainted = False
                if row is not None:
                    for ph in dict.fromkeys(placeholders):
                        if ph not in row:
                            continue
                        val = row[ph]
                        if '"' in val:
                            tainted = True
                            col.add(("quote", sc.name, ph, row["__row__"]), Finding(
                                CRITICAL, "quote_in_cell",
                                f'Examples row {row["__row__"]}, column "{ph}" = {val!r} '
                                f'-> step becomes: {step.text.replace("<" + ph + ">", val)}',
                                scenario=sc.name, fix_in=FIX_FEATURE,
                                hint="Examples values must not contain double quotes - remove them",
                            ))
                        if is_picklist and val.strip() == "":
                            col.add(("emptypick", sc.name, ph, row["__row__"]), Finding(
                                CRITICAL, "empty_picklist_value",
                                f'Examples row {row["__row__"]}, column "{ph}" is empty but is used in: {step.text}',
                                scenario=sc.name, fix_in=FIX_FEATURE,
                                hint=f'the picklist step cannot take an empty value - move this row to a NEW '
                                     f'Scenario Outline with the same steps but WITHOUT the line "{step.text}", '
                                     f'and drop the "{ph}" column from that new Examples table',
                            ))
                    text = _PLACEHOLDER_RE.sub(
                        lambda m: row.get(m.group(1), m.group(0)) if row is not None else m.group(0), step.text)
                elif is_picklist and re.search(r'"\s*"', step.text):
                    col.add(("emptypick-lit", sc.name, step.text), Finding(
                        CRITICAL, "empty_picklist_value", step.text, scenario=sc.name, fix_in=FIX_FEATURE,
                        hint="the picklist step cannot take an empty value",
                    ))
                if tainted:
                    continue  # the quote is the cause; do not also report the step as undefined
                check_text(sc, step.text, text)

    return col.result()


# ---------------------------------------------------------------------------
# Helpers for the pipeline
# ---------------------------------------------------------------------------

def has_critical(findings: list[Finding]) -> bool:
    return any(f.severity == CRITICAL for f in findings)


def critical_count(findings: list[Finding]) -> int:
    return sum(1 for f in findings if f.severity == CRITICAL)


def findings_to_validation_entries(findings: list[Finding]) -> list[dict]:
    """Same shape as the validate agent's "findings" items, plus the extra
    kind/detail fields, so the UI can show them alongside the AI findings."""
    return [
        {
            "severity": f.severity,
            "area": "step_checker",
            "kind": f.kind,
            "scenario_or_step": f.scenario or "file-level",
            "description": f"[{f.kind}] {f.detail}",
            "recommendation": f.hint,
            "detail": f.detail,
            "source": "code",
        }
        for f in findings
    ]


def format_for_prompt(findings: list[Finding]) -> str:
    lines = []
    for i, f in enumerate(findings, start=1):
        where = f' (scenario: "{f.scenario}")' if f.scenario else ""
        lines.append(f"{i}. [{f.severity}] {f.kind}{where}: {f.detail}\n   fix: {f.hint}")
    return "\n".join(lines)


# --- QC template guard -------------------------------------------------------
# The QC team's template steps must survive a repair untouched. This is a
# read-only comparison used to REJECT a bad repair; it never edits the script.

_TEMPLATE_EXPRESSIONS = (
    "I select {string} in the {string} picklist field",
    "I enter {string} in the {string} input field",
    "I should see a confirmation message {string}",
    "I click the Delete button",
)


def _balanced_end(src: str, open_pos: int, open_ch: str, close_ch: str) -> int:
    depth, i, n = 0, open_pos, len(src)
    while i < n:
        c = src[i]
        if c in "\"'`":
            read = _read_quoted(src, i)
            if not read:
                return -1
            i = read[1]
            continue
        if c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _norm(code: str) -> str:
    return re.sub(r"[\s;]+", "", code)


def _template_blocks(script: str) -> dict[str, list[str]]:
    src = _strip_js_comments(script or "")
    blocks: dict[str, list[str]] = {}
    for m in _STEP_CALL_RE.finditer(src):
        pos = m.end()
        if pos < len(src) and src[pos] in "\"'":
            read = _read_quoted(src, pos)
            if read and _js_unescape(read[0]) in _TEMPLATE_EXPRESSIONS:
                end = _balanced_end(src, src.rfind("(", 0, pos), "(", ")")
                if end != -1:
                    blocks.setdefault(_js_unescape(read[0]), []).append(_norm(src[m.start():end + 1]))
    for name, pat in (("beforeEach", r"\bbeforeEach\s*\("), ("loginSession", r"\bfunction\s+loginSession\s*\(")):
        for m in re.finditer(pat, src):
            if name == "beforeEach":
                end = _balanced_end(src, m.end() - 1, "(", ")")
            else:
                brace = src.find("{", m.end())
                end = _balanced_end(src, brace, "{", "}") if brace != -1 else -1
            if end != -1:
                blocks.setdefault(name, []).append(_norm(src[m.start():end + 1]))
    creds = re.search(r"const\s+creds\s*=[^\n;]*", src)
    if creds:
        blocks["creds"] = [_norm(creds.group(0))]
    return blocks


def qc_template_intact(original_script: str, new_script: str) -> bool:
    """True when the QC template survived a repair.

    The template is the FIRST occurrence of each template item in the original
    script (it is written at the top, verbatim). For every item that existed
    originally, the repaired script must still start with that exact first
    occurrence, and must not contain any variant that was not in the original.
    Removing a later duplicate copy is allowed - that is the repair."""
    before = _template_blocks(original_script)
    after = _template_blocks(new_script)
    for key, bodies in before.items():
        kept = after.get(key)
        if not kept or kept[0] != bodies[0]:
            return False
        if any(b not in bodies for b in kept) or len(kept) > len(bodies):
            return False
    return True