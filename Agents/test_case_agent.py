import json
import os
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
present in the Project Analysis.
"""

        response = await self.agent.run(user_message)
        return response.text