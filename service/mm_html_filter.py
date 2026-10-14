from __future__ import annotations

import re

_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}

# One regex that yields either an HTML comment or a (start/end) tag. Quoted
# attribute values may contain ">" (e.g. "a > b"), so they are matched whole.
_TOKEN_RE = re.compile(
    r"(?P<comment><!--.*?-->)"
    r"|<(?P<close>/)?(?P<name>[A-Za-z][\w:-]*)"
    r"(?P<attrs>(?:\"[^\"]*\"|'[^']*'|[^'\">])*)>",
    re.S,
)

_NGIF_RE = re.compile(r"""\*ngIf\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.S)

_STYLE_DISPLAY_RE = re.compile(r"""\[style\.display\]\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.S)
_HIDDEN_RE = re.compile(r"""\[hidden\]\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.S)
_TERNARY_RE = re.compile(r"^(?P<cond>[^?]+?)\s*\?\s*'(?P<a>[^']*)'\s*:\s*'(?P<b>[^']*)'\s*$", re.S)

_EQ_FULL = re.compile(
    r"^\(*\s*MenuRights\s*\??\s*\.\s*BizTransactionClassId\s*===?\s*(-?\d+)\s*\)*$"
)

_PRUNE_MARKER = "<!--[pruned: not shown for this screen]-->"


def _has_negation(e: str) -> bool:
    return re.search(r"!(?!=)", e) is not None


def _certainly_true(expr: str, target: int) -> bool:
    """True only when `expr` is certainly true for ClassId == target."""
    e = expr.strip()
    if ";" in e or "|" in e.replace("||", "") or "?" in e.replace("?.", ""):
        return False
    if _has_negation(e):
        return False
    if "&&" in e and "||" in e:
        return False  # mixed precedence -> not understood
    if "||" in e:
        for term in e.split("||"):
            m = _EQ_FULL.match(term.strip())
            if m and int(m.group(1)) == target:
                return True
        return False
    parts = e.split("&&")
    matches = [_EQ_FULL.match(p.strip()) for p in parts]
    return all(m is not None and int(m.group(1)) == target for m in matches)


def _attr_value(match) -> str:
    return match.group(1) if match.group(1) is not None else match.group(2)


def _element_excluded(attrs: str, target: int) -> bool:
    """True when some attribute proves the element is not visible for `target`."""
    m = _NGIF_RE.search(attrs)
    if m and _excluded_by_class_id(_attr_value(m), target):
        return True

    m = _STYLE_DISPLAY_RE.search(attrs)
    if m:
        value = _attr_value(m).replace("?.", ".").strip()
        t = _TERNARY_RE.match(value)
        if t:
            a, b = t.group("a").strip().lower(), t.group("b").strip().lower()
            if b == "none" and a != "none":      # visible when COND is true
                if _excluded_by_class_id(t.group("cond"), target):
                    return True
            elif a == "none" and b != "none":    # visible when COND is false
                if _certainly_true(t.group("cond"), target):
                    return True

    m = _HIDDEN_RE.search(attrs)
    if m and _certainly_true(_attr_value(m).replace("?.", "."), target):
        return True
    return False


def _excluded_by_class_id(expr: str, target: int) -> bool:
    """True only when `expr` is certainly false for ClassId == target."""
    e = expr.strip()
    # Anything with ternaries, pipes, `; else ...`, negations etc. is not understood -> keep.
    if ";" in e or "|" in e.replace("||", ""):
        return False
    if "?" in e.replace("?.", ""):
        return False
    if _has_negation(e):
        return False

    if "||" in e:
        ids: list[int] = []
        for term in e.split("||"):
            m = _EQ_FULL.match(term.strip())
            if not m:
                return False  # mixed OR expression -> keep
            ids.append(int(m.group(1)))
        return target not in ids

    for conjunct in e.split("&&"):
        m = _EQ_FULL.match(conjunct.strip())
        if m and int(m.group(1)) != target:
            return True  # one false AND-term makes the whole condition false
    return False


def prune_html_for_class_id(html: str, target_class_id: int | None) -> tuple[str, dict]:
    """Remove elements that can never be visible for `target_class_id`.

    Returns (new_html, stats). If target_class_id is None, or an element's
    closing tag cannot be found, the HTML is left as it is.
    """
    stats = {"blocks_pruned": 0, "chars_before": len(html), "chars_after": len(html)}
    if target_class_id is None:
        return html, stats

    tokens = [m for m in _TOKEN_RE.finditer(html)]
    out: list[str] = []
    pos = 0
    i = 0
    while i < len(tokens):
        m = tokens[i]
        if m.group("comment") or m.group("close"):
            i += 1
            continue

        name = m.group("name").lower()
        attrs = m.group("attrs") or ""
        if not _element_excluded(attrs, target_class_id):
            i += 1
            continue

        self_closing = attrs.rstrip().endswith("/") or name in _VOID_TAGS
        if self_closing:
            end = m.end()
            next_i = i + 1
        else:
            depth = 1
            end = None
            next_i = i + 1
            for j in range(i + 1, len(tokens)):
                t = tokens[j]
                if t.group("comment"):
                    continue
                if t.group("name").lower() != name:
                    continue
                if t.group("close"):
                    depth -= 1
                    if depth == 0:
                        end = t.end()
                        next_i = j + 1
                        break
                else:
                    t_attrs = t.group("attrs") or ""
                    if not (t_attrs.rstrip().endswith("/") or name in _VOID_TAGS):
                        depth += 1
            if end is None:
                i += 1  # no matching close tag found -> keep, never guess
                continue

        out.append(html[pos:m.start()])
        out.append(_PRUNE_MARKER)
        pos = end
        stats["blocks_pruned"] += 1
        i = next_i

    out.append(html[pos:])
    result = "".join(out)
    stats["chars_after"] = len(result)
    return result, stats


def squeeze_whitespace(text: str, drop_full_line_slash_comments: bool = False) -> str:
    """Drop indentation, trailing spaces and blank lines (no meaning is lost).
    Optionally drops lines that are ONLY a `//` comment (safe for .ts files;
    lines that merely contain `//` inside code or strings are never touched)."""
    lines = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line:
            continue
        if drop_full_line_slash_comments and line.startswith("//"):
            continue
        lines.append(line)
    return "\n".join(lines)