from pathlib import Path
from uuid import uuid4

import pytest

from agents.base import AgentInvocation
from agents.gateway import ToolCall
from apps.worker.nodes import GatewayToolDispatcher
from domain.enums import RiskLevel
from tools.gateway import ToolCallResult, ToolExecutionStatus
from tools.production import ProductionToolSet


@pytest.mark.parametrize("path", ["/workspace/app.py", "../outside.py"])
async def test_invalid_model_path_is_denied_then_a_safe_call_can_recover(tmp_path: Path, path: str):
    ids = dict(run_id=uuid4(), task_id=uuid4(), attempt_id=uuid4())
    tool_set = ProductionToolSet(
        source_repository=tmp_path,
        worktree=tmp_path,
        **ids,
        sandbox=None,
        python_image="python",
        node_image="node",
        uid=1000,
        gid=1000,
    )
    registry = tool_set.registry()
    executed = []

    class ValidatingGateway:
        async def execute(self, request, *, context):
            tool = registry.resolve(request.tool_name, request.tool_version)
            args = tool.validate_arguments(request.arguments, context)
            tool.authorize(context)
            executed.append(args.path)
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                tool_version="1.0",
                status=ToolExecutionStatus.COMPLETED,
                risk=RiskLevel.LOW,
                output={"content": "actual file contents"},
                attempts=1,
            )

    invocation = AgentInvocation(
        **ids,
        trace_id="path-recovery",
        project_id=uuid4(),
        repository_id=uuid4(),
        baseline_commit="a" * 40,
        goal="Inspect source",
        input_payload={"agent_role": "researcher"},
    )
    dispatcher = GatewayToolDispatcher(
        gateway=ValidatingGateway(),
        registry=registry,
        project_id=invocation.project_id,
        repository_id=invocation.repository_id,
        baseline_commit=invocation.baseline_commit,
        worktree=tmp_path,
        agent_capabilities=frozenset({"repository-read"}),
        risk_ceiling=RiskLevel.LOW,
    )
    denied = await dispatcher.dispatch(
        ToolCall(call_id="bad", name="read_file", arguments={"path": path}), invocation=invocation
    )
    assert denied["status"] == "FAILED"
    assert denied["attempts"] == 0
    assert executed == []
    recovered = await dispatcher.dispatch(
        ToolCall(call_id="good", name="read_file", arguments={"path": "app.py"}),
        invocation=invocation,
    )
    assert recovered["status"] == "COMPLETED"
    assert executed == ["app.py"]
