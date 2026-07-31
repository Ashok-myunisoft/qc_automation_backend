class WorkflowState:
    def __init__(self):
        self.plan = None
        self.project_context = {}
        self.user_request = ""
        self.business_context = ""
        self.project_analysis = None
        self.test_cases = None
        self.generated_script = None
        self.validation_result = None
        self.current_step = ""