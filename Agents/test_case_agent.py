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

from service import screen_spec as _screen_spec, value_gen as _value_gen  # screen spec
from service import run_metrics as _run_metrics  # Phase 0 instrumentation

logger = logging.getLogger(__name__)


class TestCaseAgent:
    def __init__(self):
        self.client = OpenAIChatClient(
            model=os.getenv("AI_MODEL")
        )

        self.instructions = Path(
            "prompts/test_case_prompt.txt").read_text(encoding="utf-8")

        self.agent = Agent(
            name="TestCaseAgent",
            client=self.client,
            instructions=self.instructions
        )

    async def generate_test_cases(
        self,
        project_analysis: dict,
        user_request: str,
        business_context: dict = None,
        extended: bool = False,
    ):
        """Every scenario is a Scenario Outline with an aligned Examples table, and
        steps use the QC team's fixed vocabulary (see test_case_prompt.txt,
        #FIXED STEP VOCABULARY) so they match the template step definitions
        that ScriptGenerateAgent copies into every script."""
        # Screen spec (only present for screens that have a formjson): shown as its own block, not inside the analysis JSON.
        spec = project_analysis.get("screen_spec") if isinstance(project_analysis, dict) else None
        spec_block = ""
        if spec:
            spec_block = (
                "\n\n" + _screen_spec.rules_for("TEST_CASES") + "\n\n"
                + _value_gen.render_for_test_cases(
                    spec, project_analysis.get("value_pool") or {}, project_analysis.get("scenario_plan") or [])
            )
            project_analysis = {k: v for k, v in project_analysis.items()
                                if k not in ("screen_spec", "value_pool", "scenario_plan")}
        _ext_vocab = ""
        try:
            from service import extended_steps as _ext_steps
            if extended:
                _ext_vocab = ("\n\nEXTENDED STEPS (also allowed when the screen has the capability; spelled exactly):\n"
                              + _ext_steps.EXTENDED_VOCAB)
        except Exception:
            _ext_vocab = ""
        user_message = f"""
User Request:
{user_request}

Project Analysis:
{json.dumps(project_analysis, indent=2)}

Business Context:
{json.dumps(business_context, indent=2) if business_context else "No business context provided."}

Reminder: every scenario must be a "Scenario Outline" with an aligned Examples table
(Condition column, one column per field, ExpectedResult column, at most 25 rows per table),
use the fixed step vocabulary, and cover the Save/form coverage matrix using only fields
present in the Project Analysis. Every step line must start with Given, When, Then or And.
Choose each field's step sentence (picklist / input / combobox / date / radio / checkbox /
textarea) from the suffix of that field's verified data-cy.{spec_block}{_ext_vocab}
"""

        _t0 = time.time()
        response = await self.agent.run(user_message)
        _run_metrics.log_llm_call("TestCaseAgent.generate_test_cases", os.getenv("AI_MODEL", ""), response, _t0)
        return response.text

    async def generate_pass(self, prefix: str, tail: str) -> str:
        """One focused topic pass. `prefix` is identical for every pass of a screen (so prompt
        caching applies); `tail` is the pass-specific part. Uses prompts/feature_pass_prompt.txt as instructions."""
        if getattr(self, "_pass_agent", None) is None:
            self._pass_agent = Agent(
                name="FeaturePassAgent", client=self.client,
                instructions=Path("prompts/feature_pass_prompt.txt").read_text(encoding="utf-8"),
            )
        _t0 = time.time()
        response = await self._pass_agent.run(prefix + "\n\n" + tail)
        _run_metrics.log_llm_call("TestCaseAgent.generate_pass", os.getenv("AI_MODEL", ""), response, _t0)
        text = (response.text or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*[ \t]*\r?\n", "", text)
            text = re.sub(r"\r?\n```\s*$", "", text)
        return text.strip()

    async def repair_test_cases(self, test_cases: str, findings_text: str, extended: bool = False) -> str:
        """One-shot repair of a feature file using findings from the
        deterministic step checker (service/step_checker.py). Returns the full
        corrected feature text. The caller keeps the original if the repair
        does not actually reduce the findings."""
        _ext = ""
        try:
            from service import extended_steps as _ext_steps
            if extended:
                _ext = ("\n- The EXTENDED step vocabulary is also allowed (never change an extended step into a base one):\n"
                        + _ext_steps.EXTENDED_VOCAB + "\n")
        except Exception:
            _ext = ""
        user_message = f"""
The feature file below has defects found by an automatic step checker. Fix
ONLY the listed defects and return the COMPLETE corrected feature file.

Rules for the repair:
- Do not add, remove or reorder scenarios, rows or steps unless a defect below
  requires it. Keep every Scenario Outline, Rule, Background and Examples table.
- Examples values must never contain double quotes - remove them from the values.
- Use the exact step vocabulary from your instructions, spelled exactly the same
  way (for example "I click the save button" - lowercase s). Keep each field on the
  step sentence for its control type (picklist / input / combobox / date / radio /
  checkbox / textarea) - never change a combobox, date or radio step into a picklist
  or input step. Every step line starts with Given, When, Then or And.
- An empty Examples cell is allowed for EVERY field step, picklists included (the step then
  leaves the field empty). Never move a row to another outline, and never remove a column or a
  step, just to avoid an empty cell: a "field missing" row keeps its column, with an empty cell.
- Every "<Placeholder>" in a step needs an Examples column with exactly that name.
- Keep Examples tables aligned. Output ONLY Gherkin - no markdown, no commentary.{_ext}

Defects found:
{findings_text}

Feature file to repair:
{test_cases}
"""
        _t0 = time.time()
        response = await self.agent.run(user_message)
        _run_metrics.log_llm_call("TestCaseAgent.repair_test_cases", os.getenv("AI_MODEL", ""), response, _t0)
        text = response.text.strip()
        if text.startswith("```"):  # tolerate a stray Markdown fence
            text = re.sub(r"^```[a-zA-Z]*[ \t]*\r?\n", "", text)
            text = re.sub(r"\r?\n```\s*$", "", text)
        return text.strip()