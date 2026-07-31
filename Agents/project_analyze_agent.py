import json
from pathlib import Path
from agent_framework import Agent
from agent_framework.openai import OpenAIChatClient
from dotenv import load_dotenv

load_dotenv()

class ProjectAnalysisAgent:
    def __init__(self):
        self.client = OpenAIChatClient(
            model="gpt-4o-mini"
        )
        self.instructions = Path(
            "prompts/project_analysis_prompt.txt").read_text(encoding="utf-8")

        self.agent = Agent(
            name="ProjectAnalysisAgent",
            client=self.client,
            instructions=self.instructions
        )


    async def analyze(self, project_context: dict, user_request: str):
        user_message = f"""
        project_context: {json.dumps(project_context, indent=2) }
        user_request: {user_request}
        """

        response = await self.agent.run(user_message)

        try:
            return json.loads(response.text)
        except json.JSONDecodeError:
            return {"error": "Failed to parse response as JSON", "response": response.text}          
        
       
       
   