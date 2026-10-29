"""Post a run result to a Microsoft Teams chat / channel through a Workflows webhook.

Setup (once, in the server's .env):
    TEAMS_WEBHOOK_URL=<the link copied from Teams -> Workflows -> "Copy webhook link">
    REPORT_BASE_URL=http://<server>:8053        (optional - adds report + screenshots download buttons)

Nothing here ever raises into a run: send_card() returns (ok, detail) and the caller only logs it.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 20
_MAX_LIST = 10          # failed screens shown in a message; the rest are summarised as "...and N more"


# ----------------------------------------------------------------------------- config
def webhook_url() -> str:
    return os.getenv("TEAMS_WEBHOOK_URL", "").strip()


def is_configured() -> bool:
    return bool(webhook_url())


def _history_url(run_id, kind: str) -> str | None:
    base = os.getenv("REPORT_BASE_URL", "").strip().rstrip("/")
    return f"{base}/api/history/{run_id}/{kind}" if base and run_id else None


# ----------------------------------------------------------------------------- sending
def send_card(card: dict) -> tuple[bool, str]:
    """POST one adaptive card. Blocking - call it with asyncio.to_thread."""
    url = webhook_url()
    if not url:
        return False, "TEAMS_WEBHOOK_URL is not set on the server"
    payload = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "contentUrl": None,
            "content": card,
        }],
    }
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            if 200 <= response.status < 300:
                return True, f"HTTP {response.status}"
            return False, f"HTTP {response.status}"
    except urllib.error.HTTPError as e:
        logger.warning("teams notification rejected: HTTP %s", e.code)
        return False, f"Teams answered HTTP {e.code} (is the workflow still turned on?)"
    except Exception as e:  # network down, DNS, timeout ...
        logger.warning("teams notification failed: %s", e)
        return False, f"{type(e).__name__}: {e}"


# ----------------------------------------------------------------------------- card building
def _md(text) -> str:
    """Escape the characters Teams would read as formatting (screen names often contain underscores)."""
    out = str(text if text is not None else "")
    for ch in ("\\", "*", "_", "[", "]", "`"):
        out = out.replace(ch, "\\" + ch)
    return out


def duration(seconds) -> str:
    seconds = int(seconds or 0)
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m {s:02d}s" if m else f"{s}s"


def _names(items: list[str], limit: int = 5) -> str:
    shown = [_md(x) for x in items[:limit]]
    if len(items) > limit:
        shown.append(f"+{len(items) - limit} more")
    return ", ".join(shown) or "-"


def _bullets(lines: list[str]) -> str:
    shown = lines[:_MAX_LIST]
    text = "\n\n".join(f"• {line}" for line in shown)
    if len(lines) > _MAX_LIST:
        text += f"\n\n…and {len(lines) - _MAX_LIST} more (see the report / History)"
    return text


def _card(title: str, color: str, facts: list[tuple[str, str]], note: str = "",
          links: list[tuple[str, str]] | None = None) -> dict:
    body: list[dict] = [{"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium", "color": color, "wrap": True}]
    if note:
        body.append({"type": "TextBlock", "text": note, "wrap": True, "spacing": "Small"})
    if facts:
        body.append({"type": "FactSet", "facts": [{"title": f"{k}:", "value": str(v)} for k, v in facts]})
    card = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard", "version": "1.4", "body": body,
    }
    if links:
        card["actions"] = [{"type": "Action.OpenUrl", "title": t, "url": u} for t, u in links]
    return card


def run_card(*, label: str, modules: list[str], items: list[dict], elapsed_seconds: float,
             combined_run_id=None, list_screens: bool = False) -> dict:
    """A completed Auto run / selected-screens run: what ran and how long it took - no pass/fail in the text.

    `items` = [{module, screen, status, ...}]. list_screens=True (selected-screens run) names the screens
    instead of the modules. With REPORT_BASE_URL set the card gets Download buttons for the whole-run
    Excel report and the screenshots compilation.
    """
    ran = [it for it in items if it.get("status") in ("passed", "failed", "error")]
    facts: list[tuple[str, str]] = []
    if list_screens:
        facts.append(("Screens", _names([f"{it.get('module')} / {it.get('screen')}" for it in items if it.get("status")], 5)))
    else:
        facts.append(("Modules", _names(modules)))
        facts.append(("Screens run", str(len(ran))))
    facts.append(("Time taken", duration(elapsed_seconds)))
    links = [(t, u) for t, u in (
        ("Download report (Excel)", _history_url(combined_run_id, "report")),
        ("Download screenshots", _history_url(combined_run_id, "screenshots")),
    ) if u]
    return _card(f"✅ {label} completed", "Good", facts, links=links)


def generate_card(*, modules: list[str], total: int, generated: int, pushed: int,
                  gen_failed: list[str], push_failed: list[str], elapsed_seconds: float) -> dict:
    """A completed Auto generate (generate + push to GitLab; nothing is run)."""
    facts = [
        ("Modules", _names(modules)), ("Screens", total),
        ("Generated and pushed", f"{pushed} of {total}"), ("Time taken", duration(elapsed_seconds)),
    ]
    return _card("✅ Auto generate completed", "Good", facts,
                 note="Tests were generated and pushed to GitLab. Nothing was run.")