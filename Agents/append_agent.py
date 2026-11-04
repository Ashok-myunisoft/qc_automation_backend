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
        # universal refine box (service/refine_engine.py)
        self.router = Agent(
            name="RefineRouterAgent",
            client=OpenAIChatClient(model=classify_model),
            instructions=Path("prompts/refine_router_prompt.txt").read_text(encoding="utf-8"),
        )
        self.editor = Agent(
            name="RefineEditAgent",
            client=OpenAIChatClient(model=scenario_model),
            instructions=Path("prompts/refine_edit_prompt.txt").read_text(encoding="utf-8"),
        )
        self.script_editor = Agent(
            name="RefineScriptAgent",
            client=OpenAIChatClient(model=scenario_model),
            instructions=Path("prompts/refine_script_prompt.txt").read_text(encoding="utf-8"),
        )
        self.answerer = Agent(
            name="RefineAnswerAgent",
            client=OpenAIChatClient(model=classify_model),
            instructions=("You answer a QA engineer's question about ONE screen of an ERP application: its Gherkin feature file "
                          "and its Cypress script. You are given an overview, the feature text, the script text (or the parts "
                          "related to the question), the fields that exist on the screen and the earlier chat. "
                          "Use only what you are given - never invent scenarios, rows, fields, messages or behaviour. "
                          "Be plain and concrete (numbers, scenario names, field names, the exact step or message). Keep it short "
                          "for a simple question; for 'explain', 'review' or 'what is missing' give a clear short list with the "
                          "reason for each point (for example fields that no scenario uses, a rule with no negative case, a step "
                          "whose code looks fragile). If what you were given does not contain the answer, say so and say what "
                          "would be needed. You never change anything. You may show Gherkin or code only when asked, in a code "
                          "block, and you must say it has NOT been added to the files (the user can ask for it to be added)."),
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

    # ------------------------------------------------------------------
    # universal refine box
    # ------------------------------------------------------------------
    async def route_request(self, text: str, history: str = "") -> str:
        """Splits the box text into EDIT / SCRIPT / DEEPEN / QUESTION / CLARIFY jobs (JSON text).
        `history` = the recent chat of this screen, so that "do the same for Quantity" can be understood."""
        message = text if not history else (
            f"EARLIER CHAT (oldest first):\n{history}\n\nNEW MESSAGE (split ONLY this one):\n{text}")
        response = await self.router.run(message)
        return _strip_fence(response.text)

    async def edit_feature(self, instruction: str, context: dict, previous_error: str = "") -> str:
        """Returns block operations as JSON text (see prompts/refine_edit_prompt.txt)."""
        fields = "\n".join(f"- {n}: {h}" for n, h in (context.get("fields") or {}).items()) or "- (none found)"
        titles = "\n".join(f"- {t}" for t in (context.get("titles") or [])) or "- (none)"
        messages = "\n".join(f"- {m}" for m in (context.get("messages") or [])) or "- (none)"
        retry = f"\n{previous_error}\n" if previous_error else ""
        message = f"""
REQUEST:
{instruction}
{retry}
THE FEATURE ({context.get("note")}):
{context.get("text")}

FIELDS THAT EXIST ON THIS SCREEN:
{fields}

MESSAGES ALREADY USED AS ExpectedResult:
{messages}

TITLES OF EVERY SCENARIO IN THE FILE (use these exact titles for replace / delete):
{titles}
"""
        t0 = time.time()
        response = await self.editor.run(message)
        try:
            from service import run_metrics
            run_metrics.log_llm_call("AppendAgent.edit_feature", os.getenv("AI_MODEL", ""), response, t0)
        except Exception:  # noqa: BLE001
            pass
        return _strip_fence(response.text)

    async def edit_script(self, instruction: str, context: dict, previous_error: str = "") -> str:
        """Returns exact find / replace operations on the Cypress script as JSON text (see prompts/refine_script_prompt.txt)."""
        steps = "\n".join(f"- {t}" for t in (context.get("steps") or [])) or "- (none)"
        retry = f"\n{previous_error}\n" if previous_error else ""
        message = f"""
REQUEST:
{instruction}
{retry}
THE SCRIPT ({context.get("note")}):
{context.get("text")}

STEP SENTENCES USED IN THE FEATURE (the script must keep defining every one of them):
{steps}
"""
        t0 = time.time()
        response = await self.script_editor.run(message)
        try:
            from service import run_metrics
            run_metrics.log_llm_call("AppendAgent.edit_script", os.getenv("AI_MODEL", ""), response, t0)
        except Exception:  # noqa: BLE001
            pass
        return _strip_fence(response.text)

    async def answer_question(self, question: str, overview: str, feature_text: str = "", script_text: str = "",
                              history: str = "", fields: str = "", messages: str = "") -> str:
        message = f"QUESTION:\n{question}\n\nOVERVIEW OF THE FEATURE FILE:\n{overview}"
        if history:
            message += f"\n\nEARLIER CHAT (oldest first):\n{history}"
        if fields:
            message += f"\n\nFIELDS THAT EXIST ON THIS SCREEN:\n{fields}"
        if messages:
            message += f"\n\nMESSAGES ALREADY USED AS ExpectedResult:\n{messages}"
        if feature_text:
            message += f"\n\nTHE FEATURE FILE:\n{feature_text}"
        if script_text:
            message += f"\n\nTHE CYPRESS SCRIPT:\n{script_text}"
        response = await self.answerer.run(message)
        return (response.text or "").strip()