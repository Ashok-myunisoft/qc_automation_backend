import json
import os
from agent_framework import Agent
from agent_framework.openai import OpenAIChatClient
from dotenv import load_dotenv
from pathlib import Path

load_dotenv()


class ValidateAgent:
    def __init__(self):
        self.client = OpenAIChatClient(
            model=os.getenv("OPENAI_MODEL")
        )
        self.validate_prompt = Path(
            "prompts/validate_prompt.txt").read_text(encoding="utf-8")

        self.agent = Agent(
            name="ValidateAgent",
            client=self.client,
            instructions=self.validate_prompt
        )


    async def validate(self, test_cases: str, generated_script: str):

        message = f"""
Validate this test case and generated script. Start with section 0 of your
instructions (QC template block present and unaltered, no duplicate step
definitions, every feature step has a definition, Scenario Outline / Examples
format).

Test Cases:
{test_cases}

Generated Script:
{generated_script}

Return JSON only.
"""

        response = await self.agent.run(message)
        text = response.text.strip()
        if text.startswith("```"):  # tolerate a stray Markdown fence around the JSON
            text = text.strip("`").strip()
            if text.lower().startswith("json"):
                text = text[4:].strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"error": "Failed to decode JSON"}