import json
from pathlib import Path
import os
from agent_framework import Agent
from agent_framework.openai import OpenAIChatClient
from dotenv import load_dotenv

load_dotenv()

class PlannerAgent:
    def __init__(self):
        self.client = OpenAIChatClient(
            model=os.getenv("OPENAI_MODEL")
        )
        self.instructions = Path(
            "prompts/planner_prompt.txt").read_text(encoding="utf-8")

        self.agent = Agent(
            name="PlannerAgent",
            client=self.client,
            instructions=self.instructions
        )


    async def plan(self, user_request: str):
        user_message = f"""
User Request:
{user_request}
        """

        response = await self.agent.run(user_message)

        try:
            return json.loads(response.text)
        except json.JSONDecodeError:
            return {"error": "Failed to parse response as JSON", "response": response.text}          
        
       
       
   