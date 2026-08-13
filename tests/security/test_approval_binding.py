from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import Field

from domain.enums import RiskLevel, TaskStatus
from domain.models import ContractModel, ToolCallRequest
from persistence.tables import TaskRow
from tests.integration.messaging.helpers import seed_task
from tools.approval import ApprovalBindingError, ApprovalExpired, ApprovalService
from tools.gateway import ApprovalRequired, ToolExecutionStatus, ToolGateway
from tools.registry import (
    NetworkProfile,
    ReplayPolicy,
    SideEffectClass,
    ToolExecutionContext,
    ToolRegistry,
    ToolSpec,
)


class CommitArguments(ContractModel):
    schema_version: str = "1.0"
    message: str = Field(min_length=1)


class CommitResult(ContractModel):
    schema_version: str = "1.0"
    commit: str


def context(ids: dict[str, Any], root: Path, **updates: object) -> ToolExecutionContext:
    values: dict[str, object] = {
        "project_id": ids["project_id"],
        "repository_id": ids["repository_id"],
        "run_id": ids["run_id"],
        "task_id": ids["task_id"],
        "attempt_id": ids["attempt_id"],
        "baseline_commit": "b" * 40,
        "agent_role": "coder",
        "agent_capabilities": frozenset({"repository.commit"}),
        "risk_ceiling": RiskLevel.HIGH,
        "worktree_root": root,
    }
    values.update(updates)
    return ToolExecutionContext.model_validate(values)


def call(ids: dict[str, Any], **argument_updates: object) -> ToolCallRequest:
    arguments = {"schema_version": "1.0", "message": "verified change"}
    arguments.update(argument_updates)
    call_id = uuid4()
    return ToolCallRequest(
        call_id=call_id,
        run_id=ids["run_id"],
        task_id=ids["task_id"],
        attempt_id=ids["attempt_id"],
        requested_by="coder",
        tool_name="git_commit",
        tool_version="1.0",
        arguments=arguments,
        idempotency_key=f"commit:{call_id}",
    )


@pytest.mark.asyncio
async def test_approval_is_bound_to_exact_call_scope_approver_and_expiry(
    database: Any,
    tmp_path: Path,
) -> None:
    ids = await seed_task(database)
    now = datetime.now(UTC)
    service = ApprovalService(database=database, ttl=timedelta(minutes=5))
    exact_call = call(ids)
    exact_context = context(ids, tmp_path)
    request = await service.request(exact_call, context=exact_context, now=now)
    await service.decide(
        request.approval_id,
        approver="admin@example.com",
        approved=True,
        now=now + timedelta(seconds=1),
    )

    authorized = await service.authorize(
        request.approval_id,
        exact_call,
        context=exact_context,
        expected_approver="admin@example.com",
        now=now + timedelta(seconds=2),
    )
    assert authorized.approver == "admin@example.com"

    changed_arguments = exact_call.model_copy(
        update={"arguments": exact_call.arguments | {"message": "different"}}
    )
    changed_repository = context(ids, tmp_path, repository_id=uuid4())
    changed_baseline = context(ids, tmp_path, baseline_commit="c" * 40)
    for changed_call, changed_context, approver in (
        (changed_arguments, exact_context, "admin@example.com"),
        (exact_call, changed_repository, "admin@example.com"),
        (exact_call, changed_baseline, "admin@example.com"),
        (exact_call, exact_context, "other@example.com"),
    ):
        with pytest.raises(ApprovalBindingError):
            await service.authorize(
                request.approval_id,
                changed_call,
                context=changed_context,
                expected_approver=approver,
                now=now + timedelta(seconds=2),
            )

    with pytest.raises(ApprovalExpired):
        await service.authorize(
            request.approval_id,
            exact_call,
            context=exact_context,
            expected_approver="admin@example.com",
            now=now + timedelta(minutes=6),
        )


@pytest.mark.asyncio
async def test_gateway_interrupts_before_protected_tool_and_executes_only_after_approval(
    database: Any,
    tmp_path: Path,
) -> None:
    ids = await seed_task(database)
    async with database.transaction() as session:
        task = await session.get(TaskRow, ids["task_id"])
        assert task is not None
        task.state = TaskStatus.RUNNING
    executions = 0

    async def commit_executor(
        arguments: ContractModel, _: ToolExecutionContext
    ) -> dict[str, object]:
        nonlocal executions
        executions += 1
        assert isinstance(arguments, CommitArguments)
        return {"schema_version": "1.0", "commit": "a" * 40}

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="git_commit",
            version="1.0",
            argument_model=CommitArguments,
            result_model=CommitResult,
            owning_capability="repository.commit",
            eligible_agents=frozenset({"coder"}),
            base_risk=RiskLevel.HIGH,
            timeout_seconds=5,
            max_attempts=1,
            replay_policy=ReplayPolicy.NEVER,
            sandbox_profile="git-integration",
            network_profile=NetworkProfile.NONE,
            side_effect=SideEffectClass.EXTERNAL,
            approval_required=True,
        ),
        commit_executor,
    )
    approvals = ApprovalService(database=database)
    gateway = ToolGateway(database=database, registry=registry, approvals=approvals)
    request = call(ids)
    execution_context = context(ids, tmp_path)

    with pytest.raises(ApprovalRequired) as paused:
        await gateway.execute(request, context=execution_context)
    assert executions == 0
    await approvals.decide(
        paused.value.request.approval_id,
        approver="admin@example.com",
        approved=True,
    )

    result = await gateway.execute(
        request,
        context=execution_context,
        approval_id=paused.value.request.approval_id,
        expected_approver="admin@example.com",
    )

    assert result.status is ToolExecutionStatus.COMPLETED
    assert result.output["commit"] == "a" * 40
    assert executions == 1
