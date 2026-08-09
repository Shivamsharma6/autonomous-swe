from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class TaskStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    LEASED = "leased"
    RUNNING = "running"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class AgentSpec(BaseModel):
    name: str
    role: str
    description: str = ""
    system_prompt: str = ""
    tools: List[str] = Field(default_factory=list)
    model: str = "gpt-4o"
    risk_level: RiskLevel = RiskLevel.LOW


class ToolCallRequest(BaseModel):
    call_id: str
    tool_name: str
    arguments: Dict[str, Any] = Field(default_factory=dict)
    requested_by: str


class ToolCallResult(BaseModel):
    call_id: str
    tool_name: str
    output: Any = None
    error: Optional[str] = None
    is_success: bool = True


class IdempotencyRecord(BaseModel):
    key: str
    result: Any = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    status: str = "completed"


class TaskNode(BaseModel):
    id: str
    title: str = ""
    name: str = ""
    description: str = ""
    assigned_agent: Optional[str] = None
    status: TaskStatus = TaskStatus.PENDING
    dependencies: List[str] = Field(default_factory=list)
    risk_level: RiskLevel = RiskLevel.LOW
    tool_calls: List[ToolCallRequest] = Field(default_factory=list)

    def model_post_init(self, __context: Any) -> None:
        if not self.title and self.name:
            self.title = self.name
        elif not self.name and self.title:
            self.name = self.title


class WorkflowState(BaseModel):
    workflow_id: str
    task_nodes: Dict[str, TaskNode] = Field(default_factory=dict)
    current_step: int = 0
    status: TaskStatus = TaskStatus.PENDING
    metadata: Dict[str, Any] = Field(default_factory=dict)
    idempotency_records: Dict[str, IdempotencyRecord] = Field(default_factory=dict)
