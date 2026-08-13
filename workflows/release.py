from typing import Any, Dict, Optional
from workflows.feature import WorkflowOrchestrator


class ReleaseWorkflow(WorkflowOrchestrator):
    """Specialized release engineering & pull request generation workflow graph."""

    def run_release(
        self,
        release_version: str,
        project_id: str = "default_project",
        task_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.run_workflow(
            user_request=f"Prepare release: {release_version}",
            project_id=project_id,
            task_id=task_id,
        )
