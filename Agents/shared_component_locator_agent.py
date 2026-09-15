import json
import logging
import os
import re
from pathlib import Path

from agent_framework import Agent
from agent_framework.openai import OpenAIChatClient
from dotenv import load_dotenv


load_dotenv()

logger = logging.getLogger(__name__)

_OUTER_JSON_FENCE_RE = re.compile(
    r"^```(?:json)?[ \t]*\r?\n(?P<body>[\s\S]*?)\r?\n```$",
    re.IGNORECASE,
)


def _parse_response_json(text: str) -> dict:
    """Accept a JSON response with or without one accidental code fence."""
    stripped = text.strip()
    match = _OUTER_JSON_FENCE_RE.fullmatch(stripped)
    return json.loads(match.group("body") if match else stripped)


class SharedComponentLocatorAgent:
    """Resolves only shared controls left unresolved by screen analysis."""

    def __init__(self):
        self.client = OpenAIChatClient(model=os.getenv("ANALYSIS_OPENAI_MODEL"))
        self.instructions = Path(
            "prompts/shared_component_locator_prompt.txt"
        ).read_text(encoding="utf-8")
        self.agent = Agent(
            name="SharedComponentLocatorAgent",
            client=self.client,
            instructions=self.instructions,
        )

    async def resolve(self, resolution_context: dict) -> dict:
        response = await self.agent.run(
            "Shared-component locator resolution context:\n"
            + json.dumps(resolution_context, indent=2)
        )
        logger.info("SharedComponentLocatorAgent raw response=%s", response.text)
        try:
            return _parse_response_json(response.text)
        except json.JSONDecodeError:
            return {"resolved_fields": [], "error": "Failed to parse response as JSON"}
