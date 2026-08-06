import json
from pathlib import Path
import os
from agent_framework import Agent
from agent_framework.openai import OpenAIChatClient
from dotenv import load_dotenv

load_dotenv()


class AppendAgent:

    def __init__(self):
        self.client = OpenAIChatClient(
            model=os.getenv("OPENAI_MODEL")
        )
        self.instructions = Path(
            "prompts/append_prompt.txt").read_text(encoding="utf-8")

        self.agent = Agent(
            name="AppendAgent",
            client=self.client,
            instructions=self.instructions
        )

    async def apply_append(self, feature_file: str, script: str, addition: str):
        message = f"""
Current Feature File:
{feature_file}

Current Cypress Script:
{script}

Addition requested (this may be a full Gherkin scenario the user already
wrote themselves, ready to insert near-verbatim, or a short plain-language
description of new scenario(s) to add):
{addition}
"""
        response = await self.agent.run(message)

        try:
            return json.loads(response.text)
        except json.JSONDecodeError:
            return {"error": "Failed to parse response as JSON", "response": response.text}