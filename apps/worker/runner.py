import asyncio
import os
import time
from typing import Any, Dict, Optional
from knowledge.memory.storage import StorageEngine
from workflows.feature import WorkflowOrchestrator


class WorkerRunner:
    """Worker process consumer for handling queued tasks."""

    def __init__(self, storage_engine: Optional[StorageEngine] = None):
        self.storage = storage_engine or StorageEngine()

    def run_task(self, task_id: str, project_id: str, user_request: str):
        workspace = os.path.abspath(os.path.join("autonomous_agent_directory", task_id))
        orchestrator = WorkflowOrchestrator(storage_engine=self.storage, workspace_path=workspace)
        return orchestrator.run_workflow(user_request=user_request, project_id=project_id, task_id=task_id)


if __name__ == "__main__":
    print("Autonomous SWE Worker process runner initialized.")
