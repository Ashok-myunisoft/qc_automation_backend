"""Extracts list / form behaviour facts from the raw screen HTML and TS (no LLM).

Every detected item keeps the exact source snippet it came from, so a pass can cite it and
the checker can verify it exists. If nothing is detected for a capability its key is absent.
"""
from __future__ import annotations

import re

_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_TAG_RE = re.compile(r"<([A-Za-z][\w-]*)((?:\s(?:[^<>\"']|\"[^\"]*\"|'[^']*')*)?)/?>", re.S)
_ATTR_RE = re.compile(r"""([:\[\(\)\]\*#\w.-]+)\s*(?:=\s*(?:"([^"]*)"|'([^']*)'))?""")


def _snip(text: str, limit: int = 220) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:limit]


def _attrs(raw: str) -> dict:
    out = {}
    for m in _ATTR_RE.finditer(raw or ""):
        out[m.group(1)] = m.group(2) if m.group(2) is not None else (m.group(3) or "")
    return out


def _first_class(cls: str) -> str:
    return (cls or "").strip().split()[0] if (cls or "").strip() else ""


def _html_of(files: list[dict]) -> str:
    return _COMMENT_RE.sub("", "\n".join(f.get("content", "") or "" for f in files
                                          if str(f.get("path", "")).lower().endswith(".html")))


def _ts_of(files: list[dict]) -> str:
    return "\n".join(f.get("content", "") or "" for f in files
                     if str(f.get("path", "")).lower().endswith(".ts") and ".spec." not in str(f.get("path", "")).lower())


def _text_after(html: str, pos: int, span: int = 400) -> str:
    chunk = html[pos:pos + span]
    chunk = re.split(r"<(?!/?(?:span|b|i|strong)\b)", chunk, maxsplit=1)[0]
    return re.sub(r"<[^>]+>", "", chunk).strip()


def _inner_text(html: str, start: int, tag: str) -> str:
    m = re.search(r"</%s\s*>" % re.escape(tag), html[start:], re.I)
    seg = html[start:start + (m.start() if m else 200)]
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", seg)).strip()


