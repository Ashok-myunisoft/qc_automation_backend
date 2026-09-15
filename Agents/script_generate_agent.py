import json
import logging
import re
import os
from agent_framework import Agent
from agent_framework.openai import OpenAIChatClient
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


_OUTER_JS_FENCE_RE = re.compile(
    r"^```(?:javascript|js)?[ \t]*\r?\n(?P<body>[\s\S]*?)\r?\n```$",
    re.IGNORECASE,
)

def _strip_outer_javascript_fence(text: str) -> str:
    """Accept a model response wrapped in one Markdown JS fence.

    The prompt explicitly forbids fences, but this is a narrow final safety
    net before the content reaches validation, GitLab, or Cypress. It only
    removes a single *outer* fence; it deliberately does not try to repair
    prose, partial code, or malformed JavaScript.
    """
    trimmed = text.strip()
    match = _OUTER_JS_FENCE_RE.fullmatch(trimmed)
    return match.group("body").strip() if match else text


_STEP_DEFINITION_RE = re.compile(
    r"\b(?:Given|When|Then)\(\s*(['\"])(?P<step>.*?)\1\s*,",
    re.DOTALL,
)
_LABEL_DERIVED_SELECTOR_RE = re.compile(
    r"(?:data-cy|dataCy)[^\n`]*\$\{\s*(?:field|checkbox|action|value)\s*\}-"
    r"(?:PickList|ComboBox|CheckBox|Textarea|Form)",
    re.IGNORECASE,
)


def _build_resolved_control_lookup(automation_contract: dict | None) -> dict[str, dict]:
    """Return exact field-name -> verified-control facts for generation."""
    controls: dict[str, dict] = {}
    for field in (automation_contract or {}).get("fields") or []:
        if not isinstance(field, dict):
            continue
        locator = field.get("automation_locator") or {}
        if not (
            locator.get("verified") is True
            and locator.get("strategy") == "data_cy"
            and locator.get("value")
        ):
            continue
        control = {
            "dataCy": locator["value"],
            "interactionType": (field.get("automation_interaction") or {}).get(
                "interaction_type", "Unknown"
            ),
            "target": locator.get("target", "Unknown"),
        }
        for key in (field.get("label"), field.get("control_name")):
            if key and key != "Unknown":
                controls[key] = control
    return controls


def _controls_referenced_by_feature(
    test_cases: str, controls: dict[str, dict]
) -> dict[str, dict]:
    """Collapse label/control-name aliases and keep fields used by the feature."""
    referenced: dict[str, dict] = {}
    for name, control in controls.items():
        if name not in test_cases:
            continue
        referenced.setdefault(control["dataCy"], control)
    return referenced


def _script_output_violations(script: str) -> list[str]:
    """Catch deterministic invalid output before it reaches ValidateAgent."""
    violations = []
    if "```" in script:
        violations.append("contains a Markdown code fence")
    if _LABEL_DERIVED_SELECTOR_RE.search(script):
        violations.append("derives a selector from a step-label parameter")
    steps = [match.group("step") for match in _STEP_DEFINITION_RE.finditer(script)]
    duplicates = sorted({step for step in steps if steps.count(step) > 1})
    if duplicates:
        violations.append("registers duplicate Cucumber steps: " + ", ".join(duplicates))
    return violations


