"""Reads what a screen DOES from its own Angular source (component .ts + template .html), by CODE (no AI).

The screen spec (service/screen_spec.py) knows the FIELDS (formjson). This module adds the BEHAVIOUR the
scenario matrix (service/scenario_matrix.py) needs to write real if / else scenarios:

  buttons    every (click) handler with its label and the condition it is shown under
             (Delete only while a record is open, Save <-> Update, Reset, Close, row edit / delete ...)
  dialogs    every dialog.open(...) with its message and its buttons (Yes / No ...)
  branches   what the Save / Delete handlers do when the server answers Status 200 and what they do not do
             otherwise (modal stays open, no dialog ...)
  list       search box, filter dropdown, sortable columns, list / card toggle, counters, empty state,
             which fields the search looks at, how a status / type value is displayed
  modal      the element the form opens in

Everything is best-effort: extract() never raises; a screen without a list / modal simply gets empty
sections and the matrix skips the scenarios that need them.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_TAG_STRIP_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _squash(text: str) -> str:
    text = re.sub(r"&#?\w+;", " ", text or "")          # &#9650; and friends (sort arrows)
    return _WS_RE.sub(" ", text).strip()


def _attr(attrs: str, name: str) -> str:
    m = re.search(r"""(?<![\w\[(*.-])%s\s*=\s*("([^"]*)"|'([^']*)')""" % re.escape(name), attrs)
    return (m.group(2) if m and m.group(2) is not None else (m.group(3) if m else "")) or ""


def _first_class(cls: str, preferred: tuple[str, ...] = ()) -> str:
    parts = cls.split()
    for p in preferred:
        if p in parts:
            return p
    return parts[0] if parts else ""


# --------------------------------------------------------------------------- html
_CLICK_TAG_RE = re.compile(r"<([A-Za-z][\w-]*)((?:\s[^<>]*?)?\(click\)\s*=\s*\"[^\"]*\"[^<>]*)>", re.S)


def _inner_text(html: str, start: int, tag: str) -> str:
    """Visible text of the element that starts at `start` (end of its opening tag); icon names are dropped."""
    close = re.compile(r"</%s\s*>" % re.escape(tag), re.I)
    m = close.search(html, start)
    chunk = html[start:m.start()] if m else html[start:start + 300]
    chunk = re.sub(r"<mat-icon[^>]*>.*?</mat-icon>", " ", chunk, flags=re.S | re.I)
    chunk = _TAG_STRIP_RE.sub(" ", chunk)
    chunk = re.sub(r"\{\{.*?\}\}", " ", chunk, flags=re.S)
    return _squash(chunk)


def _ternary_labels(expr: str) -> tuple[str, str, str]:
    """isEditMode() ? 'Update' : 'Save' -> ('isEditMode()', 'Update', 'Save')"""
    m = re.match(r"\s*(.+?)\s*\?\s*['\"]([^'\"]+)['\"]\s*:\s*['\"]([^'\"]+)['\"]\s*$", expr or "")
    return (m.group(1), m.group(2), m.group(3)) if m else ("", "", "")


def _buttons_from_html(html: str) -> list[dict]:
    out: list[dict] = []
    for tag, attrs in _CLICK_TAG_RE.findall(html):
        click = re.search(r"\(click\)\s*=\s*\"([^\"]*)\"", attrs)
        if not click:
            continue
        handler = click.group(1).strip()
        start = html.find(attrs) + len(attrs) + 1
        label = _attr(attrs, "title") or _attr(attrs, "aria-label")
        cond_expr, label_true, label_false = _ternary_labels(_attr(attrs, "[title]"))
        text = _inner_text(html, start, tag)
        # Save / Update: the visible label is a ternary in the inner text or the [title] binding
        if not label and not text and label_true:
            label = label_false
        cls = _attr(attrs, "class")
        ngif = _attr(attrs, "*ngIf")
        item = {
            "handler": handler,
            "method": re.match(r"\s*(\w+)", handler).group(1) if re.match(r"\s*(\w+)", handler) else "",
            "tag": tag,
            "class": cls,
            "title": label,
            "text": text,
            "shown_when": ngif,
            "in_row": "item" in handler and "(" in handler,
        }
        if label_true:
            item["title_when_true"] = label_true
            item["title_when_false"] = label_false
            item["title_condition"] = cond_expr
        out.append(item)
    return out


