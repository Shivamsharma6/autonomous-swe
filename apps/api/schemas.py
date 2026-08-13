from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field
from agents.base import ModelProviderConfig


class ProjectCreateReq(BaseModel):
    name: str
    repo_path: str = ""
    description: str = ""
    project_id: Optional[str] = None


class TaskCreateReq(BaseModel):
    project_id: str
    user_request: str
    description: str = ""
    task_id: Optional[str] = None
    model_provider: Optional[ModelProviderConfig] = None
