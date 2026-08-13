from typing import Any, Dict, Optional
from workflows.feature import WorkflowOrchestrator


class BugfixWorkflow(WorkflowOrchestrator):
    """Specialized bugfix & self-healing workflow graph."""

    def run_bugfix(
        self,
        bug_description: str,
        project_id: str = "default_project",
        task_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.run_workflow(
            user_request=f"Fix Bug: {bug_description}",
            project_id=project_id,
            task_id=task_id,
        )