def _list_from_html(html: str) -> dict:
    lst: dict = {}
    # search box
    m = re.search(r"<input\b([^<>]*?(?:search|Search)[^<>]*)>", html)
    if m:
        attrs = m.group(1)
        lst["search"] = {
            "class": _first_class(_attr(attrs, "class")),
            "placeholder": _attr(attrs, "placeholder"),
            "model": (re.search(r"\[\(ngModel\)\]\s*=\s*\"([^\"]+)\"", attrs) or [None, ""])[1],
        }
    # filter dropdown
    m = re.search(r"<select\b([^<>]*)>(.*?)</select>", html, re.S)
    if m:
        attrs, body = m.group(1), m.group(2)
        first_opt = re.search(r"<option[^>]*>\s*([^<{]+?)\s*</option>", body)
        lst["filter"] = {
            "class": _first_class(_attr(attrs, "class")),
            "model": (re.search(r"\[\(ngModel\)\]\s*=\s*\"([^\"]+)\"", attrs) or [None, ""])[1],
            "all_option": first_opt.group(1) if first_opt else "",
        }
    # sortable columns: <th ... (click)="sortBy('Col')"> Label ...
    cols = []
    for m in re.finditer(r"<th\b([^<>]*)\(click\)\s*=\s*\"\s*sortBy\(\s*['\"](\w+)['\"]\s*\)\s*\"([^<>]*)>(.*?)</th>", html, re.S):
        label = _squash(re.sub(r"\{\{.*?\}\}", " ", _TAG_STRIP_RE.sub(" ", re.sub(r"<span[^>]*sort-indicator.*?</span>\s*</span>", " ", m.group(4), flags=re.S))))
        label = re.sub(r"[▲▼]", "", label).strip()
        cols.append({"field": m.group(2), "label": label,
                     "class": _first_class(_attr(m.group(1) + m.group(3), "class"), ("sortable-col",))})
    if cols:
        lst["sortable_columns"] = cols
    # table headers in order (to find a column's index)
    heads = re.search(r"<thead>(.*?)</thead>", html, re.S)
    if heads:
        labels = []
        for th in re.findall(r"<th\b[^<>]*>(.*?)</th>", heads.group(1), re.S):
            t = _squash(re.sub(r"\{\{.*?\}\}", " ", _TAG_STRIP_RE.sub(" ", th)))
            labels.append(re.sub(r"[▲▼]", "", t).strip())
        lst["table_headers"] = labels
    # list / card toggle buttons
    views = []
    for m in re.finditer(r"<button\b([^<>]*)\(click\)\s*=\s*\"\s*viewMode\s*=\s*['\"](\w+)['\"]\s*\"([^<>]*)>", html):
        views.append({"mode": m.group(2), "title": _attr(m.group(1) + m.group(3), "title"),
                      "class": _first_class(_attr(m.group(1) + m.group(3), "class"), ("toggle-btn",))})
    if views:
        lst["views"] = views
    if re.search(r"\*ngIf\s*=\s*\"[^\"]*viewMode\s*===?\s*'list'", html):
        m = re.search(r"<div\b[^<>]*class\s*=\s*\"([^\"]*)\"[^<>]*\*ngIf\s*=\s*\"[^\"]*viewMode\s*===?\s*'list'", html)
        if m:
            lst["list_container"] = _first_class(m.group(1))
    m = re.search(r"<div\b[^<>]*class\s*=\s*\"([^\"]*)\"[^<>]*\*ngIf\s*=\s*\"[^\"]*viewMode\s*===?\s*'card'", html)
    if m:
        lst["card_container"] = _first_class(m.group(1))
    # counters
    stats = []
    for m in re.finditer(r"<span[^>]*class\s*=\s*\"[^\"]*stat-label[^\"]*\"[^>]*>\s*([^<]+?)\s*</span>", html):
        stats.append(m.group(1).strip())
    if stats:
        lst["counters"] = stats
        lst["counter_label_class"] = "stat-label"
        lst["counter_number_class"] = "stat-number"
    # empty state
    m = re.search(r"<div\b[^<>]*class\s*=\s*\"([^\"]*empty-state[^\"]*)\"[^<>]*>(.*?)</div>", html, re.S)
    if m:
        h = re.search(r"<h\d[^>]*>\s*([^<]+?)\s*</h\d>", m.group(2))
        lst["empty_state"] = {"class": "empty-state", "message": h.group(1) if h else ""}
    # how a status is shown: item.IsActive === 0 ? 'ACTIVE' : 'INACTIVE'
    m = re.search(r"\{\{\s*item\.(\w+)\s*===?\s*(\d+)\s*\?\s*'([^']+)'\s*:\s*'([^']+)'\s*\}\}", html)
    if m:
        lst["status"] = {"field": m.group(1), "value_for_first": int(m.group(2)),
                         "first_text": m.group(3), "else_text": m.group(4)}
    # per-row actions
    row_actions = [b for b in _buttons_from_html(html) if b["in_row"]]
    if row_actions:
        seen, uniq = set(), []
        for b in row_actions:
            key = (b["method"], b["title"])
            if key not in seen:
                seen.add(key)
                uniq.append({"method": b["method"], "title": b["title"], "class": b["class"]})
        lst["row_actions"] = uniq
    return lst


