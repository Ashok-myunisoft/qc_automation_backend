import json
import os
import re
from pathlib import Path
from agent_framework import Agent
from agent_framework.openai import OpenAIChatClient
from dotenv import load_dotenv

load_dotenv()


class TestCaseAgent:
    def __init__(self):
        self.client = OpenAIChatClient(
            model=os.getenv("OPENAI_MODEL")
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
    ):
        """Every scenario is a Scenario Outline with an aligned Examples table, and
        steps use the QC team's fixed vocabulary (see test_case_prompt.txt,
        #FIXED STEP VOCABULARY) so they match the template step definitions
        that ScriptGenerateAgent copies into every script."""
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
textarea) from the suffix of that field's verified data-cy.
"""

        response = await self.agent.run(user_message)
        return response.text

    async def repair_test_cases(self, test_cases: str, findings_text: str) -> str:
        """One-shot repair of a feature file using findings from the
        deterministic step checker (service/step_checker.py). Returns the full
        corrected feature text. The caller keeps the original if the repair
        does not actually reduce the findings."""
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
- A picklist step must never receive an empty value. For a case that needs an
  empty picklist, put it in its own Scenario Outline that omits that picklist
  step, rather than leaving the cell empty. Fix EVERY listed row - when you are
  done, no Examples cell used by a "... picklist field" step may be empty, and a
  row whose two picklists are both empty goes to an outline that omits both
  select steps. Example of the change:
    BEFORE
      Scenario Outline: Save - "<Condition>"
        When I select "<Type>" in the "TypeName" picklist field
        And I enter "<Amount>" in the "Amount" input field
        And I click the save button
        Then I should see a confirmation message "<ExpectedResult>"
        Examples:
          | Condition    | Type             | Amount | ExpectedResult             |
          | Basic Save   | PLACEHOLDER_Type | 10     | Details Saved Successfully |
          | Missing type |                  | 10     | Type* Should not be Empty  |
    AFTER (the empty-picklist row moves to its own outline, steps unchanged except
    the picklist line and its column are gone)
      Scenario Outline: Save - "<Condition>"
        ... same four steps ...
        Examples:
          | Condition  | Type             | Amount | ExpectedResult             |
          | Basic Save | PLACEHOLDER_Type | 10     | Details Saved Successfully |
      Scenario Outline: Save without Type - "<Condition>"
        When I enter "<Amount>" in the "Amount" input field
        And I click the save button
        Then I should see a confirmation message "<ExpectedResult>"
        Examples:
          | Condition    | Amount | ExpectedResult            |
          | Missing type | 10     | Type* Should not be Empty |
- Every "<Placeholder>" in a step needs an Examples column with exactly that name.
- Keep Examples tables aligned. Output ONLY Gherkin - no markdown, no commentary.

Defects found:
{findings_text}

Feature file to repair:
{test_cases}
"""
        response = await self.agent.run(user_message)
        text = response.text.strip()
        if text.startswith("```"):  # tolerate a stray Markdown fence
            text = re.sub(r"^```[a-zA-Z]*[ \t]*\r?\n", "", text)
            text = re.sub(r"\r?\n```\s*$", "", text)
        return text.strip()