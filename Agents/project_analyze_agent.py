import json
import logging
import os
import re
from pathlib import Path

from agent_framework import Agent
from agent_framework.openai import OpenAIChatClient
from dotenv import load_dotenv


load_dotenv()

from service.screen_spec import rules_for as _spec_rules_for  # screen spec: extra rules only when a spec exists

logger = logging.getLogger(__name__)

# MM screens send ~100K+ tokens of source and ask for a very long JSON answer.
# They run on a 1M-token model by default (no .env change needed). Override with
# MM_ANALYSIS_MODEL in .env only if you ever want a different one.
_MM_DEFAULT_MODEL = "gpt-4.1-mini"

_MM_OUTPUT_RULES = """

IMPORTANT - OUTPUT RULES FOR THIS LARGE SCREEN (they override any habit of shortening):
1. Return ONE complete, valid JSON object. Never write "..." or any placeholder or comment inside the JSON.
2. The screen's biz_transaction_class_id is __CLASS_ID__. Include ONLY the fields, columns, buttons and actions that are visible for this ClassId. Leave out anything shown only for other ClassIds - do not list it with a "filtered out" note.
3. Keep the reply compact: at most ONE source_evidence entry per item, with symbol_or_markup of at most 120 characters copied EXACTLY from the source. If you cannot copy it exactly, set that item's source_evidence to [].
4. tables[].columns and tables[].editable_columns must be JSON arrays of objects (one per grid column you can actually find in the source). If you cannot find any, return []. Never write a sentence there.
5. Do not repeat identical entries in rest_apis.
"""

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.I)
_ELLIPSIS_EMPTY_RE = re.compile(r"([\[{])\s*\.\.\.\s*([\]}])")   # [ ... ]  /  { ... }
_ELLIPSIS_TAIL_RE = re.compile(r",\s*\.\.\.\s*([\]}])")          # , ... ]
_TRAILING_COMMA_RE = re.compile(r",(\s*[\]}])")


def _parse_json_lenient(text: str):
    """json.loads, plus a small repair for the typical model slips: markdown
    fences, "[ ... ]" placeholders and trailing commas. Raises JSONDecodeError
    if the text still is not valid JSON afterwards."""
    try:
        return json.loads(text), False
    except json.JSONDecodeError:
        pass
    cleaned = _FENCE_RE.sub("", text.strip())
    cleaned = _ELLIPSIS_EMPTY_RE.sub(r"\1\2", cleaned)
    cleaned = _ELLIPSIS_TAIL_RE.sub(r"\1", cleaned)
    cleaned = _TRAILING_COMMA_RE.sub(r"\1", cleaned)
    return json.loads(cleaned), True


def _is_mm_context(project_context) -> bool:
    return isinstance(project_context, dict) and "biz_transaction_screen" in project_context


class ProjectAnalysisAgent:
    def __init__(self):
        # ANALYSIS_OPENAI_MODEL lets ProjectAnalysisAgent run a different
        # (stronger) OpenAI model than the other agents, without touching
        # OPENAI_MODEL shared by everything else. Falls back to OPENAI_MODEL
        # if ANALYSIS_OPENAI_MODEL isn't set, so nothing breaks if it's
        # omitted from .env.   (Regular screens: unchanged.)
        self.client = OpenAIChatClient(
            model=os.getenv("ANALYSIS_OPENAI_MODEL", os.getenv("OPENAI_MODEL"))
        )
        self.instructions = Path(
            "prompts/project_analysis_prompt.txt").read_text(encoding="utf-8")

        self.agent = Agent(
            name="ProjectAnalysisAgent",
            client=self.client,
            instructions=self.instructions
        )

        # MM screens: separate agent on a large-context model, built on first use.
        self._mm_model = os.getenv("MM_ANALYSIS_MODEL", _MM_DEFAULT_MODEL)
        self._mm_agent = None
        self._mm_agent_failed = False

    def _get_mm_agent(self):
        if self._mm_agent is None and not self._mm_agent_failed:
            try:
                self._mm_agent = Agent(
                    name="ProjectAnalysisAgentMM",
                    client=OpenAIChatClient(model=self._mm_model),
                    instructions=self.instructions,
                )
            except Exception as e:  # pragma: no cover
                logger.warning("Could not create MM analysis agent (%s): %s - using the regular model", self._mm_model, e)
                self._mm_agent_failed = True
        return self._mm_agent

    async def _run(self, is_mm: bool, message: str) -> str:
        if is_mm:
            mm_agent = self._get_mm_agent()
            if mm_agent is not None:
                try:
                    logger.info("ProjectAnalysisAgent: MM screen -> model=%s", self._mm_model)
                    return (await mm_agent.run(message)).text
                except Exception as e:
                    logger.warning(
                        "MM analysis on model %s failed (%s) - falling back to the regular analysis model",
                        self._mm_model, e,
                    )
        return (await self.agent.run(message)).text

    async def analyze(self, project_context: dict, user_request: str):
        is_mm = _is_mm_context(project_context)

        user_message = f"""
        project_context: {json.dumps(project_context)}
        user_request: {user_request}
        """
        if not is_mm and isinstance(project_context, dict) and project_context.get("screen_spec"):
            user_message += "\n\n" + _spec_rules_for("ANALYSIS")
        if is_mm:
            user_message += _MM_OUTPUT_RULES.replace(
                "__CLASS_ID__", str(project_context.get("biz_transaction_class_id")))

        text = await self._run(is_mm, user_message)
        logger.info("ProjectAnalysisAgent raw response=%s", text)

        try:
            result, repaired = _parse_json_lenient(text)
            if repaired:
                logger.warning("ProjectAnalysisAgent: JSON needed repair (placeholders / trailing commas) - repaired OK")
            return result
        except json.JSONDecodeError as first_error:
            first_error_text = str(first_error)  # keep it: Python deletes `first_error` after this block
            logger.warning("ProjectAnalysisAgent: invalid JSON (%s) - retrying once", first_error_text)

        retry_message = user_message + (
            "\n\nIMPORTANT: your previous reply was NOT valid JSON (" + first_error_text[:200] + "). "
            "Reply again with the COMPLETE JSON object only. Do not use '...' or any placeholder anywhere; "
            "fill every array with real content or leave it []. No markdown, no comments."
        )
        try:
            text = await self._run(is_mm, retry_message)
            logger.info("ProjectAnalysisAgent retry raw response=%s", text)
            result, _ = _parse_json_lenient(text)
            return result
        except json.JSONDecodeError:
            return {"error": "Failed to parse response as JSON", "response": text}
        except Exception as e:  # e.g. rate limit on the second big request
            logger.warning("ProjectAnalysisAgent retry failed: %s", e)
            return {"error": "Failed to parse response as JSON", "response": text}