def _modal_from_html(html: str) -> dict:
    m = re.search(r"<div\b[^<>]*#(\w+)[^<>]*class\s*=\s*\"([^\"]*\bmodal\b[^\"]*)\"", html) \
        or re.search(r"<div\b[^<>]*class\s*=\s*\"([^\"]*\bmodal\b[^\"]*)\"[^<>]*#(\w+)", html)
    if not m:
        return {}
    cls = m.group(2) if m.lastindex == 2 and " " not in m.group(1) and "#" not in m.group(1) and m.group(1).isidentifier() else m.group(2)
    first = _first_class(m.group(2) if m.group(2) else "", ("modal",))
    return {"selector": "." + (first or "modal"), "ref": m.group(1)}


# --------------------------------------------------------------------------- typescript
def _balanced(text: str, open_pos: int, open_ch: str = "(", close_ch: str = ")") -> int:
    depth = 0
    for i in range(open_pos, len(text)):
        c = text[i]
        if c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return i
    return len(text) - 1


def _method_bodies(ts: str) -> dict[str, str]:
    """{method name: body} for 'public foo(...)' style methods (good enough for components)."""
    out: dict[str, str] = {}
    for m in re.finditer(r"(?:public|private|protected)?\s*(?:async\s+)?(\w+)\s*\([^)]*\)\s*(?::\s*[\w<>\[\]|\s]+)?\s*\{", ts):
        name = m.group(1)
        if name in ("if", "for", "while", "switch", "catch", "constructor", "function"):
            continue
        end = _balanced(ts, m.end() - 1, "{", "}")
        out[name] = ts[m.end():end]
    return out


def _lit(chunk: str, key: str) -> str:
    m = re.search(r"\b%s\s*:\s*(`[^`]*`|'[^']*'|\"[^\"]*\")" % re.escape(key), chunk)
    return m.group(1)[1:-1] if m else ""


def _dialogs_from_ts(ts: str, bodies: dict[str, str]) -> list[dict]:
    out = []
    for method, body in bodies.items():
        for m in re.finditer(r"dialog\.open\s*\(", body):
            end = _balanced(body, m.end() - 1)
            chunk = body[m.end():end]
            msg = _lit(chunk, "message")
            if not msg and "responseValue" in chunk:
                msg = "(server message)"
            out.append({
                "method": method,
                "heading": _lit(chunk, "heading"),
                "message": msg,
                "fragment": msg.split("${")[0].replace('"', "").strip(),
                "button1": _lit(chunk, "button1"),
                "button2": _lit(chunk, "button2"),
                "is_confirm": bool(_lit(chunk, "button1")),
            })
    return out


def _branches_from_ts(bodies: dict[str, str]) -> list[dict]:
    out = []
    for method, body in bodies.items():
        m = re.search(r"if\s*\(\s*[\w?.]*Status\s*===?\s*200\s*\)\s*\{", body)
        if not m:
            continue
        end = _balanced(body, m.end() - 1, "{", "}")
        ok = body[m.end():end]
        rest = body[end + 1:end + 40]
        has_else = bool(re.match(r"\s*else\b", rest))
        service = re.search(r"service\.(form(?:save|delete|load)service)", body)
        out.append({
            "method": method,
            "service_call": service.group(1) if service else "",
            "on_200": {
                "opens_dialog": "dialog.open" in ok,
                "closes_modal": bool(re.search(r"style\.display\s*=\s*['\"]none['\"]", ok)),
                "reloads_list": bool(re.search(r"load\w*List\w*\s*\(", ok)),
                "resets_form": bool(re.search(r"reset\w*\s*\(", ok, re.I)),
            },
            "otherwise": "else branch" if has_else else "nothing happens (no dialog, modal stays open, list unchanged)",
            "has_else": has_else,
        })
    return out


