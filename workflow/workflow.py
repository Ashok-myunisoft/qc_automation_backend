from agent_framework import WorkflowBuilder

from workflow.executor import (
    ProjectAnalysisExecutor,
    TestCaseExecutor,
    ScriptGenerateExecutor,
    ValidateExecutor,
)


class QCWorkflow:
    def build(self):
        analysis   = ProjectAnalysisExecutor()
        testcase   = TestCaseExecutor()
        script     = ScriptGenerateExecutor()
        validation = ValidateExecutor()

        builder = WorkflowBuilder(start_executor=analysis)
        builder.add_edge(analysis,   testcase)
        builder.add_edge(testcase,   script)
        builder.add_edge(script,     validation)

        return builder.build()