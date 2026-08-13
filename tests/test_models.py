import pytest
from datetime import datetime
from policies.risk.policy_engine import RiskLevel
from execution.scheduler.scheduler import TaskStatus, TaskNode
from agents.base import AgentSpec, ModelProviderConfig
from tools.base import ToolCallRequest, ToolCallResult
from knowledge.memory.storage import IdempotencyRecord
from workflows.feature import WorkflowState


def test_risk_level_enum():
    assert RiskLevel.LOW == "low"
    assert RiskLevel.MEDIUM == "medium"
    assert RiskLevel.HIGH == "high"
    assert RiskLevel.CRITICAL == "critical"


def test_task_status_enum():
    assert TaskStatus.PENDING == "pending"
    assert TaskStatus.IN_PROGRESS == "in_progress"
    assert TaskStatus.COMPLETED == "completed"
    assert TaskStatus.FAILED == "failed"
    assert TaskStatus.BLOCKED == "blocked"
    assert TaskStatus.CANCELLED == "cancelled"


def test_agent_spec_model():
    agent = AgentSpec(
        name="coder",
        role="Developer",
        system_prompt="Write code",
        tools=["read_file", "write_file"],
        risk_level=RiskLevel.MEDIUM,
    )
    assert agent.name == "coder"
    assert agent.role == "Developer"
    assert agent.system_prompt == "Write code"
    assert agent.tools == ["read_file", "write_file"]
    assert agent.risk_level == RiskLevel.MEDIUM
    assert agent.model is not None


def test_tool_call_request_model():
    req = ToolCallRequest(
        call_id="call-123",
        tool_name="read_file",
        arguments={"path": "main.py"},
        requested_by="coder",
    )
    assert req.call_id == "call-123"
    assert req.tool_name == "read_file"
    assert req.arguments == {"path": "main.py"}
    assert req.requested_by == "coder"


def test_tool_call_result_model():
    res = ToolCallResult(
        call_id="call-123",
        tool_name="read_file",
        output="file content",
        is_success=True,
    )
    assert res.call_id == "call-123"
    assert res.output == "file content"
    assert res.error is None
    assert res.is_success is True


def test_idempotency_record_model():
    record = IdempotencyRecord(
        key="op-456",
        result={"status": "ok"},
    )
    assert record.key == "op-456"
    assert record.result == {"status": "ok"}
    assert isinstance(record.created_at, datetime)


def test_task_node_model():
    node = TaskNode(
        id="task-1",
        title="Setup project",
        description="Initialize repository structure",
        assigned_agent="coder",
        dependencies=[],
        risk_level=RiskLevel.LOW,
    )
    assert node.id == "task-1"
    assert node.title == "Setup project"
    assert node.status == TaskStatus.PENDING
    assert node.dependencies == []
    assert node.risk_level == RiskLevel.LOW
    assert node.tool_calls == []


def test_workflow_state_model():
    state: WorkflowState = {
        "workflow_id": "wf-99",
        "task_id": "task-1",
        "user_request": "Setup project",
        "workflow_status": "PENDING",
    }
    assert state["workflow_id"] == "wf-99"
    assert state["workflow_status"] == "PENDING"


def test_model_provider_config():
    config = ModelProviderConfig(
        provider="custom",
        model_name="qwen2.5-coder",
        base_url="http://localhost:8080/v1",
        api_key="",
        temperature=0.2,
    )
    assert config.provider == "custom"
    assert config.model_name == "qwen2.5-coder"
    assert config.base_url == "http://localhost:8080/v1"
    assert config.api_key == ""