def _interaction_violations(script: str, controls: dict[str, dict]) -> list[str]:
    """Check verified controls against the interaction primitives in output."""
    violations = []
    for name, control in controls.items():
        data_cy = control["dataCy"]
        interaction = str(control.get("interactionType", "")).lower()
        if data_cy not in script:
            violations.append(f'verified locator for "{name}" is missing')
            continue
        local_contexts = []
        for match in re.finditer(re.escape(data_cy), script):
            start = script.rfind("\n", 0, match.start()) + 1
            end = min(len(script), match.end() + 300)
            local_contexts.append(script[start:end])
        local_script = "\n".join(local_contexts)
        if interaction == "picklist":
            if "mat-option" in local_script:
                violations.append(f'picklist "{name}" uses Material mat-option interaction')
            direct_native_select = re.search(
                rf"data-cy\s*=\s*['\"]{re.escape(data_cy)}['\"][^;\n]*\.select\s*\(",
                script,
                re.IGNORECASE,
            )
            if direct_native_select:
                violations.append(f'picklist "{name}" uses native select interaction')
        elif interaction == "material_option" and "mat-option" not in script:
            violations.append(f'material option "{name}" does not use mat-option interaction')
    return violations


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

    async def generate_script(
        self,
        test_cases: str,
        locator_map: dict[str, str] | None = None,
        automation_contract: dict | None = None,
    ) -> str:
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
        contract_block = json.dumps(automation_contract, indent=2) if automation_contract else "none supplied"
        resolved_controls = _build_resolved_control_lookup(automation_contract)
        referenced_controls = _controls_referenced_by_feature(test_cases, resolved_controls)
        resolved_controls_block = json.dumps(resolved_controls, indent=2) if resolved_controls else "none supplied"
        required_locators = ", ".join(
            control["dataCy"] for control in referenced_controls.values()
        ) or "none"
        user_message = f"""
Feature file to implement (every Given/When/Then below needs a step
definition in your output — this file is the screen's ONLY step file,
nothing else backs it):

{test_cases}

Source-derived automation contract (authoritative when present):
{contract_block}

Resolved field-control lookup (authoritative, exact values):
{resolved_controls_block}

Known real locators (VERIFIED from the actual source code — use these
EXACT data-cy values for any field/button named below that appears in
this list, instead of deriving your own):
{locator_block}

Product default action locators (use these only when the source-derived
automation contract and known real locators provide no verified locator for
the matching action):
- save -> data-cy="SaveForm"
- add new -> data-cy="AddNewForm"
- update -> data-cy="UpdateForm"
- delete -> data-cy="DeleteForm"
- edit -> data-cy="EditForm"
- attachments -> data-cy="FormAttachment"
"""
        response = await self.agent.run(user_message)
        script = _strip_outer_javascript_fence(response.text)
        violations = _script_output_violations(script)
        violations.extend(_interaction_violations(script, referenced_controls))
        if not violations:
            return script

        logger.warning("ScriptGenerateAgent output gate failed: %s", violations)
        repair_message = f"""
Repair the JavaScript below. Return only complete raw JavaScript.

CRITICAL REPAIR RULES — read before changing anything:
1. Fix ONLY the listed violations below. Do not rewrite, remove, or restructure
   any part of the script that is not directly causing a listed violation.
2. The resolvedControls object must be preserved EXACTLY as it appears in the
   invalid script — same keys, same dataCy values, same interactionType values.
   Do not add, remove, or rename any entry in resolvedControls.
3. All existing step definitions (Given/When/Then) must be preserved. Only
   modify a step definition if it is directly named in the violations list.
4. Do not derive any selector from a step parameter — always use resolvedControls[field].dataCy.

Violations to fix:
{json.dumps(violations)}

Verified locator strings that MUST appear in the repaired script
(inside resolvedControls — do not omit any):
{required_locators}

Resolved field-control lookup (resolvedControls must match this exactly):
{resolved_controls_block}

Feature file (authoritative — every step must have a handler):
{test_cases}

Automation contract:
{contract_block}

Invalid script to repair:
{script}
"""
        repaired = _strip_outer_javascript_fence((await self.agent.run(repair_message)).text)
        remaining = _script_output_violations(repaired)
        remaining.extend(_interaction_violations(repaired, referenced_controls))
        if remaining:
            logger.error("ScriptGenerateAgent repair output still fails gate: %s", remaining)
            raise ValueError("ScriptGenerateAgent output failed deterministic gate after repair: " + "; ".join(remaining))
        return repaired