import json
from pathlib import Path
from agent_framework import Agent
from agent_framework.openai import OpenAIChatClient
from dotenv import load_dotenv

load_dotenv()


class TestCaseAgent:
    def __init__(self):
        self.client = OpenAIChatClient(
            model="gpt-4o-mini"
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
        business_context: dict = None
    ):    
        user_message = f"""
User Request:
{user_request}

Project Analysis:
{json.dumps(project_analysis, indent=2)}

Business Context:
{json.dumps(business_context, indent=2) if business_context else "No business context provided."}
"""

        response = await self.agent.run(user_message)    
        return response.text
  