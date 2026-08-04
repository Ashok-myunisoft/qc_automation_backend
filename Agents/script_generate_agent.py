import os
from agent_framework import Agent
from agent_framework.openai import OpenAIChatClient
from dotenv import load_dotenv

load_dotenv()


class ScriptGenerateAgent:
    """Writes a COMPLETE, self-contained Cypress step-definition file for one
    screen's entire feature — every Given/When/Then in it, not a subset.
    No shared step library, no REUSE-FIRST filtering: this screen's script
    depends on nothing else, so it must implement everything the feature
    actually uses."""

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

    async def generate_script(self, test_cases: str) -> str:
        """test_cases: the full Gherkin feature text (as produced by
        TestCaseAgent). Returns the complete, self-contained .js file
        content implementing every step in it."""
        user_message = f"""
Feature file to implement (every Given/When/Then below needs a step
definition in your output — this file is the screen's ONLY step file,
nothing else backs it):

{test_cases}
"""
        response = await self.agent.run(user_message)
        return response.text
