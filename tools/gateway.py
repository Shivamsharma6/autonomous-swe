from __future__ import annotations

import asyncio
from enum import StrEnum
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from domain.enums import GraphExecutionState, RiskLevel, TaskStatus
from domain.models import ApprovalRequest, ContractModel, ToolCallRequest
from persistence.database import Database
from persistence.repositories import DomainRepository
from persistence.tables import (
    GraphExecutionRow,
    RunRow,
    TaskAttemptRow,
    TaskRow,
    ToolExecutionRow,
    utc_now,
)
from policies.guardrails.secret_redactor import SecretRedactor
from policies.risk.policy_engine import ToolRiskPolicy
from tools.approval import ApprovalService
from tools.registry import (
    RegisteredTool,
    ReplayPolicy,
    SideEffectClass,
    ToolExecutionContext,
    ToolRegistry,
)


class ToolExecutionStatus(StrEnum):
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    CLAIMED = "CLAIMED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    NEEDS_RECONCILIATION = "NEEDS_RECONCILIATION"


_TERMINAL = {
    ToolExecutionStatus.COMPLETED,
    ToolExecutionStatus.FAILED,
    ToolExecutionStatus.NEEDS_RECONCILIATION,
}


class ToolCallResult(ContractModel):
    schema_version: str = "1.0"
    call_id: UUID
    tool_name: str
    tool_version: str
    status: ToolExecutionStatus
    output: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    risk: RiskLevel
    attempts: int = Field(ge=0, le=20)
    replayed: bool = False


class ToolGatewayError(RuntimeError):
    pass


class ToolIdempotencyConflict(ToolGatewayError):
    pass


class ToolClaimInProgress(ToolGatewayError):
    pass


class UnknownSideEffectOutcome(ToolGatewayError):
    pass


class TransientToolError(ToolGatewayError):
    pass


class ApprovalRequired(ToolGatewayError):
    def __init__(self, request: ApprovalRequest) -> None:
        super().__init__(f"tool call requires approval {request.approval_id}")
        self.request = request