def extract_behavior(source_files: list[dict]) -> dict:
    html, ts = _html_of(source_files), _ts_of(source_files)
    out: dict = {}
    if not html and not ts:
        return out
    tags = [(m.group(1).lower(), _attrs(m.group(2)), m) for m in _TAG_RE.finditer(html)]

    # ---- list / table / card containers --------------------------------------------------------
    containers = {}
    for tag, at, m in tags:
        cls = at.get("class", "")
        if re.search(r"table-container|data-table|list-container", cls, re.I):
            containers["list"] = {"class": _first_class(cls), "tag": tag, "snippet": _snip(m.group(0))}
        elif tag == "table" and "list" not in containers:
            containers["list"] = {"class": "", "tag": tag, "snippet": _snip(m.group(0))}
        if re.search(r"card-grid|cards?-container|card-list", cls, re.I):
            containers.setdefault("card", {"class": _first_class(cls), "tag": tag, "snippet": _snip(m.group(0))})
    if containers:
        out["containers"] = containers

    # ---- search ---------------------------------------------------------------------------------
    for tag, at, m in tags:
        if tag == "input" and (at.get("type", "").lower() == "search"
                               or re.search(r"search", " ".join([at.get("placeholder", ""), at.get("class", ""),
                                                                 at.get("name", ""), at.get("aria-label", "")]), re.I)):
            if at.get("data-cy") == "PicklistSearchBar":
                continue
            out["search"] = {"placeholder": at.get("placeholder", ""), "class": at.get("class", ""),
                             "snippet": _snip(m.group(0))}
            break

    # ---- filters --------------------------------------------------------------------------------
    filters = []
    for tag, at, m in tags:
        if tag != "select":
            continue
        end = html.find("</select", m.end())
        body = html[m.end(): end if end != -1 else m.end() + 1500]
        opts = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", o)).strip()
                for o in re.findall(r"<option\b[^>]*>(.*?)</option>", body, re.S | re.I)]
        opts = [o for o in opts if o and "{{" not in o]
        all_opt = next((o for o in opts if re.match(r"(all|any)\b", o, re.I)), "")
        filters.append({"class": _first_class(at.get("class", "")), "all_option": all_opt, "static_options": opts,
                        "snippet": _snip(m.group(0))})
    if filters:
        out["filters"] = filters

    # ---- sortable columns -----------------------------------------------------------------------
    cols = []
    for tag, at, m in tags:
        if tag != "th":
            continue
        click = " ".join(v for k, v in at.items() if k in ("(click)",))
        if not re.search(r"sort", click + " " + at.get("class", ""), re.I):
            continue
        fm = re.search(r"""['"](\w+)['"]""", click)
        cols.append({"field": fm.group(1) if fm else "", "label": _inner_text(html, m.end(), "th"),
                     "snippet": _snip(m.group(0))})
    if cols:
        out["sortable_columns"] = cols
        ranks = _sort_ranks(ts)
        if ranks:
            out["sort_ranks"] = ranks

    # ---- view toggles ---------------------------------------------------------------------------
    views = []
    for tag, at, m in tags:
        title = at.get("title", "")
        click = at.get("(click)", "")
        if re.search(r"\b(list|card|grid)\s*view\b", title, re.I) or re.search(r"(view|display)mode\s*=\s*['\"]?(card|list|grid)", click, re.I):
            mode = "card" if re.search(r"card|grid", title + click, re.I) else "list"
            views.append({"mode": mode, "title": title, "snippet": _snip(m.group(0))})
    if views:
        out["views"] = views

    # ---- counters / stat tiles ------------------------------------------------------------------
    labels = []
    for tag, at, m in tags:
        if re.search(r"\bstat-label\b|\bkpi-label\b|\bsummary-label\b", at.get("class", "")):
            labels.append({"class": _first_class(at.get("class", "")), "label": _inner_text(html, m.end(), tag),
                           "snippet": _snip(m.group(0))})
    nums = [at.get("class", "") for tag, at, m in tags if re.search(r"\bstat-number\b|\bkpi-value\b|\bsummary-value\b", at.get("class", ""))]
    if labels:
        out["counters"] = {"label_class": labels[0]["class"], "number_class": _first_class(nums[0]) if nums else "",
                           "labels": [l["label"] for l in labels if l["label"]], "snippet": labels[0]["snippet"]}

    # ---- empty state ----------------------------------------------------------------------------
    for tag, at, m in tags:
        if re.search(r"empty-state|no-data|no-records|nodata", at.get("class", ""), re.I):
            msg = _inner_text(html, m.end(), tag)
            msg = re.sub(r"\{\{.*?\}\}", "", msg).strip()
            out["empty_state"] = {"class": _first_class(at.get("class", "")), "message": msg[:120],
                                  "snippet": _snip(html[m.start(): m.start() + 300])}
            break

    # ---- modal / reset / close / button visibility ---------------------------------------------
    modal = next(((tag, at, m) for tag, at, m in tags if re.search(r"(^|\s)modal(\s|$)", at.get("class", ""))), None)
    if modal:
        out["modal"] = {"selector": ".modal", "snippet": _snip(modal[2].group(0))}
    roles = {}
    for tag, at, m in tags:
        title, cls, click = at.get("title", ""), at.get("class", ""), at.get("(click)", "")
        hay = " ".join([title, cls, click]).lower()
        if re.search(r"close", hay) and "close" not in roles and not at.get("data-cy"):
            roles["close"] = {"title": title, "class": _first_class(cls), "snippet": _snip(m.group(0))}
        elif re.search(r"\breset\b|clear\s*form|new\s*form", hay) or (title.strip().lower() == "new" and "add" in cls):
            roles.setdefault("reset", {"title": title, "class": _first_class(cls), "snippet": _snip(m.group(0))})
    if roles:
        out["roles"] = roles
    vis = []
    for tag, at, m in tags:
        cond = at.get("*ngIf", "")
        label = " ".join([at.get("title", ""), at.get("data-cy", ""), at.get("class", "")])
        if cond and re.search(r"\b(save|update|delete|reset|edit)\b", label, re.I):
            vis.append({"control": _snip(label, 60), "condition": _snip(cond, 80), "snippet": _snip(m.group(0))})
    if vis:
        out["button_visibility"] = vis
    return out


def _sort_ranks(ts: str) -> dict:
    """Rank maps in the TS, e.g.  { Manufacturing: 0, Quality: 1 }  -> {"<name>": {"Manufacturing": 0, ...}}"""
    ranks = {}
    for m in re.finditer(r"(\w*(?:rank|order|priority)\w*)\s*[:=][^{;]{0,40}\{([^{}]{5,300})\}", ts, re.I):
        pairs = re.findall(r"['\"]?([A-Za-z][\w ]*)['\"]?\s*:\s*(\d+)", m.group(2))
        if len(pairs) >= 2:
            ranks[m.group(1)] = {k.strip(): int(v) for k, v in pairs}
    return ranks


def capabilities(behavior: dict) -> list[str]:
    """Capability names present in a behavior dict (used by the planner and the coverage checklist)."""
    caps = []
    if behavior.get("search"):
        caps.append("search")
    if behavior.get("filters"):
        caps.append("filter")
    if behavior.get("sortable_columns"):
        caps.append("sort")
    if behavior.get("views"):
        caps.append("view")
    if behavior.get("counters"):
        caps.append("counters")
    if behavior.get("empty_state"):
        caps.append("empty_state")
    roles = behavior.get("roles") or {}
    if "reset" in roles:
        caps.append("reset")
    if "close" in roles:
        caps.append("close")
    if behavior.get("button_visibility"):
        caps.append("button_visibility")
    return caps
