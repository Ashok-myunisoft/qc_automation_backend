import json
import os
from pathlib import Path

from agent_framework import Agent
from agent_framework.openai import OpenAIChatClient
from dotenv import load_dotenv

load_dotenv()


def _distill(run_results: list[dict]) -> dict:
    screens = []
    for r in run_results:
        stats = r.get("stats") or {}
        failed_scenarios = []
        for suite in r.get("suites") or []:
            for scen in suite.get("scenarios") or []:
                if scen.get("state") == "failed":
                    msg = (scen.get("err_message") or "").strip()
                    if len(msg) > 300:
                        msg = msg[:297] + "..."
                    failed_scenarios.append({"name": scen.get("name"), "error": msg})
        screens.append({
            "screen":     r.get("slug"),
            "passed":     r.get("passed"),
            "passes":     stats.get("passes", 0),
            "failures":   stats.get("failures", 0),
            "duration_ms": r.get("duration", 0),
            "failed_scenarios": failed_scenarios,
        })
    return {
        "screen_count":     len(run_results),
        "screens_passed":   sum(1 for r in run_results if r.get("passed")),
        "total_scenarios":  sum((r.get("stats") or {}).get("tests", 0) for r in run_results),
        "total_failures":   sum((r.get("stats") or {}).get("failures", 0) for r in run_results),
        "screens":          screens,
    }


class ReportAgent:
    def __init__(self):
        self.client = OpenAIChatClient(model=os.getenv("OPENAI_MODEL"))
        self.instructions = Path("prompts/report_prompt.txt").read_text(encoding="utf-8")
        self.agent = Agent(
            name="ReportAgent",
            client=self.client,
            instructions=self.instructions,
        )

    async def summarize(self, module: str, run_results: list[dict]) -> str:
        distilled = _distill(run_results)
        message = f"""
Module: {module or "(unspecified)"}

Run data (compact — do NOT paraphrase these numbers back at me, use them as facts):
{json.dumps(distilled, indent=2)}
"""
        try:
            response = await self.agent.run(message)
        except Exception:
            return ""
        return (response.text or "").strip()
