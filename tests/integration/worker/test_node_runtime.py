import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from agents.base import AgentBudgetExceeded, InMemoryUsageRecorder
from agents.gateway import FailureClass, GatewayError, ModelResponse, ModelUsage, ToolCall
from apps.worker.nodes import ProductionNodeExecutor
from domain.enums import RiskLevel, TaskType
from persistence.tables import AuditEventRow, OutboxRow, WorkflowNodeExecutionRow
from tests.integration.messaging.helpers import seed_task
from tools.gateway import ToolCallResult, ToolExecutionStatus
from tools.production import ProductionToolSet
from workflows.state import NodeExecutionRequest


def request_for(ids):
    return NodeExecutionRequest(
        **ids, trace_id="node-budget-regression", baseline_commit="b" * 40,
        task_type=TaskType.IMPLEMENTATION, node_name="implement",
        goal="Read the related files, implement the assigned change, and report actual evidence.",
        idempotency_key=f"node-runtime:{ids['attempt_id']}",
    )


class EmptyMemory:
    async def get_context(self, request):
        return SimpleNamespace(rendered="", memories=())


def executor_for(tmp_path, ids, *, database=None, gateway=None):
    tools = ProductionToolSet(
        source_repository=tmp_path, worktree=tmp_path,
        run_id=ids["run_id"], task_id=ids["task_id"], attempt_id=ids["attempt_id"],
        sandbox=None, python_image="python", node_image="node", uid=1000, gid=1000,
    )
    return ProductionNodeExecutor(
        database=database, memory=EmptyMemory(), model_gateway=gateway, tool_set=tools,
        project_id=ids["project_id"], repository_id=ids["repository_id"],
        baseline_commit="b" * 40, allowed_tools=("read_file", "apply_patch", "run_tests"),
        assigned_capability="implementation", risk_ceiling=RiskLevel.MEDIUM,
        primary_model="local-model", fallback_models=(), artifacts=None,
    )


@pytest.mark.parametrize("input_tokens, should_finish", [(3200, True), (6000, False)])
async def test_multi_file_node_can_finish_within_shared_budget_but_not_exceed_it(
    tmp_path, monkeypatch, input_tokens, should_finish,
):
    ids = {name: uuid4() for name in (
        "run_id", "task_id", "attempt_id", "project_id", "repository_id",
    )}
    (tmp_path / "pyproject.toml").write_text('[project]\nname="fixture"\nversion="0.1.0"\n')
    (tmp_path / "requirements.txt").write_text("")

    class MultiFileGateway:
        calls = 0

        async def complete(self, request, *, cancel=None):
            self.calls += 1
            tool_calls = ()
            output = None
            if self.calls <= 7:
                tool_calls = (ToolCall(
                    call_id=f"read-{self.calls}", name="read_file",
                    arguments={"path": f"part-{self.calls}.py"},
                ),)
            elif self.calls == 8:
                tool_calls = (ToolCall(
                    call_id="write", name="apply_patch",
                    arguments={"path": "app.py", "content": "print('fixed')\n"},
                ),)
            else:
                output = {"summary": "Implemented after reading related source.",
                          "changed_paths": ["app.py"]}
            return ModelResponse(
                trace_id=request.trace_id, model=request.model, tool_calls=tool_calls,
                finish_reason="tool_calls" if tool_calls else "stop",
                structured_output=output, usage=ModelUsage(input_tokens=input_tokens,
                                                         output_tokens=64),
            )

    class SuccessfulTools:
        async def execute(self, request, *, context):
            return ToolCallResult(
                call_id=request.call_id, tool_name=request.tool_name, tool_version="1.0",
                status=ToolExecutionStatus.COMPLETED, risk=RiskLevel.MEDIUM,
                output={"path": request.arguments["path"], "content": "source"}, attempts=1,
            )

    gateway = MultiFileGateway()
    executor = executor_for(tmp_path, ids, gateway=gateway)
    executor._tool_gateway = SuccessfulTools()
    executor._usage = InMemoryUsageRecorder()

    async def no_prior(request):
        return ()

    monkeypatch.setattr(executor, "_prior_tool_results", no_prior)
    if should_finish:
        output, *_ = await executor._invoke(request_for(ids))
        assert output.changed_paths == ("app.py",)
        assert gateway.calls == 9
    else:
        with pytest.raises(AgentBudgetExceeded, match="token budget"):
            await executor._invoke(request_for(ids))
        assert gateway.calls < 9


@pytest.mark.parametrize("error, expected_code", [
    (AgentBudgetExceeded("agent token budget exceeded"), "AGENT_BUDGET_EXCEEDED"),
    (GatewayError("synthetic private provider response", failure_class=FailureClass.PERMANENT),
     "MODEL_PROVIDER_ERROR"),
])
async def test_node_failure_publishes_safe_task_activity_once(
    database, tmp_path, error, expected_code,
):
    ids = await seed_task(database)
    request = request_for(ids)
    executor = executor_for(tmp_path, ids, database=database)
    await executor._claim(request)
    await executor._mark_failed(request, error)
    await executor._mark_failed(request, error)
    async with database.sessions() as session:
        node = await session.get(
            WorkflowNodeExecutionRow, request.idempotency_uuid("node-execution")
        )
        events = list(await session.scalars(select(AuditEventRow).where(
            AuditEventRow.event_type == "task.node_failed",
            AuditEventRow.aggregate_id == ids["task_id"],
        )))
        assert node.status == "FAILED"
        assert len(events) == 1
        event = events[0]
        assert event.payload["error_code"] == expected_code
        assert event.payload["task_id"] == str(ids["task_id"])
        assert event.payload["run_id"] == str(ids["run_id"])
        assert event.payload["node"] == "implement"
        assert "synthetic private" not in json.dumps(event.payload)
        assert "synthetic private" not in json.dumps(node.result)
        outbox = await session.scalar(select(OutboxRow).where(OutboxRow.event_id == event.id))
        assert outbox.topic == "task-state"
        assert outbox.payload == event.payload
