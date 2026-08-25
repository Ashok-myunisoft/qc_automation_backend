import json
import logging
from pathlib import Path
import os
from agent_framework import Agent
from agent_framework.openai import OpenAIChatClient
from dotenv import load_dotenv


load_dotenv()

logger = logging.getLogger(__name__)

class ProjectAnalysisAgent:
    def __init__(self):
        self.client = OpenAIChatClient(
            model=os.getenv("OPENAI_MODEL")
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
        project_context: {json.dumps(project_context)}
        user_request: {user_request}
        """

        response = await self.agent.run(user_message)
        logger.info("ProjectAnalysisAgent raw response=%s", response.text)

        try:
            return json.loads(response.text)
        except json.JSONDecodeError:
            return {"error": "Failed to parse response as JSON", "response": response.text}
