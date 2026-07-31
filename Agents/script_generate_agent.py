import json
import os
from agent_framework import Agent
from agent_framework.openai import OpenAIChatClient
from dotenv import load_dotenv

load_dotenv()

class ScriptGenerateAgent:
    def __init__(self):
        self.client = OpenAIChatClient(
            model=os.getenv("OPENAI_MODEL")
        )

        with open("prompts/script_generate_prompt.txt", "r", encoding="utf-8") as f:
            self.instructions = f.read()

        self.agent = Agent(
            name="ScriptGenerateAgent",
            client=self.client,
            instructions=self.instructions
        )

    async def generate_script(self, test_cases: str):
        test_case_message = f"""
test_cases: {test_cases}
"""
        response = await self.agent.run(test_case_message)
        return response.text

 