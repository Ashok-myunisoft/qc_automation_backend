"""Code-built `project_analysis` (same schema as the analysis agent's, subset) from the screen spec.

Used for every normal (non-MM) screen that has a formjson spec - the analysis agent is not called for it.
The live data-cy scan verifies the locators afterwards; nothing here guesses a data-cy for a field.
"""
from __future__ import annotations

import logging
import re

from service import chunk_analysis, screen_behavior

logger = logging.getLogger(__name__)


def bypass_applies(spec: dict | None, is_mm: bool) -> tuple[bool, str]:
    """(applies, reason) - reason is logged as one line. The code chooses the path by what the screen has."""
    if is_mm:
        return False, "MM screen keeps the analysis agent"
    if not spec or not spec.get("fields"):
        return False, "no formjson spec for this screen"
    return True, "code-built analysis"


def _kind_category(kind: str) -> str:
    return {"picklist": "picklist", "combobox": "combobox", "date": "date", "radio": "radio",
            "checkbox": "checkbox", "textarea": "textarea"}.get(kind or "", "input")


def _field(f: dict) -> dict:
    out = {
        "field_key": f["name"], "control_name": f["name"], "label": f.get("label") or f["name"],
        "required": bool(f.get("required")), "readonly": bool(f.get("readonly")),
        "component_kind": f.get("kind") or "input", "component_category": _kind_category(f.get("kind", "")),
        "automation_locator": {"strategy": "data_cy", "value": None, "verified": False},
    }
    if f.get("max_length"):
        out["max_length"] = f["max_length"]
    if f.get("regex"):
        out["regex"] = f["regex"]
    if f.get("options"):
        out["options"] = [o["text"] for o in f["options"] if not o.get("blank")]
    if f.get("key_field"):
        out["is_key_field"] = True
    if f.get("calculated"):
        out["calculated"] = True
        out["calculated_origin"] = f.get("calculated_origin") or []
        if f.get("computed_from"):
            out["computed_from"] = f["computed_from"]
        if f.get("calculated_rule"):
            out["calculated_rule"] = f["calculated_rule"]
    return out


_ACTION_FROM_CY = (("addnew", "Add New"), ("save", "Save"), ("update", "Update"), ("edit", "Edit"), ("delete", "Delete"))


def _business_actions(spec: dict) -> list[dict]:
    seen, out = set(), []
    for cy in spec.get("buttons") or []:
        low = str(cy).lower()
        for key, name in _ACTION_FROM_CY:
            if key in low and name not in seen:
                seen.add(name)
                out.append({"action_name": name, "data_cy": str(cy),
                            "automation_locator": {"strategy": "data_cy", "value": None, "verified": False}})
    for name in ("Add New", "Save", "Update", "Delete"):  # product defaults: same ones the script builder assumes
        if name not in seen:
            out.append({"action_name": name, "automation_locator": {"strategy": "data_cy", "value": None, "verified": False}})
    return out


def _sections(source_files: list[dict]) -> list[dict]:
    """Tabs, via the existing chunk_analysis helpers (best effort)."""
    out = []
    try:
        html = "\n".join(f.get("content", "") or "" for f in source_files if str(f.get("path", "")).endswith(".html")
                         and not str(f.get("path", "")).startswith("[backend]"))
        _body, tabs = chunk_analysis.extract_tab_sections(html)
        for i, (label, content) in enumerate(tabs, start=1):
            names = [m.lower() for m in re.findall(r"""formControlName\s*=\s*["']([\w.]+)["']""", content)]
            out.append({"section_label": label or f"Section {i}", "activation_required": True,
                        "contains_control_names": list(dict.fromkeys(names))})
    except Exception:  # noqa: BLE001
        logger.exception("screen_facts: tab detection failed - treating the screen as untabbed")
    return out


def build_analysis_from_spec(spec: dict, project_context: dict, module: str, screen_name: str) -> dict:
    files = project_context.get("source_files") or []
    fields = [_field(f) for f in spec["fields"] if f.get("candidate")]
    nav = project_context.get("navigation_hint") or []
    if isinstance(nav, str):
        nav = [p.strip() for p in re.split(r"->|>|/", nav) if p.strip()]
    nav = [str(x) for x in nav if x] or [module, screen_name]

    behavior = screen_behavior.extract_behavior([f for f in files if not str(f.get("path", "")).startswith("[backend]")])
    msgs = spec.get("messages") or {}
    ui_validations = [{"message": m} for m in msgs.get("required", [])]
    business_validations = []
    for key, vals in msgs.items():
        if key in ("required", "frontend"):
            continue
        for v in vals if isinstance(vals, list) else []:
            business_validations.append({"rule": key, "message": v})
    for r in ((spec.get("backend") or {}).get("rules") or []):
        if isinstance(r, dict) and r.get("message"):
            business_validations.append({"rule": r.get("name") or r.get("kind") or "backend rule", "message": r["message"]})
    dialogs = [{"expected_message": m} for m in msgs.get("frontend", [])]
    es = behavior.get("empty_state") or {}
    if es.get("message"):
        dialogs.append({"expected_message": es["message"]})

    page = {
        "page_name": screen_name,
        "navigation_entry_type": project_context.get("navigation_entry_type") or "menu",
        "navigation_path": nav,
        "forms": [{"form_name": spec.get("formname") or screen_name, "fields": fields}],
        "loops": [],
        "screen_sections": _sections(files),
        "business_actions": _business_actions(spec),
        "tables": [], "dialogs": dialogs,
        "ui_validations": ui_validations, "business_validations": business_validations,
        "functional_behavior": {"list_behavior": {k: v for k, v in behavior.items() if k != "button_visibility"},
                                "source": "code-extracted from HTML/TS; raw files are given to the feature passes"},
    }
    grid_fields = [_field(f) for f in spec["fields"] if f.get("candidate") and f.get("grid")]
    if grid_fields:
        page["loops"].append({"fields": grid_fields})
        page["forms"][0]["fields"] = [x for x in fields if x["control_name"] not in {g["control_name"] for g in grid_fields}]
    return {"modules": [{"module_name": module, "pages": [page]}], "unresolved": [],
            "source": "code-built from the screen spec", "behavior": behavior}