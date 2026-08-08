import json
import logging
import os
from pathlib import Path
from typing import Annotated

from agent_framework import Agent, tool
from agent_framework.openai import OpenAIChatClient
from dotenv import load_dotenv

from service import db_service

load_dotenv()

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tools — thin, annotated wrappers around service/db_service.py.
# These are the ONLY way the agent can touch the DB: no raw SQL, and every
# dynamic identifier gets validated against INFORMATION_SCHEMA inside
# db_service before it can reach a query (see db_service.py's module
# docstring for the full reasoning).
#
# Every wrapper catches Exception broadly, not just DbServiceError — a hard
# connection failure (bad TESTDB_HOST, server unreachable, timeout) surfaces
# as a raw pymssql error, not a DbServiceError, and the framework re-raises
# anything a tool throws. Catching narrowly meant a connection problem blew
# up the whole agent.run() on the first tool call instead of reporting back
# cleanly. Now every failure returns {"error": ...} to the model, so it can
# react per its prompt (try another lookup, or give up honestly) — and the
# real exception is logged server-side, since the model only sees a short
# message.
# ---------------------------------------------------------------------------

@tool
def list_tables(
    keyword: Annotated[str | None, "Optional substring to filter table names by, e.g. 'Instrument'. Omit to list broadly."] = None,
    limit: Annotated[int, "Max number of table names to return"] = 50,
) -> str:
    """Lists real table names in the test DB, optionally filtered by a substring keyword. Use this FIRST when you have a plausible keyword but no confirmed table name yet."""
    try:
        return json.dumps(db_service.list_tables(keyword, limit))
    except Exception as e:
        logger.warning("list_tables tool failed: %s", e)
        return json.dumps({"error": str(e)})


@tool
def search_tables_by_column(
    column_keyword: Annotated[str, "Substring to search for within COLUMN names, e.g. 'InstrumentCode'"],
    limit: Annotated[int, "Max number of table/column pairs to return"] = 50,
) -> str:
    """Searches COLUMN names (not table names) for a substring. Use this when the table name itself is opaque or legacy — a form field name tends to survive table renames better than the table name does."""
    try:
        return json.dumps(db_service.search_tables_by_column(column_keyword, limit))
    except Exception as e:
        logger.warning("search_tables_by_column tool failed: %s", e)
        return json.dumps({"error": str(e)})


@tool
def describe_table(
    table_name: Annotated[str, "A table name you have some confidence in, e.g. from list_tables or search_tables_by_column"],
) -> str:
    """Returns a table's columns and its foreign-key relationships in both directions (references / referenced_by), so you can confirm it's the right table and follow FK chains to related lookup data (e.g. a GL account column that references a GLAccounts table)."""
    try:
        return json.dumps(db_service.describe_table(table_name))
    except Exception as e:
        logger.warning("describe_table tool failed: %s", e)
        return json.dumps({"error": str(e)})


@tool
def get_sample_values(
    table_name: Annotated[str, "A table name already confirmed via describe_table"],
    column_name: Annotated[str, "A column name already confirmed via describe_table"],
    limit: Annotated[int, "Max number of distinct real values to return"] = 10,
) -> str:
    """Returns real, currently-valid distinct values for one column. Only call this AFTER describe_table has confirmed the table and column are the right ones."""
    try:
        return json.dumps(db_service.get_sample_values(table_name, column_name, limit))
    except Exception as e:
        logger.warning("get_sample_values tool failed: %s", e)
        return json.dumps({"error": str(e)})


_TOOLS = [list_tables, search_tables_by_column, describe_table, get_sample_values]


class BusinessContextAgent:
    """Explores the real test DB schema (never guesses table/column names
    outright) to find real, currently-valid test data for one screen, using
    source-code-derived hints as a starting point. Returns a flat
    field-name -> [real values] dict, or {"error": ...} if it genuinely
    couldn't find a confident match — callers should treat that as an
    accepted, honest outcome and fall back to placeholder values (per
    test_case_prompt.txt's own existing fallback rule), NOT as a fatal error
    for the whole generate pipeline."""

    def __init__(self):
        self.client = OpenAIChatClient(model=os.getenv("OPENAI_MODEL"))
        self.instructions = Path(
            "prompts/business_context_prompt.txt").read_text(encoding="utf-8")

        self.agent = Agent(
            name="BusinessContextAgent",
            client=self.client,
            instructions=self.instructions,
        )

    async def find_context(self, module: str, screen: str, source_hints: list[str]) -> dict:
        """source_hints: strings pulled from the screen's real source/step
        defs — API route fragments, form field names, data-cy attributes.
        Best-effort signal, not a guarantee (see prompts/business_context_prompt.txt)."""
        user_message = f"""
module: {module}
screen: {screen}

source_hints:
{chr(10).join(f"- {h}" for h in source_hints) if source_hints else "(none available)"}
"""
        try:
            response = await self.agent.run(user_message, tools=_TOOLS)
        except Exception as e:
            logger.warning("BusinessContextAgent call failed: %s", e)
            return {"error": f"agent call failed: {e}"}

        try:
            result = json.loads(response.text)
        except json.JSONDecodeError:
            logger.warning("BusinessContextAgent returned invalid JSON: %s", response.text)
            return {"error": "agent returned invalid JSON"}

        logger.info("BusinessContextAgent.find_context module=%r screen=%r -> %r", module, screen, result)
        return result