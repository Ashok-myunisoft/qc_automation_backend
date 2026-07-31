import json
from pathlib import Path
import os
from agent_framework import Agent
from agent_framework.openai import OpenAIChatClient
from dotenv import load_dotenv

load_dotenv()


class InterruptAgent:
    def __init__(self):
        self.client = OpenAIChatClient(
            model=os.getenv("OPENAI_MODEL")
        )
        self.instructions = Path(
            "prompts/interrupt_prompt.txt").read_text(encoding="utf-8")

        self.agent = Agent(
            name="InterruptAgent",
            client=self.client,
            instructions=self.instructions
        )

    async def apply_change(self, feature_file: str, script: str, note: str):
        message = f"""
Current Feature File:
{feature_file}

Current Cypress Script:
{script}

Requested change:
{note}
"""
        response = await self.agent.run(message)

        try:
            return json.loads(response.text)
        except json.JSONDecodeError:
            return {"error": "Failed to parse response as JSON", "response": response.text}
