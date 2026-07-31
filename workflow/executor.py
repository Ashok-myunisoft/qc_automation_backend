import logging

from agent_framework import Executor, WorkflowContext, handler

from Agents.planner_agent import PlannerAgent
from Agents.project_analyze_agent import ProjectAnalysisAgent
from Agents.test_case_agent import TestCaseAgent
from Agents.script_generate_agent import ScriptGenerateAgent
from Agents.validate_agent import ValidateAgent
from workflow.context import WorkflowState

logger = logging.getLogger(__name__)


class PlannerExecutor(Executor):
    def __init__(self):
        super().__init__(id="planner_executor")
        self.agent = PlannerAgent()

    @handler
    async def plan(self, state: WorkflowState, ctx: WorkflowContext[WorkflowState]):
        logger.info("Starting Planning")
        result = await self.agent.plan(user_request=state.user_request)
        state.plan = result
        logger.info("Planning completed")
        logger.debug(f"Plan: {state.plan}")
        await ctx.send_message(state)


class ProjectAnalysisExecutor(Executor):
    def __init__(self):
        super().__init__(id="project_analysis_executor")
        self.agent = ProjectAnalysisAgent()

    @handler
    async def analyze_project(self, state: WorkflowState, ctx: WorkflowContext[WorkflowState]):
        logger.info("Starting Project Analysis")
        result = await self.agent.analyze(
            project_context=state.project_context,
            user_request=state.user_request,
        )
        state.project_analysis = result
        logger.info("Project Analysis completed")
        logger.debug(f"Project Analysis: {state.project_analysis}")
        await ctx.send_message(state)


class TestCaseExecutor(Executor):
    def __init__(self):
        super().__init__(id="test_case_executor")
        self.agent = TestCaseAgent()

    @handler
    async def generate_test_cases(self, state: WorkflowState, ctx: WorkflowContext[WorkflowState]):
        logger.info("Starting Test Case Generation")
        state.current_step = "generate_test_cases"
        result = await self.agent.generate_test_cases(
            project_analysis=state.project_analysis,
            user_request=state.user_request,
            business_context=state.business_context,
        )
        state.test_cases = result
        logger.info("Test cases generated")
        logger.debug(f"Generated Test Cases: {state.test_cases}")
        await ctx.send_message(state)


class ScriptGenerateExecutor(Executor):
    def __init__(self):
        super().__init__(id="script_generate_executor")
        self.agent = ScriptGenerateAgent()

    @handler
    async def generate_script(self, state: WorkflowState, ctx: WorkflowContext[WorkflowState]):
        logger.info("Starting Script Generation")
        state.current_step = "generate_script"
        result = await self.agent.generate_script(test_cases=state.test_cases)
        state.generated_script = result
        logger.info("Script generated")
        logger.debug(f"Generated Script: {state.generated_script}")
        await ctx.send_message(state)


class ValidateExecutor(Executor):
    def __init__(self):
        super().__init__(id="validate_executor")
        self.agent = ValidateAgent()

    @handler
    async def validate(self, state: WorkflowState, ctx: WorkflowContext[WorkflowState]):
        logger.info("Starting Validation")
        state.current_step = "validate"
        result = await self.agent.validate(
            generated_script=state.generated_script,
            test_cases=state.test_cases,
        )
        state.validation_result = result
        logger.info("Validation completed")
        logger.debug(f"Validation Result: {state.validation_result}")
        await ctx.send_message(state)