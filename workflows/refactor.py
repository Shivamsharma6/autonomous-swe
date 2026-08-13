from typing import Any, Dict, Optional
from workflows.feature import WorkflowOrchestrator


class RefactorWorkflow(WorkflowOrchestrator):
    """Specialized code refactoring workflow graph."""

    def run_refactor(
        self,
        target_module: str,
        project_id: str = "default_project",
        task_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.run_workflow(
            user_request=f"Refactor module: {target_module}",
            project_id=project_id,
            task_id=task_id,
        )
