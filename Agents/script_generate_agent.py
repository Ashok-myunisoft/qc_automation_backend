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
       
        model =  os.getenv("OPENAI_MODEL")
        self.client = OpenAIChatClient(model=model)

        with open("prompts/script_generate_prompt.txt", "r", encoding="utf-8") as f:
            self.instructions = f.read()

        self.agent = Agent(
            name="ScriptGenerateAgent",
            client=self.client,
            instructions=self.instructions
        )

    async def generate_script(self, test_cases: str, locator_map: dict[str, str] | None = None) -> str:
        """test_cases: the full Gherkin feature text (as produced by
        TestCaseAgent). locator_map: optional {field_name: real data-cy
        value} distilled from ProjectAnalysisAgent's own output (see
        app.py's _build_locator_map) — VERIFIED selectors, not guesses.
        When a step refers to a field present in this map, the prompt
        instructs the model to use that exact value instead of deriving
        one from the field's name. Returns the complete, self-contained
        .js file content implementing every step in the feature."""
        locator_block = (
            "\n".join(f'- "{name}" -> data-cy="{locator}"' for name, locator in locator_map.items())
            if locator_map else
            "none supplied — derive data-cy from the field/button name as usual."
        )
        user_message = f"""
Feature file to implement (every Given/When/Then below needs a step
definition in your output — this file is the screen's ONLY step file,
nothing else backs it):

{test_cases}

Known real locators (VERIFIED from the actual source code — use these
EXACT data-cy values for any field/button named below that appears in
this list, instead of deriving your own):
{locator_block}

Product default action locators (use these exact data-cy values whenever
the step clearly refers to the matching action, even if analysis output is
missing, `0`, or a placeholder):
- save -> data-cy="SaveForm"
- add new -> data-cy="AddNewForm"
- update -> data-cy="UpdateForm"
- delete -> data-cy="DeleteForm"
- edit -> data-cy="EditForm"
- attachments -> data-cy="FormAttachment"
"""
        response = await self.agent.run(user_message)
        return response.text
