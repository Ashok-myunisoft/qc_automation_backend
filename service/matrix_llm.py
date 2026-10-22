"""OPTIONAL: one cheap AI call per screen that returns realistic names / descriptions for the scenario matrix
(service/scenario_matrix.py). Off unless USE_MATRIX_LLM_VALUES=1. Every value the model returns is checked by
code (length, no double quotes, not empty, unique); anything that fails is dropped and the built-in word bank
of service/matrix_values.py is used instead, so a bad answer can never break generation.
"""
from __future__ import annotations

import json
import logging
import os
import re

logger = logging.getLogger(__name__)

_INSTRUCTIONS = (
    "You write realistic test data for an ERP screen. Reply with ONE JSON object only (no markdown): "
    "{\"<FieldName>\": [\"value 1\", \"value 2\", ...]} with the number of values asked for. "
    "Values must look like real business data for that screen and field, be different from each other, "
    "never contain double quotes, pipes or angle brackets, and respect the maximum length given for the field."
)


def enabled() -> bool:
    return os.getenv("USE_MATRIX_LLM_VALUES", "0").strip().lower() in ("1", "true", "yes", "on")


def _target_fields(spec: dict) -> list[dict]:
    out = []
    for f in spec.get("fields", []):
        if not f.get("active") or f.get("readonly") or f.get("kind") == "grid" or f.get("record_picker"):
            continue
        s = f"{f['name']} {f.get('label', '')}".lower()
        is_name = bool(re.search(r"name|title", s)) and not re.search(r"code|number", s)
        is_desc = f.get("kind") == "textarea" or bool(re.search(r"desc|remark|note|comment", s))
        if (is_name or is_desc) and f.get("kind") in ("input", "textarea", "picklist"):
            out.append(f)
    return out


def build_user_message(spec: dict, fields: list[dict], per_field: int) -> str:
    lines = [f"Screen: {spec.get('screen_name') or spec.get('formname')}", f"Give {per_field} values for each field:"]
    for f in fields:
        mx = f.get("max_length")
        # names get a short run suffix from the matrix, so ask for a little less
        room = (mx - 4) if (mx and f.get("kind") != "textarea") else mx
        lines.append(f"- {f['name']} (label '{f.get('label')}', {'description text' if f.get('kind') == 'textarea' else 'name'}"
                     f"{', at most ' + str(room) + ' characters' if room else ''})")
    return "\n".join(lines)


def parse_and_validate(text: str, fields: list[dict], min_values: int = 3) -> dict[str, list[str]]:
    """Pure function (unit-testable): model text -> {field: [clean values]}; bad answers give {}."""
    try:
        m = re.search(r"\{.*\}", text or "", re.S)
        data = json.loads(m.group(0)) if m else {}
    except Exception:
        return {}
    out: dict[str, list[str]] = {}
    for f in fields:
        raw = data.get(f["name"])
        if not isinstance(raw, list):
            continue
        mx = f.get("max_length")
        room = (mx - 4) if (mx and f.get("kind") != "textarea") else mx
        seen, good = set(), []
        for v in raw:
            v = re.sub(r"\s+", " ", re.sub(r"[\"|<>\r\n]+", " ", str(v))).strip()
            if not v or (room and len(v) > room) or v.lower() in seen:
                continue
            seen.add(v.lower())
            good.append(v)
        if len(good) >= min_values:
            out[f["name"]] = good
    return out


async def realistic_values(spec: dict, per_field: int = 10) -> dict[str, list[str]]:
    """{} when the flag is off or anything goes wrong."""
    if not enabled():
        return {}
    fields = _target_fields(spec)
    if not fields:
        return {}
    try:
        from agent_framework import Agent
        from agent_framework.openai import OpenAIChatClient

        client = OpenAIChatClient(model=os.getenv("OPENAI_MODEL"))
        agent = Agent(name="MatrixValueAgent", client=client, instructions=_INSTRUCTIONS)
        response = await agent.run(build_user_message(spec, fields, per_field))
        values = parse_and_validate(response.text, fields)
        logger.info("matrix: AI values for %d of %d field(s)", len(values), len(fields))
        return values
    except Exception:
        logger.exception("matrix: AI values failed - using the built-in word bank")
        return {}