class ToolGateway:
    def __init__(
        self,
        *,
        database: Database,
        registry: ToolRegistry,
        approvals: ApprovalService,
        risk_policy: ToolRiskPolicy | None = None,
        repository: DomainRepository | None = None,
        poll_interval_seconds: float = 0.05,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("tool poll interval must be positive")
        self._database = database
        self._registry = registry
        self._approvals = approvals
        self._risk_policy = risk_policy or ToolRiskPolicy()
        self._repository = repository or DomainRepository()
        self._redactor = SecretRedactor()
        self._poll_interval = poll_interval_seconds

    async def execute(
        self,
        call: ToolCallRequest,
        *,
        context: ToolExecutionContext,
        approval_id: UUID | None = None,
        expected_approver: str | None = None,
    ) -> ToolCallResult:
        await self._validate_context(call, context)
        registered = self._registry.resolve(call.tool_name, call.tool_version)
        arguments = registered.validate_arguments(call.arguments, context)
        normalized_call = call.model_copy(update={"arguments": arguments.model_dump(mode="json")})
        risk = self._risk_policy.calculate(
            base=registered.spec.base_risk,
            tool_name=registered.spec.name,
            arguments=normalized_call.arguments,
            side_effect=registered.spec.side_effect,
        )
        registered.authorize(context, calculated_risk=risk)
        initial = (
            ToolExecutionStatus.WAITING_FOR_APPROVAL
            if registered.spec.approval_required
            else ToolExecutionStatus.CLAIMED
        )
        row, owner = await self._claim(normalized_call, initial, risk=risk)
        status = ToolExecutionStatus(row.status)
        if status in _TERMINAL:
            return _result_from_row(row, replayed=True)

        if registered.spec.approval_required:
            approval = await self._approvals.request(
                normalized_call,
                context=context,
            )
            if approval_id is None:
                raise ApprovalRequired(approval)
            if approval_id != approval.approval_id:
                raise PermissionError("approval ID does not match this exact tool call")
            await self._approvals.authorize(
                approval_id,
                normalized_call,
                context=context,
                expected_approver=expected_approver,
            )
            owner = await self._promote_approved(normalized_call)
            if not owner:
                return await self._wait_for_terminal(
                    normalized_call,
                    timeout_seconds=registered.spec.timeout_seconds * registered.spec.max_attempts
                    + 1,
                )
        elif not owner:
            return await self._wait_for_terminal(
                normalized_call,
                timeout_seconds=registered.spec.timeout_seconds * registered.spec.max_attempts + 1,
            )
        return await self._execute_owned(
            normalized_call,
            registered=registered,
            arguments=arguments,
            context=context,
            risk=risk,
        )

    async def complete_approved(
        self,
        call: ToolCallRequest,
        *,
        output: dict[str, Any],
        risk: RiskLevel = RiskLevel.HIGH,
    ) -> ToolCallResult:
        """Finalize an externally executed, human-approved tool call through the
        same durable audit/outbox path as gateway-executed tools."""
        async with self._database.transaction() as session:
            row = await session.scalar(
                select(ToolExecutionRow)
                .where(ToolExecutionRow.idempotency_key == call.idempotency_key)
                .with_for_update()
            )
            if row is None or row.id != call.call_id:
                raise ToolGatewayError("approved tool execution is missing")
            _validate_replay(row, call)
            if ToolExecutionStatus(row.status) not in {
                ToolExecutionStatus.CLAIMED,
                ToolExecutionStatus.WAITING_FOR_APPROVAL,
            }:
                raise ToolGatewayError(
                    f"approved tool call is not executable in status {row.status}"
                )
        redacted = cast(dict[str, Any], self._redactor.redact(dict(output)))
        return await self._persist_terminal(
            call,
            status=ToolExecutionStatus.COMPLETED,
            output=redacted,
            error=None,
            risk=risk,
            attempts=1,
        )

    async def _execute_owned(
        self,
        call: ToolCallRequest,
        *,
        registered: RegisteredTool,
        arguments: ContractModel,
        context: ToolExecutionContext,
        risk: RiskLevel,
    ) -> ToolCallResult:
        attempts = 0
        for attempts in range(1, registered.spec.max_attempts + 1):
            try:
                async with asyncio.timeout(registered.spec.timeout_seconds):
                    raw_result = await registered.executor(arguments, context)
                validated = registered.validate_result(raw_result)
                output = cast(
                    dict[str, Any],
                    self._redactor.redact(validated.model_dump(mode="json")),
                )
                return await self._persist_terminal(
                    call,
                    status=ToolExecutionStatus.COMPLETED,
                    output=output,
                    error=None,
                    risk=risk,
                    attempts=attempts,
                )
            except UnknownSideEffectOutcome as error:
                return await self._uncertain(
                    call,
                    error=error,
                    risk=risk,
                    attempts=attempts,
                )
            except TimeoutError as error:
                if registered.spec.side_effect is SideEffectClass.EXTERNAL:
                    return await self._uncertain(
                        call,
                        error=UnknownSideEffectOutcome("external tool timed out"),
                        risk=risk,
                        attempts=attempts,
                    )
                if not registered.spec.retry_on_timeout or not _may_retry(registered, attempts):
                    return await self._failed(call, error, risk=risk, attempts=attempts)
                await _retry_delay(registered, attempts)
            except TransientToolError as error:
                if registered.spec.side_effect is SideEffectClass.EXTERNAL:
                    return await self._uncertain(
                        call,
                        error=UnknownSideEffectOutcome(str(error)),
                        risk=risk,
                        attempts=attempts,
                    )
                if not registered.spec.retry_on_transient or not _may_retry(registered, attempts):
                    return await self._failed(call, error, risk=risk, attempts=attempts)
                await _retry_delay(registered, attempts)
            except Exception as error:
                return await self._failed(call, error, risk=risk, attempts=attempts)
        raise AssertionError("tool attempt loop exited unexpectedly")

    async def _claim(
        self,
        call: ToolCallRequest,
        initial: ToolExecutionStatus,
        *,
        risk: RiskLevel,
    ) -> tuple[ToolExecutionRow, bool]:
        async with self._database.transaction() as session:
            statement = (
                insert(ToolExecutionRow)
                .values(
                    id=call.call_id,
                    run_id=call.run_id,
                    task_id=call.task_id,
                    attempt_id=call.attempt_id,
                    requested_by=call.requested_by,
                    tool_name=call.tool_name,
                    tool_version=call.tool_version,
                    arguments=call.arguments,
                    idempotency_key=call.idempotency_key,
                    status=initial.value,
                    result={},
                )
                .on_conflict_do_nothing()
                .returning(ToolExecutionRow.id)
            )
            inserted = await session.scalar(statement)
            row = await session.scalar(
                select(ToolExecutionRow)
                .where(ToolExecutionRow.idempotency_key == call.idempotency_key)
                .with_for_update()
            )
            if row is None:
                conflicting = await session.get(ToolExecutionRow, call.call_id)
                if conflicting is not None:
                    raise ToolIdempotencyConflict("call_id is already bound to another key")
                raise ToolGatewayError("tool claim disappeared")
            _validate_replay(row, call)
            if inserted is not None:
                event_id = uuid5(NAMESPACE_URL, f"tool-requested:{call.call_id}")
                redacted = cast(dict[str, Any], self._redactor.redact(call.arguments))
                payload = {
                    "call_id": str(call.call_id),
                    "tool": f"{call.tool_name}@{call.tool_version}",
                    "risk": risk.value,
                    "arguments": redacted,
                }
                await self._repository.append_audit(
                    session,
                    event_id=event_id,
                    event_type="tool.requested",
                    aggregate_type="tool_call",
                    aggregate_id=call.call_id,
                    payload=payload,
                    correlation_id=call.run_id,
                    causation_id=call.call_id,
                )
                await self._repository.enqueue_event(
                    session,
                    event_id=event_id,
                    topic="tool-execution",
                    payload=payload,
                )
            return row, inserted is not None

    async def _promote_approved(self, call: ToolCallRequest) -> bool:
        async with self._database.transaction() as session:
            result = await session.execute(
                update(ToolExecutionRow)
                .where(
                    ToolExecutionRow.id == call.call_id,
                    ToolExecutionRow.idempotency_key == call.idempotency_key,
                    ToolExecutionRow.status == ToolExecutionStatus.WAITING_FOR_APPROVAL.value,
                )
                .values(status=ToolExecutionStatus.CLAIMED.value)
            )
            return bool(getattr(result, "rowcount", 0))

    async def _wait_for_terminal(
        self,
        call: ToolCallRequest,
        *,
        timeout_seconds: float,
    ) -> ToolCallResult:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while loop.time() < deadline:
            async with self._database.transaction() as session:
                row = await session.scalar(
                    select(ToolExecutionRow).where(
                        ToolExecutionRow.idempotency_key == call.idempotency_key
                    )
                )
                if row is None:
                    raise ToolGatewayError("claimed tool execution is missing")
                _validate_replay(row, call)
                if ToolExecutionStatus(row.status) in _TERMINAL:
                    return _result_from_row(row, replayed=True)
            await asyncio.sleep(self._poll_interval)
        raise ToolClaimInProgress(
            f"tool execution {call.idempotency_key} did not reach a terminal state"
        )

    async def _failed(
        self,
        call: ToolCallRequest,
        error: Exception,
        *,
        risk: RiskLevel,
        attempts: int,
    ) -> ToolCallResult:
        redacted = str(self._redactor.redact(str(error)))
        return await self._persist_terminal(
            call,
            status=ToolExecutionStatus.FAILED,
            output={},
            error=redacted,
            risk=risk,
            attempts=attempts,
        )

    async def _uncertain(
        self,
        call: ToolCallRequest,
        *,
        error: UnknownSideEffectOutcome,
        risk: RiskLevel,
        attempts: int,
    ) -> ToolCallResult:
        result = await self._persist_terminal(
            call,
            status=ToolExecutionStatus.NEEDS_RECONCILIATION,
            output={},
            error=str(self._redactor.redact(str(error))),
            risk=risk,
            attempts=attempts,
        )
        async with self._database.transaction() as session:
            graph = await session.scalar(
                select(GraphExecutionRow).where(GraphExecutionRow.task_id == call.task_id)
            )
            if graph is not None and graph.state is not GraphExecutionState.NEEDS_RECONCILIATION:
                await self._repository.transition_graph_execution(
                    session,
                    task_id=graph.task_id,
                    run_id=graph.run_id,
                    repository_id=graph.repository_id,
                    baseline_commit=graph.baseline_commit,
                    thread_id=graph.thread_id,
                    target=GraphExecutionState.NEEDS_RECONCILIATION,
                    checkpoint_id=graph.checkpoint_id,
                )
        return result

    async def _persist_terminal(
        self,
        call: ToolCallRequest,
        *,
        status: ToolExecutionStatus,
        output: dict[str, Any],
        error: str | None,
        risk: RiskLevel,
        attempts: int,
    ) -> ToolCallResult:
        payload = {
            "output": output,
            "error": error,
            "risk": risk.value,
            "attempts": attempts,
        }
        async with self._database.transaction() as session:
            row = await session.scalar(
                select(ToolExecutionRow)
                .where(ToolExecutionRow.id == call.call_id)
                .with_for_update()
            )
            if row is None:
                raise ToolGatewayError("owned tool execution is missing")
            if ToolExecutionStatus(row.status) in _TERMINAL:
                return _result_from_row(row, replayed=True)
            row.status = status.value
            row.result = payload
            row.completed_at = utc_now()
            event_id = uuid5(NAMESPACE_URL, f"tool-terminal:{call.call_id}:{status.value}")
            event = {
                "call_id": str(call.call_id),
                "status": status.value,
                "risk": risk.value,
                "attempts": attempts,
                "error": error,
            }
            await self._repository.append_audit(
                session,
                event_id=event_id,
                event_type=f"tool.{status.value.casefold()}",
                aggregate_type="tool_call",
                aggregate_id=call.call_id,
                payload=event,
                correlation_id=call.run_id,
                causation_id=call.call_id,
            )
            await self._repository.enqueue_event(
                session,
                event_id=event_id,
                topic="tool-execution",
                payload=event,
            )
            await session.flush()
            return _result_from_row(row, replayed=False)

    async def _validate_context(self, call: ToolCallRequest, context: ToolExecutionContext) -> None:
        if (
            call.run_id != context.run_id
            or call.task_id != context.task_id
            or call.attempt_id != context.attempt_id
            or call.requested_by != context.agent_role
        ):
            raise PermissionError("tool call does not match its execution context")
        async with self._database.transaction() as session:
            task = await session.get(TaskRow, call.task_id)
            attempt = await session.get(TaskAttemptRow, call.attempt_id)
            run = await session.get(RunRow, call.run_id)
            if task is None or attempt is None or run is None:
                raise PermissionError("tool call domain identity does not exist")
            if (
                task.state is not TaskStatus.RUNNING
                or task.project_id != context.project_id
                or task.repository_id != context.repository_id
                or attempt.task_id != task.id
                or run.project_id != context.project_id
                or run.repository_id != context.repository_id
                or run.baseline_commit != context.baseline_commit
            ):
                raise PermissionError(
                    "tool call is not bound to the active scheduler task and repository baseline"
                )


def _validate_replay(row: ToolExecutionRow, call: ToolCallRequest) -> None:
    if (
        row.id != call.call_id
        or row.run_id != call.run_id
        or row.task_id != call.task_id
        or row.attempt_id != call.attempt_id
        or row.requested_by != call.requested_by
        or row.tool_name != call.tool_name
        or row.tool_version != call.tool_version
        or row.arguments != call.arguments
        or row.idempotency_key != call.idempotency_key
    ):
        raise ToolIdempotencyConflict(
            "idempotency key replay does not match the exact original tool call"
        )


def _result_from_row(row: ToolExecutionRow, *, replayed: bool) -> ToolCallResult:
    result = row.result or {}
    return ToolCallResult(
        call_id=row.id,
        tool_name=row.tool_name,
        tool_version=row.tool_version,
        status=ToolExecutionStatus(row.status),
        output=result.get("output") if isinstance(result.get("output"), dict) else {},
        error=str(result["error"]) if result.get("error") is not None else None,
        risk=RiskLevel(str(result.get("risk") or RiskLevel.LOW.value)),
        attempts=int(result.get("attempts", 0)),
        replayed=replayed,
    )


def _may_retry(registered: RegisteredTool, attempts: int) -> bool:
    return (
        registered.spec.replay_policy is not ReplayPolicy.NEVER
        and attempts < registered.spec.max_attempts
    )


async def _retry_delay(registered: RegisteredTool, attempts: int) -> None:
    delay = min(
        registered.spec.maximum_backoff_seconds,
        registered.spec.initial_backoff_seconds * (2 ** max(0, attempts - 1)),
    )
    if delay:
        await asyncio.sleep(delay)