def _switch_map(ts: str) -> dict[str, str]:
    """A function that maps a numeric key to display text:  case 0: return 'Manufacturing';"""
    best: dict[str, str] = {}
    for m in re.finditer(r"switch\s*\([^)]*\)\s*\{", ts):
        end = _balanced(ts, m.end() - 1, "{", "}")
        pairs = re.findall(r"case\s+(-?\d+)\s*:\s*return\s*['\"]([^'\"]+)['\"]", ts[m.end():end])
        if len(pairs) > len(best):
            best = {k: v for k, v in pairs}
    return best


def _search_fields(ts: str) -> list[str]:
    fields = re.findall(r"item\.(\w+)\?\.toLowerCase\(\)\.includes\(\s*search\s*\)", ts)
    return list(dict.fromkeys(fields))


# --------------------------------------------------------------------------- public
def _pick(source_files: list[dict], primary_dir: str, suffix: str) -> str:
    texts = []
    for f in source_files or []:
        path = str(f.get("path", ""))
        if not path.endswith(suffix) or path.endswith(".spec.ts") or path.startswith("[backend]"):
            continue
        if primary_dir and not path.startswith(primary_dir + "/"):
            continue
        texts.append(f.get("content") or "")
    return "\n".join(texts)


def extract_from_texts(ts: str, html: str) -> dict:
    html = _COMMENT_RE.sub("", html or "")
    ts = ts or ""
    bodies = _method_bodies(ts)
    buttons = _buttons_from_html(html)
    facts = {
        "found": bool(ts.strip() or html.strip()),
        "buttons": [b for b in buttons if not b["in_row"]],
        "dialogs": _dialogs_from_ts(ts, bodies),
        "branches": _branches_from_ts(bodies),
        "list": _list_from_html(html),
        "modal": _modal_from_html(html),
        "type_text": _switch_map(ts),
        "search_fields": _search_fields(ts),
    }
    lst = facts["list"]
    lst["sort_ranks"] = {}          # {field name: {displayed text: rank}} - how the list orders a coded column
    status = lst.get("status")
    if status:
        lst["sort_ranks"][status["field"]] = {status["first_text"]: 0, status["else_text"]: 1}
    m = re.search(r"\{\{\s*\w+\(\s*item\.(\w+)\s*\)\s*\}\}", _COMMENT_RE.sub("", html or ""))
    if m and facts["type_text"]:
        lst["type_field"] = m.group(1)
        lst["sort_ranks"][m.group(1)] = {v: int(k) for k, v in facts["type_text"].items()}
    # convenience: which handler is which
    roles: dict[str, dict] = {}
    for b in facts["buttons"]:
        hay = f"{b['method']} {b['title']} {b['text']} {b['class']}".lower()
        low_method = b["method"].lower()
        if low_method == "save" or "save" in b["class"].split():
            roles["save"] = b
        elif low_method in ("delete",) or "delete" in b["class"].split():
            roles["delete"] = b
        elif low_method == "reset":
            roles["reset"] = b
        elif low_method in ("close", "cancel"):
            roles["close"] = b
        elif low_method in ("createnew", "addnew", "add", "new") or "btn-add" in b["class"].split():
            roles.setdefault("new", b)
    facts["roles"] = {k: {"method": v["method"], "title": v["title"], "text": v["text"], "class": v["class"],
                          "shown_when": v["shown_when"],
                          **({"title_when_true": v["title_when_true"], "title_when_false": v["title_when_false"],
                              "title_condition": v["title_condition"]} if "title_when_true" in v else {})}
                      for k, v in roles.items()}
    return facts


def extract(source_files: list[dict], primary_dir: str = "") -> dict:
    """Facts for the screen whose files are in source_files (same list the screen spec uses)."""
    try:
        return extract_from_texts(_pick(source_files, primary_dir, ".ts"), _pick(source_files, primary_dir, ".html"))
    except Exception:
        logger.exception("behavior facts could not be read - the matrix will use the field spec only")
        return {"found": False, "buttons": [], "dialogs": [], "branches": [], "list": {}, "modal": {},
                "type_text": {}, "search_fields": [], "roles": {}}


def summary(facts: dict) -> str:
    if not facts or not facts.get("found"):
        return "no TS/HTML facts found"
    lst = facts.get("list") or {}
    bits = [f"{len(facts.get('buttons', []))} button(s)", f"{len(facts.get('dialogs', []))} dialog(s)",
            f"{len(facts.get('branches', []))} save/delete branch(es)"]
    for key, name in (("search", "search"), ("filter", "filter"), ("sortable_columns", "sortable columns"),
                      ("views", "view toggle"), ("counters", "counters"), ("empty_state", "empty state")):
        if lst.get(key):
            bits.append(name)
    return ", ".join(bits)