"""Calculated / read-only field detection and date helpers - code only, no AI.

A calculated field is a field the APP fills (Days from the From and To dates, a total, a balance, a status). Test data
must never be typed into it; scenarios only CHECK it. It is found in three places:

  formjson : ReadOnly true / Type "ReadOnly" / Disabled / IsDisabled  (already read into field["readonly"])
  HTML     : readonly / disabled attribute, a read-only binding ([readonly]="true", [disabled]="isReadOnly" ...)
  TS       : a control that is assigned (setValue / patchValue) inside the change handler of ANOTHER field
             (valueChanges.subscribe, (dateChange) / (change) / (ngModelChange) ... handlers and the methods they call)

Also here: the date format the screen shows (read from the screen's own code) and the "today / today+N / today-N"
helpers used by the feature repair.

Every public function is best-effort: on any problem it returns an empty result and the caller carries on.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import date, datetime, timedelta

logger = logging.getLogger(__name__)

_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_TS_COMMENT_RE = re.compile(r"/\*.*?\*/|(?<![:\"'`\\])//[^\n]*", re.S)
_TAG_RE = re.compile(r"<([A-Za-z][\w-]*)((?:\s(?:[^<>\"']|\"[^\"]*\"|'[^']*')*)?)/?>", re.S)
_FCN_RE = re.compile(r"""(?<![\w\[])formControlName\s*=\s*["']([\w.]+)["']""", re.I)
_DATE_ATTR_RE = re.compile(r"matDatepicker|\bdatepicker\b|type\s*=\s*[\"']date[\"']|\bgbdate\b|\bgb-date\b", re.I)
_NUMBER_RE = re.compile(r"^-?\d+$")
# one attribute: name and (optional) quoted value - values are consumed, so a class named "readonly-box" is not an attribute
_ATTR_PAIR_RE = re.compile(r"""([\[\(\*#]*[\w.:-]+[\]\)]*)\s*(?:=\s*(?:"([^"]*)"|'([^']*)'))?""")
_RO_BINDING_NAME_RE = re.compile(r"\[(?:attr\.)?(?:readonly|disabled|isreadonly|isdisabled)\]")
# a binding that only switches on the record state (new / existing) is NOT a calculated field
_STATE_SWITCH_RE = re.compile(r"edit|update|new|add|exist|mode|create|saved|record|isid|\bid\b|selected|open", re.I)


def _n(text) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def _lit_true(expr: str) -> bool | None:
    e = (expr or "").strip().strip("'\"").lower()
    if e in ("", "true", "1", "readonly", "disabled", "yes"):
        return True
    if e in ("false", "0", "no", "null", "undefined"):
        return False
    return None


# --------------------------------------------------------------------------- HTML
def _is_date_tag(low: str) -> bool:
    """gb-date, mat-datepicker, app-date-picker ... (but not 'app-update-form' / 'validate')."""
    return any(t in ("date", "datepicker", "datetime", "daterange", "gbdate", "datefield") or t.startswith("datepicker")
               for t in re.split(r"[-_]", low))


def _attr_pairs(attrs: str):
    for m in _ATTR_PAIR_RE.finditer(attrs or ""):
        yield m.group(1), (m.group(2) if m.group(2) is not None else m.group(3))


def html_flags(html_text: str) -> dict:
    """{name_lower: {"readonly": bool, "conditional": expr | None, "date": bool}} for every formControlName."""
    out: dict[str, dict] = {}
    html = _HTML_COMMENT_RE.sub("", html_text or "")
    opens: list[tuple[str, int]] = []
    for m in _TAG_RE.finditer(html):
        tag, attrs = m.group(1), m.group(2) or ""
        low = tag.lower()
        fcn = _FCN_RE.search(attrs)
        if not fcn:
            if _is_date_tag(low):
                opens.append((low, m.start()))
                opens = opens[-6:]
            continue
        name = fcn.group(1).lower()
        info = out.setdefault(name, {"readonly": False, "conditional": None, "date": False})
        # the control itself, or the date component it sits in
        is_date = _is_date_tag(low) or bool(_DATE_ATTR_RE.search(attrs))
        if not is_date:
            for dtag, pos in reversed(opens):
                if m.start() - pos < 700 and f"</{dtag}" not in html[pos:m.start()]:
                    is_date = True
                    break
        info["date"] = info["date"] or is_date
        for an, av in _attr_pairs(attrs):
            low_an = an.lower()
            if low_an in ("readonly", "disabled", "isreadonly"):
                if av is None or _lit_true(av) is True:
                    info["readonly"] = True
            elif _RO_BINDING_NAME_RE.fullmatch(low_an) and av is not None:
                expr = av.strip()
                plain = expr.strip("'\"").lower()
                if plain in ("true", "1"):
                    info["readonly"] = True
                elif plain in ("false", "0", "null", "undefined", ""):
                    continue
                elif _STATE_SWITCH_RE.search(expr):
                    info["conditional"] = expr          # record-state switch (new / existing): informational only
                else:
                    info["readonly"] = True             # a plain read-only / computed flag
                    info["conditional"] = expr
    return out


# --------------------------------------------------------------------------- TS helpers
def _strip_ts(text: str) -> str:
    return _TS_COMMENT_RE.sub("", text or "")


def _match_close(text: str, i: int, open_ch: str, close_ch: str) -> int:
    """Index of the bracket that closes the one at text[i] (quotes are skipped); len(text) when unbalanced."""
    depth, k, quote = 0, i, ""
    while k < len(text):
        c = text[k]
        if quote:
            if c == "\\":
                k += 1
            elif c == quote:
                quote = ""
        elif c in "'\"`":
            quote = c
        elif c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return k
        k += 1
    return len(text)


def _split_top(text: str) -> list[str]:
    """Comma split at depth 0."""
    parts, depth, cur, quote = [], 0, [], ""
    for c in text:
        if quote:
            cur.append(c)
            if c == quote:
                quote = ""
            continue
        if c in "'\"`":
            quote = c
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        if c == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(c)
    if "".join(cur).strip():
        parts.append("".join(cur))
    return parts


_KEYWORDS = {"if", "for", "while", "switch", "catch", "function", "return", "else", "do", "try", "new", "await", "super"}
_METHOD_RE = re.compile(
    r"^[ \t]*(?:(?:public|private|protected|async|static|get|set)\s+)*([A-Za-z_$][\w$]*)\s*\(([^()]*(?:\([^()]*\)[^()]*)*)\)\s*(?::\s*[\w<>\[\]|.,? ]+)?\s*\{",
    re.M)


_ARROW_RE = re.compile(
    r"^[ \t]*(?:(?:public|private|protected|readonly|static)\s+)*([A-Za-z_$][\w$]*)\s*(?::[^=\n]+)?=\s*(?:async\s*)?\([^)]*\)\s*(?::\s*[\w<>\[\]|. ]+)?\s*=>\s*\{",
    re.M)


def _methods(ts: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for rx in (_METHOD_RE, _ARROW_RE):
        for m in rx.finditer(ts):
            name = m.group(1)
            if name in _KEYWORDS:
                continue
            i = m.end() - 1
            j = _match_close(ts, i, "{", "}")
            out.setdefault(name, ts[i:j + 1])
    return out


_CONTROL_REF_RES = (
    re.compile(r"""\.get\(\s*['"]([\w.]+)['"]\s*\)"""),
    re.compile(r"""\.controls\s*\[\s*['"](\w+)['"]\s*\]"""),
    re.compile(r"""\.controls\.(\w+)"""),
)


def _control_before(text: str, end: int) -> str | None:
    """The control a `.valueChanges` / `.setValue` call at text[end] belongs to (the last reference before it)."""
    seg = text[max(0, end - 160):end]
    best, pos = None, -1
    for rx in _CONTROL_REF_RES:
        for m in rx.finditer(seg):
            if m.start() > pos:
                best, pos = m.group(1), m.start()
    return best


_EVENT_RE = re.compile(
    r"""\(\s*(dateChange|dateInput|change|ngModelChange|selectionChange|valueChange|onChange|onSelect|selectedValue|"""
    r"""optionSelected|blur|focusout|input|keyup|onDateChange|onValueChange|onBlur|onSelectionChange|onselect)\s*\)\s*=\s*"([^"]*)\"""",
    re.I)


def _html_handlers(html_text: str) -> list[tuple[str, str]]:
    """[(trigger control name | '*', method name)] for change-like events in the template."""
    out = []
    html = _HTML_COMMENT_RE.sub("", html_text or "")
    for m in _TAG_RE.finditer(html):
        attrs = m.group(2) or ""
        fcn = _FCN_RE.search(attrs)
        trigger = fcn.group(1) if fcn else "*"
        for em in _EVENT_RE.finditer(attrs):
            call = re.search(r"(?:this\.)?([A-Za-z_$][\w$]*)\s*\(", em.group(2))
            if call:
                out.append((trigger, call.group(1)))
    return out


def _ts_regions(ts: str, html_text: str) -> list[dict]:
    """Change handlers: [{"trigger": control | '*', "body": text}]."""
    methods = _methods(ts)
    regions: list[dict] = []
    for m in re.finditer(r"\.valueChanges\b", ts):
        sub = ts.find(".subscribe(", m.end())
        if sub < 0 or sub - m.end() > 400:
            continue
        i = ts.find("(", sub)
        j = _match_close(ts, i, "(", ")")
        pipe_part = ts[m.end():sub]
        trigger = _control_before(ts, m.start()) or "*"
        regions.append({"trigger": trigger, "body": pipe_part + ts[i:j + 1]})
    for trigger, fn in _html_handlers(html_text):
        if fn in methods:
            regions.append({"trigger": trigger, "body": methods[fn]})

    # a handler usually calls another method of the same component (calculateDays(), updateTotals() ...)
    def expand(body: str, depth: int, seen: set[str]) -> str:
        if depth <= 0:
            return body
        extra = []
        for call in set(re.findall(r"this\.([A-Za-z_$][\w$]*)\s*\(", body)):
            if call in methods and call not in seen:
                seen.add(call)
                extra.append(expand(methods[call], depth - 1, seen))
        return body + "\n" + "\n".join(extra)

    for r in regions:
        r["body"] = expand(r["body"], 2, set())
    return regions


_RESET_EXPR = {"null", "''", '""', "undefined", "[]", "{}", "``"}
_SET_NAMED_RES = (
    re.compile(r"""\.get\(\s*['"]([\w.]+)['"]\s*\)\s*[?!]?\s*\.(?:setValue|patchValue)\s*\("""),
    re.compile(r"""\.controls\s*\[\s*['"](\w+)['"]\s*\]\s*[?!]?\s*\.(?:setValue|patchValue)\s*\("""),
    re.compile(r"""\.controls\.(\w+)\s*[?!]?\s*\.(?:setValue|patchValue)\s*\("""),
)
_SET_OBJECT_RE = re.compile(r"\.(?:patchValue|setValue)\s*\(\s*\{")


def _assignments(body: str) -> list[tuple[str, str]]:
    """[(control name, value expression)] for setValue / patchValue calls inside one handler body."""
    out: list[tuple[str, str]] = []
    for rx in _SET_NAMED_RES:
        for m in rx.finditer(body):
            i = m.end() - 1
            j = _match_close(body, i, "(", ")")
            args = _split_top(body[i + 1:j])
            out.append((m.group(1), args[0].strip() if args else ""))
    for m in _SET_OBJECT_RE.finditer(body):
        i = m.end() - 1
        j = _match_close(body, i, "{", "}")
        for entry in _split_top(body[i + 1:j]):
            if ":" in entry:
                k, v = entry.split(":", 1)
                k = k.strip().strip("'\"")
                if re.fullmatch(r"[\w$]+", k):
                    out.append((k, v.strip()))
    return out


def _days_rule(expr: str, body: str, sources: list[str]) -> dict | None:
    """{"kind": "days_between", "from_field", "to_field", "inclusive"} when the expression is a day difference."""
    text = expr
    ident = re.fullmatch(r"(?:this\.)?([A-Za-z_$][\w$]*)", expr.strip())
    if ident:
        d = re.search(r"(?:let|const|var|this\.)?\s*%s\s*=\s*([^;]+);" % re.escape(ident.group(1)), body)
        if d:
            text = d.group(1)
    is_days = bool(re.search(r"getTime\s*\(\)|differenceInDays|differenceInCalendarDays|\.diff\s*\(|86400000|"
                             r"1000\s*\*\s*60\s*\*\s*60\s*\*\s*24|duration\s*\(", text + " " + body))
    if not is_days or len(sources) < 2:
        return None
    inclusive = bool(re.search(r"\+\s*1\b", text)) or bool(re.search(r"\+\s*1\b", body)) and "days" in body.lower()
    # which source is the start and which the end
    def role(name: str) -> int:
        s = name.lower()
        if re.search(r"from|start|begin|\bin\b|checkin", s):
            return 0
        if re.search(r"\bto\b|to[A-Z]|end|till|upto|until|out|checkout", name, re.I) or s.endswith("to"):
            return 1
        return -1
    roles = {n: role(n) for n in sources}
    starts = [n for n in sources if roles[n] == 0]
    ends = [n for n in sources if roles[n] == 1]
    if starts and ends:
        return {"kind": "days_between", "from_field": starts[0], "to_field": ends[0], "inclusive": inclusive}
    # fall back on the order in the expression: (end - start)
    order = sorted(sources, key=lambda n: (text.lower().find(n.lower()) if n.lower() in text.lower() else 10 ** 6))
    if len(order) >= 2 and text.lower().find(order[0].lower()) >= 0 and text.lower().find(order[1].lower()) >= 0:
        return {"kind": "days_between", "from_field": order[1], "to_field": order[0], "inclusive": inclusive}
    return None


def ts_calculated(ts_text: str, html_text: str, field_names: list[str], date_names: set[str] | None = None) -> dict[str, dict]:
    """{name_lower: {"computed_from": [names], "rule": dict | None}} for controls a change handler of another
    control assigns (and does not just reset)."""
    ts = _strip_ts(ts_text)
    by_low = {n.lower(): n for n in field_names}
    found: dict[str, dict] = {}
    for region in _ts_regions(ts, html_text):
        body, trigger = region["body"], str(region["trigger"] or "*")
        for target, expr in _assignments(body):
            tl = target.lower()
            if tl not in by_low or expr.strip().lower() in _RESET_EXPR or tl == trigger.lower():
                continue
            names = []
            if trigger != "*" and trigger.lower() in by_low:
                names.append(by_low[trigger.lower()])
            for low, real in by_low.items():
                if low == tl or real in names:
                    continue
                if re.search(r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % re.escape(real), body, re.I):
                    names.append(real)
            names = names[:4]
            rule = None
            try:
                srcs = [n for n in names if date_names is None or n.lower() in date_names]
                rule = _days_rule(expr, body, srcs)
            except Exception:  # noqa: BLE001
                rule = None
            cur = found.get(tl)
            if cur is None or (rule and not cur.get("rule")):
                found[tl] = {"computed_from": names, "rule": rule}
    return found


# --------------------------------------------------------------------------- apply to the spec fields
def apply(fields: list[dict], rendered: dict, source_files: list[dict], primary_dir: str = "") -> dict:
    """Marks field["calculated"] (+ readonly) from formjson / HTML / TS. Returns {"calculated": [...], "date_format": ..}."""
    own = [f for f in source_files or [] if not str(f.get("path", "")).startswith("[backend]")
           and (not primary_dir or str(f.get("path", "")).startswith(primary_dir + "/"))]
    html_text = "\n".join(f.get("content") or "" for f in own if str(f.get("path", "")).endswith(".html"))
    ts_text = "\n".join(f.get("content") or "" for f in own if str(f.get("path", "")).endswith(".ts")
                        and not str(f.get("path", "")).endswith((".spec.ts", ".module.ts")))
    flags = {}
    ts_found = {}
    try:
        flags = html_flags(html_text)
    except Exception:  # noqa: BLE001
        logger.exception("calculated fields: HTML read failed - formjson only")
    try:
        names = [f["name"] for f in fields if f.get("candidate") or f.get("active")]
        date_names = {f["name"].lower() for f in fields if f.get("kind") == "date" or (flags.get(f["name"].lower()) or {}).get("date")}
        ts_found = ts_calculated(ts_text, html_text, names, date_names)
    except Exception:  # noqa: BLE001
        logger.exception("calculated fields: TS read failed - formjson / HTML only")
    summary = []
    for f in fields:
        low = f["name"].lower()
        origins = []
        if f.get("readonly"):
            origins.append("formjson")
        h = flags.get(low) or {}
        if h.get("date") and f.get("kind") in ("input", ""):
            f["kind"] = "date"
        if h.get("readonly"):
            origins.append("html")
        elif h.get("conditional"):
            f["readonly_conditional"] = h["conditional"]
        t = ts_found.get(low)
        if t:
            origins.append("ts")
        f["calculated"] = False
        if origins and not (f.get("key_field") or f.get("record_picker")) and f.get("kind") != "grid":
            f["calculated"] = True
            f["readonly"] = True
            f["calculated_origin"] = origins
            f["computed_from"] = list((t or {}).get("computed_from") or [])
            f["calculated_rule"] = (t or {}).get("rule")
            summary.append({"name": f["name"], "label": f.get("label") or f["name"], "origin": origins,
                            "computed_from": f["computed_from"], "rule": f["calculated_rule"]})
    fmt, src = date_format(ts_text, html_text)
    return {"calculated": summary, "date_format": fmt, "date_format_source": src}


# --------------------------------------------------------------------------- date format
_DATE_FMT_RES = (
    re.compile(r"""\|\s*date\s*:\s*['"]([^'"]+)['"]"""),
    re.compile(r"""dateInput\s*:\s*['"]([^'"]+)['"]"""),
    re.compile(r"""(?:dateFormat|displayFormat|DateFormat|dateformat|DisplayFormat|format)\s*[:=]\s*['"]([^'"]+)['"]"""),
    re.compile(r"""\.format\(\s*['"]([^'"]+)['"]\s*\)"""),
    re.compile(r"""\.transform\([^,()]+(?:\([^()]*\))?\s*,\s*['"]([^'"]+)['"]\s*\)"""),
    re.compile(r"""formatDate\(\s*[^,()]+(?:\([^()]*\))?\s*,\s*['"]([^'"]+)['"]"""),
    re.compile(r"""\[?format\]?\s*=\s*"'?([^"']+)'?\""""),
    re.compile(r"""\[?dateFormat\]?\s*=\s*"'?([^"']+)'?\""""),
)
_DATE_FMT_OK = re.compile(r"^[dDMmyY/\-. ,]+$")


def date_format(ts_text: str, html_text: str) -> tuple[str, str]:
    """('dd/MMM/yyyy', 'ts') - the date format the screen's own code uses ('' when it is not written there)."""
    votes: Counter = Counter()
    for label, text in (("ts", _strip_ts(ts_text)), ("html", _HTML_COMMENT_RE.sub("", html_text or ""))):
        for rx in _DATE_FMT_RES:
            for m in rx.finditer(text):
                raw = m.group(1).strip()
                # keep the date part of 'dd/MMM/yyyy HH:mm'
                parts = [p for p in raw.split(" ") if p and not re.search(r"[Hhsa]", p)]
                cand = " ".join(parts).strip()
                low = cand.lower()
                if cand and _DATE_FMT_OK.match(cand) and "y" in low and "d" in low and "m" in low:
                    votes[(cand, label)] += 1
    if not votes:
        return "", ""
    (fmt, label), _n_ = votes.most_common(1)[0]
    return fmt, label


def _py_format(fmt: str) -> str:
    def sub(m):
        t = m.group(0)
        return {"yyyy": "%Y", "YYYY": "%Y", "yy": "%y", "YY": "%y", "MMMM": "%B", "MMM": "%b", "MM": "%m", "M": "%m",
                "dd": "%d", "DD": "%d", "d": "%d", "D": "%d"}.get(t, t)
    return re.sub(r"yyyy|YYYY|yy|YY|MMMM|MMM|MM|M|dd|DD|d|D", sub, fmt)


def parse_relative(value: str) -> int | None:
    """'today' -> 0, 'today+3' -> 3, 'Today - 2 days' -> -2; None when it is not a relative date."""
    m = re.fullmatch(r"\s*today\s*(?:([+-])\s*(\d+)\s*(?:days?)?)?\s*", str(value or ""), re.I)
    if not m:
        return None
    return 0 if not m.group(1) else (int(m.group(2)) * (1 if m.group(1) == "+" else -1))


def to_relative(value: str, fmt: str = "", today: date | None = None) -> str | None:
    """A literal date in the screen's format (or a common one) -> 'today+N' / 'today-N' / 'today'; None if unparseable."""
    v = str(value or "").strip()
    if not v:
        return None
    rel = parse_relative(v)
    if rel is not None:
        return format_relative(rel)
    formats = ([_py_format(fmt)] if fmt else []) + ["%d/%b/%Y", "%d-%b-%Y", "%d %b %Y", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y",
                                                   "%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%d/%B/%Y", "%d %B %Y"]
    for f in formats:
        try:
            d = datetime.strptime(v, f).date()
        except ValueError:
            continue
        return format_relative((d - (today or date.today())).days)
    return None


def format_relative(n: int) -> str:
    return "today" if n == 0 else (f"today+{n}" if n > 0 else f"today-{abs(n)}")


def days_between(from_value: str, to_value: str, inclusive: bool) -> int | None:
    a, b = parse_relative(from_value), parse_relative(to_value)
    if a is None or b is None:
        return None
    n = b - a + (1 if inclusive else 0)
    return n