import os
import time
from typing import Any, Dict, Optional
from pydantic import BaseModel


class ObservabilityConfig(BaseModel):
    tracing_enabled: bool = False
    project_name: str = "autonomous-swe-platform"
    langchain_api_key: Optional[str] = None


class LangSmithTracer:
    """Configures LangSmith tracing metadata and telemetry."""

    def __init__(self, project_name: str = "autonomous-swe-platform"):
        self.project_name = project_name
        self.api_key = os.environ.get("LANGCHAIN_API_KEY")
        self.is_enabled = os.environ.get("LANGCHAIN_TRACING_V2") == "true" and bool(self.api_key)

    def get_run_metadata(self, task_id: str, agent_role: str, model_name: str) -> Dict[str, Any]:
        return {
            "langsmith_project": self.project_name,
            "tracing_active": self.is_enabled,
            "task_id": task_id,
            "agent_role": agent_role,
            "model_name": model_name,
            "timestamp": time.time(),
        }


def setup_observability() -> ObservabilityConfig:
    api_key = os.environ.get("LANGCHAIN_API_KEY")
    tracing = os.environ.get("LANGCHAIN_TRACING_V2") == "true" and bool(api_key)
    project = os.environ.get("LANGCHAIN_PROJECT", "autonomous-swe-platform")
    return ObservabilityConfig(
        tracing_enabled=tracing,
        project_name=project,
        langchain_api_key=api_key,
    )
