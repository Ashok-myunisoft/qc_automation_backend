import json
import logging
import os
from pathlib import Path
from agent_framework import Agent
from agent_framework.openai import OpenAIChatClient
from dotenv import load_dotenv
from service.git_service import fetch_qc_repo_tree, fetch_qc_file_by_path

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
            instructions=self.instructions
        )

    async def find_files(self, module: str, screen: str) -> dict:
        """
        Given a module name and screen name, fetches the QC test repo tree
        and uses the agent to identify the correct .feature and .cy.js paths.
        Then fetches their content and returns everything.

        Returns:
        {
            "feature_path": "features/crm/lead_management.feature",
            "feature_content": "<full .feature content>",
            "script_path": "cypress/e2e/crm/lead_management.cy.js",
            "script_content": "<full .cy.js content>",
        }
        Or on failure:
        {
            "error": "reason"
        }
        """

        # Step 1 — fetch the full QC repo file tree (paths only)
        all_paths = fetch_qc_repo_tree()

        if not all_paths:
            return {"error": "Could not fetch QC repo tree from GitHub"}

        # Step 2 — ask agent to identify the right paths
        user_message = f"""
Module: {module}
Screen: {screen}

QC test repo file paths:
{chr(10).join(all_paths)}
"""

        logger.info("ArchitectureAgent: calling agent.run()...")
        response = await self.agent.run(user_message)
        logger.info("ArchitectureAgent: agent.run() returned")

        try:
            identified = json.loads(response.text)
            logger.info(f"ArchitectureAgent: parsed JSON = {identified}")
        except json.JSONDecodeError:
            return {"error": f"Agent returned invalid JSON: {response.text}"}

        feature_path = identified.get("feature_path")
        script_path  = identified.get("script_path")

        if not feature_path or not script_path:
            return {"error": f"Agent could not identify files for {module} > {screen}"}

        # Step 3 — fetch the actual file contents
        logger.info(f"ArchitectureAgent: fetching feature file {feature_path}...")
        feature_content = fetch_qc_file_by_path(feature_path)
        logger.info("ArchitectureAgent: feature file fetched")

        logger.info(f"ArchitectureAgent: fetching script file {script_path}...")
        script_content  = fetch_qc_file_by_path(script_path)
        logger.info("ArchitectureAgent: script file fetched")

        if not feature_content:
            return {"error": f"File not found in repo: {feature_path}"}

        if not script_content:
            return {"error": f"File not found in repo: {script_path}"}

        return {
            "feature_path":    feature_path,
            "feature_content": feature_content,
            "script_path":     script_path,
            "script_content":  script_content,
        }