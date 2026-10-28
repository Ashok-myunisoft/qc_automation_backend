import json
import logging
import os
import re
import time
from pathlib import Path
from agent_framework import Agent
from agent_framework.openai import OpenAIChatClient
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


def _strip_fence(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*[ \t]*\r?\n", "", t)
        t = re.sub(r"\r?\n```\s*$", "", t)
    return t.strip()


class AppendAgent:
    """Two small model calls used by service/append_editor.py and service/request_intent.py.

    * describe_scenario(): writes ONLY the new Scenario Outline block for a described scenario. It never
      sees or returns the whole feature / script - the editor merges the block in by code.
    * classify_text(): one cheap call that classifies request pieces the deterministic reader could not.

    Value changes, comment lines and pasted scenarios never reach the model at all."""

    def __init__(self):
        scenario_model = os.getenv("AI_MODEL") or os.getenv("OPENAI_MODEL")
        classify_model = os.getenv("OPENAI_MODEL") or os.getenv("AI_MODEL")
        self.instructions = Path("prompts/append_prompt.txt").read_text(encoding="utf-8")
        self.agent = Agent(
            name="AppendAgent",
            client=OpenAIChatClient(model=scenario_model),
            instructions=self.instructions,
        )
        self.classifier = Agent(
            name="RequestClassifierAgent",
            client=OpenAIChatClient(model=classify_model),
            instructions="You classify short QA requests. Reply with JSON only, no prose, no markdown.",
        )

    async def describe_scenario(self, description: str, context: dict, previous_error: str = "") -> str:
        """Returns the new Scenario Outline block(s) as Gherkin text (no Feature / Background / Rule)."""
        fields = "\n".join(f"- {name}: {hint}" for name, hint in (context.get("fields") or {}).items()) or "- (none found)"
        titles = "\n".join(f"- {t}" for t in (context.get("titles") or [])) or "- (none)"
        messages = "\n".join(f"- {m}" for m in (context.get("messages") or [])) or "- (none)"
        retry = (f"\nYour previous answer was rejected: {previous_error}\nFix exactly that and answer again.\n"
                 if previous_error else "")
        message = f"""
REQUEST (write the scenario for this):
{description}
{retry}
FIELDS THAT EXIST ON THIS SCREEN (use ONLY these names in the steps; the text after the colon shows the step
sentence the file already uses for that field):
{fields}

SCENARIO TITLES ALREADY IN THE FILE (do not repeat one):
{titles}

MESSAGES ALREADY USED AS ExpectedResult (copy one verbatim when it fits; never invent a message):
{messages}

ONE EXISTING SCENARIO, ONLY AS A STYLE REFERENCE (do not copy its data or title):
{context.get("example") or "(none)"}
"""
        t0 = time.time()
        response = await self.agent.run(message)
        try:
            from service import run_metrics
            run_metrics.log_llm_call("AppendAgent.describe_scenario", os.getenv("AI_MODEL", ""), response, t0)
        except Exception:  # noqa: BLE001
            pass
        return _strip_fence(response.text)

    async def classify_text(self, prompt: str) -> str:
        response = await self.classifier.run(prompt)
        return _strip_fence(response.text)