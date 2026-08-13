from typing import Any, Dict, Optional, TypedDict


class BaseWorkflowState(TypedDict, total=False):
    workflow_id: str
    task_id: str
    project_id: str
    user_request: str
    current_node: str
    workflow_status: str
    metadata: Dict[str, Any]
