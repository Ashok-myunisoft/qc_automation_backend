import json
import logging
import os
from pathlib import Path
from agent_framework import Agent
from agent_framework.openai import OpenAIChatClient
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


class ArchitectureAgent:
    def __init__(self):
        self.client = OpenAIChatClient(model=os.getenv("OPENAI_MODEL"))
        self.instructions = Path(
            "prompts/architecture_prompt.txt").read_text(encoding="utf-8")

        self.agent = Agent(
            name="ArchitectureAgent",
            client=self.client,
            instructions=self.instructions,
        )

    async def _ask(self, user_message: str) -> dict:
        try:
            response = await self.agent.run(user_message)
        except Exception as e:
            logger.warning("ArchitectureAgent call failed: %s", e)
            return {"error": f"agent call failed: {e}"}

        try:
            return json.loads(response.text)
        except json.JSONDecodeError:
            logger.warning("ArchitectureAgent returned invalid JSON: %s", response.text)
            return {"error": "agent returned invalid JSON"}

    async def resolve_screen(self, tree: list[str], module: str, screen: str, repo_kind: str) -> dict:
        logger.info(
            "ArchitectureAgent.resolve_screen called with module=%r screen=%r repo_kind=%r (%d tree entries)",
            module, screen, repo_kind, len(tree),
        )
        user_message = f"""
repo_kind: {repo_kind}
scope: screen
module: {module}
screen: {screen}

repo file paths:
{chr(10).join(tree)}
"""
        result = await self._ask(user_message)
        logger.info("ArchitectureAgent.resolve_screen raw result for module=%r screen=%r: %r", module, screen, result)
        return result

    async def resolve_module(self, tree: list[str], module: str, repo_kind: str) -> dict:
        logger.info(
            "ArchitectureAgent.resolve_module called with module=%r repo_kind=%r (%d tree entries)",
            module, repo_kind, len(tree),
        )
        user_message = f"""
repo_kind: {repo_kind}
scope: module
module: {module}

repo file paths:
{chr(10).join(tree)}
"""
        result = await self._ask(user_message)
        logger.info("ArchitectureAgent.resolve_module raw result for module=%r: %r", module, result)
        return result