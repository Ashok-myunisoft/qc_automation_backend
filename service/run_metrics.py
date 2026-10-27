"""Phase 0 instrumentation: per-LLM-call usage and per-screen scenario counts.

Everything here is best-effort and never raises into the pipeline.
One JSON line per screen run is appended to logs/run_metrics.jsonl.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path

logger = logging.getLogger(__name__)
_LOG_PATH = Path(__file__).resolve().parent.parent / "logs" / "run_metrics.jsonl"


def usage_of(response) -> dict:
    """Pulls model usage / finish_reason out of an agent_framework AgentResponse, whatever it exposes."""
    out: dict = {}
    try:
        u = getattr(response, "usage_details", None) or getattr(response, "usage", None)
        if u is not None:
            if isinstance(u, dict):
                get = u.get
            else:
                get = lambda k, d=None: getattr(u, k, d)  # noqa: E731
            out["input_tokens"] = get("input_token_count", None) or get("input_tokens", None)
            out["output_tokens"] = get("output_token_count", None) or get("output_tokens", None)
        out["finish_reason"] = str(getattr(response, "finish_reason", None)
                                   or _raw_finish_reason(response) or "unavailable")
        if "input_tokens" not in out:
            out["available_attrs"] = [a for a in dir(response) if not a.startswith("_")][:25]
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)
    return out


def _raw_finish_reason(response):
    raw = getattr(response, "raw_representation", None)
    try:
        choices = getattr(raw, "choices", None) or (raw.get("choices") if isinstance(raw, dict) else None)
        if choices:
            c = choices[0]
            return getattr(c, "finish_reason", None) or (c.get("finish_reason") if isinstance(c, dict) else None)
    except Exception:  # noqa: BLE001
        pass
    return None


def log_llm_call(agent: str, model: str, response, started: float | None = None, extra: str = "") -> dict:
    info = {"agent": agent, "model": model, **usage_of(response)}
    if started:
        info["seconds"] = round(time.time() - started, 1)
    if extra:
        info["note"] = extra
    logger.info("llm-call %s", json.dumps(info, default=str))
    if info.get("finish_reason", "").lower() in ("length", "max_tokens"):
        logger.warning("llm-call %s hit the output limit (finish_reason=%s)", agent, info["finish_reason"])
    return info


def counts(feature_text: str) -> dict:
    """Scenario outline and Examples row counts of a feature text."""
    text = feature_text or ""
    outlines = len(re.findall(r"^\s*Scenario\s+(?:Outline|Template)\s*:", text, re.M | re.I))
    scenarios = len(re.findall(r"^\s*Scenario\s*:", text, re.M | re.I))
    rows, in_ex, header = 0, False, False
    for ln in text.splitlines():
        s = ln.strip()
        if re.match(r"^(Examples|Scenarios)\s*:", s, re.I):
            in_ex, header = True, True
            continue
        if in_ex and s.startswith("|"):
            if header:
                header = False
            else:
                rows += 1
        elif s and not s.startswith("#"):
            in_ex = False
    return {"outlines": outlines, "scenarios": scenarios, "rows": rows}


class ScreenRun:
    """Collects stage counts for one screen run and writes one JSON line at the end."""

    def __init__(self, screen: str, module: str = ""):
        self.data = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "module": module, "screen": screen,
                     "stages": {}, "llm_calls": 0, "paths": {}, "flags": _flags()}
        self.t0 = time.time()

    def stage(self, name: str, feature_text: str) -> dict:
        c = counts(feature_text)
        self.data["stages"][name] = c
        logger.info("run-metrics %s stage=%s outlines=%d rows=%d", self.data["screen"], name, c["outlines"], c["rows"])
        return c

    def note(self, key: str, value) -> None:
        self.data["paths"][key] = value

    def llm(self, n: int = 1) -> None:
        self.data["llm_calls"] += n

    def summary(self) -> str:
        parts = [f"{k}: {v['outlines']}/{v['rows']}" for k, v in self.data["stages"].items()]
        return "scenarios (outlines/rows) by stage - " + "; ".join(parts) if parts else "no stage counts"

    def write(self) -> None:
        try:
            self.data["seconds"] = round(time.time() - self.t0, 1)
            _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with _LOG_PATH.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(self.data, ensure_ascii=False, default=str) + "\n")
        except Exception:  # noqa: BLE001
            logger.exception("could not write run metrics")


def _flags() -> dict:
    return {"screen_spec": True, "backend_digest": True, "analysis_agent": False, "feature_passes": True,
            "extended_steps": True, "coverage_loop": True}