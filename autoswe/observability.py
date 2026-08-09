# autoswe/observability.py
import os
import time
from typing import Dict, Any, Optional, List
from pydantic import BaseModel, Field

class ObservabilityConfig(BaseModel):
    tracing_enabled: bool = False
    project_name: str = "autonomous-swe-platform"
    langchain_api_key: Optional[str] = None

class TokenCostTracker(BaseModel):
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_cost_usd: float = 0.0

    # Standard model pricing rates ($ per 1K tokens)
    PRICING_TABLE: Dict[str, Dict[str, float]] = Field(default_factory=lambda: {
        "gemini-3.6-flash": {"prompt": 0.0001, "completion": 0.0004},
        "gemini-1.5-pro": {"prompt": 0.00125, "completion": 0.005},
        "gpt-4o": {"prompt": 0.0025, "completion": 0.010},
        "gpt-4o-mini": {"prompt": 0.00015, "completion": 0.0006},
        "claude-3-5-sonnet": {"prompt": 0.003, "completion": 0.015},
    })

    def record_usage(self, model_name: str, prompt_tokens: int, completion_tokens: int) -> float:
        self.total_prompt_tokens += prompt_tokens
        self.total_completion_tokens += completion_tokens
        
        rates = self.PRICING_TABLE.get(model_name, {"prompt": 0.0005, "completion": 0.0015})
        cost = (prompt_tokens / 1000.0 * rates["prompt"]) + (completion_tokens / 1000.0 * rates["completion"])
        self.total_cost_usd += cost
        return cost

class LangSmithTracer:
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
            "timestamp": time.time()
        }

def setup_observability() -> ObservabilityConfig:
    api_key = os.environ.get("LANGCHAIN_API_KEY")
    tracing = os.environ.get("LANGCHAIN_TRACING_V2") == "true" and bool(api_key)
    project = os.environ.get("LANGCHAIN_PROJECT", "autonomous-swe-platform")
    return ObservabilityConfig(
        tracing_enabled=tracing,
        project_name=project,
        langchain_api_key=api_key
    )
