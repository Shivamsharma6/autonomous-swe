from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import Field
from sqlalchemy import select

from domain.enums import GraphExecutionState, RiskLevel, TaskStatus
from domain.models import ContractModel, ToolCallRequest
from persistence.repositories import DomainRepository
from persistence.tables import GraphExecutionRow, TaskRow, ToolExecutionRow
from tests.integration.messaging.helpers import seed_task
from tools.approval import ApprovalService
from tools.gateway import (
    ToolExecutionStatus,
    ToolGateway,
    TransientToolError,
    UnknownSideEffectOutcome,
)
from tools.registry import (
    NetworkProfile,
    ReplayPolicy,
    SideEffectClass,
    ToolExecutionContext,
    ToolRegistry,
    ToolSpec,
)


class EchoArguments(ContractModel):
    schema_version: str = "1.0"
    value: str = Field(min_length=1)


class EchoResult(ContractModel):
    schema_version: str = "1.0"
    echoed: str


def execution_context(ids: dict[str, Any], root: Path) -> ToolExecutionContext:
    return ToolExecutionContext(
        project_id=ids["project_id"],
        repository_id=ids["repository_id"],
        run_id=ids["run_id"],
        task_id=ids["task_id"],
        attempt_id=ids["attempt_id"],
        baseline_commit="b" * 40,
        agent_role="coder",
        agent_capabilities=frozenset({"test.echo"}),
        risk_ceiling=RiskLevel.MEDIUM,
        worktree_root=root,
    )


def call(ids: dict[str, Any], *, call_id: UUID | None = None) -> ToolCallRequest:
    identifier = call_id or uuid4()
    return ToolCallRequest(
        call_id=identifier,
        run_id=ids["run_id"],
        task_id=ids["task_id"],
        attempt_id=ids["attempt_id"],
        requested_by="coder",
        tool_name="echo",
        tool_version="1.0",
        arguments={"schema_version": "1.0", "value": "hello"},
        idempotency_key=f"echo:{identifier}",
    )


def registry_with(executor: object, *, side_effect: SideEffectClass) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="echo",
            version="1.0",
            argument_model=EchoArguments,
            result_model=EchoResult,
            owning_capability="test.echo",
            eligible_agents=frozenset({"coder"}),
            base_risk=RiskLevel.LOW,
            timeout_seconds=2,
            max_attempts=2,
            replay_policy=ReplayPolicy.IDEMPOTENT,
            sandbox_profile="none",
            network_profile=NetworkProfile.NONE,
            side_effect=side_effect,
            approval_required=False,
            initial_backoff_seconds=0,
        ),
        executor,  # type: ignore[arg-type]
    )
    return registry


@pytest.mark.asyncio
async def test_concurrent_duplicate_calls_execute_once_and_replay_persisted_result(
    database: Any,
    tmp_path: Path,
) -> None:
    ids = await seed_task(database)
    async with database.transaction() as session:
        task = await session.get(TaskRow, ids["task_id"])
        assert task is not None
        task.state = TaskStatus.RUNNING
    executions = 0

    async def executor(arguments: ContractModel, _: ToolExecutionContext) -> dict[str, object]:
        nonlocal executions
        executions += 1
        await asyncio.sleep(0.03)
        assert isinstance(arguments, EchoArguments)
        return {"schema_version": "1.0", "echoed": arguments.value}

    gateway = ToolGateway(
        database=database,
        registry=registry_with(executor, side_effect=SideEffectClass.NONE),
        approvals=ApprovalService(database=database),
        poll_interval_seconds=0.005,
    )
    request = call(ids)
    context = execution_context(ids, tmp_path)

    first, duplicate = await asyncio.gather(
        gateway.execute(request, context=context),
        gateway.execute(request, context=context),
    )
    replay = await gateway.execute(request, context=context)

    assert executions == 1
    assert (
        first.output
        == duplicate.output
        == replay.output
        == {
            "schema_version": "1.0",
            "echoed": "hello",
        }
    )
    assert sum(result.replayed for result in (first, duplicate)) == 1
    assert replay.replayed is True
    async with database.transaction() as session:
        rows = tuple((await session.scalars(select(ToolExecutionRow))).all())
    assert len(rows) == 1
    assert rows[0].status == ToolExecutionStatus.COMPLETED.value


@pytest.mark.asyncio
async def test_uncertain_external_side_effect_is_never_retried_and_marks_reconciliation(
    database: Any,
    tmp_path: Path,
) -> None:
    ids = await seed_task(database)
    repository = DomainRepository()
    async with database.transaction() as session:
        task = await session.get(TaskRow, ids["task_id"])
        assert task is not None
        task.state = TaskStatus.RUNNING
        await repository.record_graph_execution(
            session,
            task_id=ids["task_id"],
            run_id=ids["run_id"],
            repository_id=ids["repository_id"],
            baseline_commit="b" * 40,
            thread_id=f"run:{ids['run_id']}:task:{ids['task_id']}",
            state=GraphExecutionState.RUNNING,
            checkpoint_id="checkpoint-before-tool",
        )
    executions = 0

    async def uncertain(_: ContractModel, __: ToolExecutionContext) -> dict[str, object]:
        nonlocal executions
        executions += 1
        raise UnknownSideEffectOutcome("provider accepted request but response was lost")

    gateway = ToolGateway(
        database=database,
        registry=registry_with(uncertain, side_effect=SideEffectClass.EXTERNAL),
        approvals=ApprovalService(database=database),
    )
    request = call(ids)

    result = await gateway.execute(
        request,
        context=execution_context(ids, tmp_path).model_copy(
            update={"risk_ceiling": RiskLevel.HIGH}
        ),
    )
    replay = await gateway.execute(
        request,
        context=execution_context(ids, tmp_path).model_copy(
            update={"risk_ceiling": RiskLevel.HIGH}
        ),
    )

    assert executions == 1
    assert result.status is ToolExecutionStatus.NEEDS_RECONCILIATION
    assert replay.status is ToolExecutionStatus.NEEDS_RECONCILIATION
    async with database.transaction() as session:
        graph = await session.scalar(
            select(GraphExecutionRow).where(GraphExecutionRow.task_id == ids["task_id"])
        )
    assert graph is not None
    assert graph.state is GraphExecutionState.NEEDS_RECONCILIATION


@pytest.mark.asyncio
async def test_declared_transient_retry_is_bounded_and_then_replayed(
    database: Any,
    tmp_path: Path,
) -> None:
    ids = await seed_task(database)
    async with database.transaction() as session:
        task = await session.get(TaskRow, ids["task_id"])
        assert task is not None
        task.state = TaskStatus.RUNNING
    executions = 0

    async def flaky(arguments: ContractModel, _: ToolExecutionContext) -> dict[str, object]:
        nonlocal executions
        executions += 1
        if executions == 1:
            raise TransientToolError("temporary runner pressure")
        assert isinstance(arguments, EchoArguments)
        return {"schema_version": "1.0", "echoed": arguments.value}

    registry = registry_with(flaky, side_effect=SideEffectClass.NONE)
    gateway = ToolGateway(
        database=database,
        registry=registry,
        approvals=ApprovalService(database=database),
    )
    request = call(ids)
    ctx = execution_context(ids, tmp_path)

    result = await gateway.execute(request, context=ctx)
    replay = await gateway.execute(request, context=ctx)

    assert result.status is ToolExecutionStatus.COMPLETED
    assert executions == 2
    assert replay.replayed is True
    assert executions == 2
