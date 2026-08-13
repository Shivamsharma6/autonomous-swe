from __future__ import annotations

import hmac
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from domain.enums import ApprovalStatus
from domain.models import ApprovalRequest, ContractModel, ToolCallRequest
from persistence.database import Database
from persistence.repositories import DomainRepository
from persistence.tables import ApprovalRow, ToolExecutionRow
from tools.registry import ToolExecutionContext


class ApprovalError(RuntimeError):
    pass


class ApprovalBindingError(ApprovalError):
    pass


class ApprovalExpired(ApprovalError):
    pass


class ApprovalNotGranted(ApprovalError):
    pass


class ApprovalAuthorization(ContractModel):
    approval_id: UUID
    call_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    approver: str = Field(min_length=1, max_length=255)
    decided_at: datetime


class ApprovalService:
    def __init__(
        self,
        *,
        database: Database,
        ttl: timedelta = timedelta(hours=1),
        repository: DomainRepository | None = None,
    ) -> None:
        if ttl.total_seconds() <= 0:
            raise ValueError("approval TTL must be positive")
        self._database = database
        self._ttl = ttl
        self._repository = repository or DomainRepository()

    async def request(
        self,
        call: ToolCallRequest,
        *,
        context: ToolExecutionContext,
        now: datetime | None = None,
    ) -> ApprovalRequest:
        _validate_call_scope(call, context)
        created_at = now or datetime.now(UTC)
        expires_at = created_at + self._ttl
        provisional = ApprovalRequest(
            approval_id=uuid5(
                NAMESPACE_URL,
                f"approval:{call.call_id}:{expires_at.isoformat()}",
            ),
            call=call,
            project_id=context.project_id,
            repository_id=context.repository_id,
            baseline_commit=context.baseline_commit,
            expires_at=expires_at,
        )
        async with self._database.transaction() as session:
            await session.execute(
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
                    status="WAITING_FOR_APPROVAL",
                    result={},
                )
                .on_conflict_do_nothing()
            )
            execution = await session.get(ToolExecutionRow, call.call_id)
            if execution is None or not _execution_matches(execution, call):
                raise ApprovalBindingError(
                    "approval call does not match its persisted tool execution"
                )
            existing = await session.scalar(
                select(ApprovalRow).where(ApprovalRow.call_id == call.call_id).with_for_update()
            )
            if existing is not None:
                return _request_from_row(existing, call)
            await self._repository.create_approval(session, provisional)
            event_id = uuid5(NAMESPACE_URL, f"approval-requested:{provisional.approval_id}")
            payload = {
                "approval_id": str(provisional.approval_id),
                "call_id": str(call.call_id),
                "call_hash": provisional.call_hash,
                "expires_at": expires_at.isoformat(),
            }
            await self._repository.append_audit(
                session,
                event_id=event_id,
                event_type="approval.requested",
                aggregate_type="approval",
                aggregate_id=provisional.approval_id,
                payload=payload,
                correlation_id=call.run_id,
                causation_id=call.call_id,
            )
            await self._repository.enqueue_event(
                session,
                event_id=event_id,
                topic="approvals",
                payload=payload,
            )
            return provisional

    async def decide(
        self,
        approval_id: UUID,
        *,
        approver: str,
        approved: bool,
        expected_call_hash: str,
        now: datetime | None = None,
    ) -> None:
        if not approver.strip():
            raise ValueError("approver is required")
        decided_at = now or datetime.now(UTC)
        expired = False
        async with self._database.transaction() as session:
            row = await session.scalar(
                select(ApprovalRow).where(ApprovalRow.id == approval_id).with_for_update()
            )
            if row is None:
                raise LookupError(f"approval {approval_id} does not exist")
            if not hmac.compare_digest(row.call_hash, expected_call_hash):
                raise ApprovalBindingError("approval decision does not match the exact call")
            if decided_at >= row.expires_at:
                row.status = ApprovalStatus.EXPIRED
                row.decided_at = decided_at
                target = ApprovalStatus.EXPIRED
                expired = True
            else:
                target = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
                if row.status is not ApprovalStatus.PENDING:
                    if row.status is target and row.approver == approver:
                        return
                    raise ApprovalBindingError("approval already has a different decision")
                row.status = target
                row.approver = approver
                row.decided_at = decided_at
            await self._repository.record_state_duration(
                session,
                aggregate_type="approval",
                aggregate_id=row.id,
                state=ApprovalStatus.PENDING.value,
                entered_at=row.created_at,
                exited_at=decided_at,
            )
            event_id = uuid5(NAMESPACE_URL, f"approval-decided:{approval_id}:{target.value}")
            payload = {
                "approval_id": str(approval_id),
                "status": target.value,
                "approver": approver if not expired else None,
            }
            execution = await session.get(ToolExecutionRow, row.call_id)
            if execution is None:
                raise ApprovalBindingError("approval tool execution is missing")
            await self._repository.append_audit(
                session,
                event_id=event_id,
                event_type="approval.decided",
                aggregate_type="approval",
                aggregate_id=approval_id,
                payload=payload,
                correlation_id=execution.run_id,
                causation_id=row.call_id,
            )
            await self._repository.enqueue_event(
                session,
                event_id=event_id,
                topic="approvals",
                payload=payload,
            )
        if expired:
            raise ApprovalExpired(f"approval {approval_id} expired")

    async def authorize(
        self,
        approval_id: UUID,
        call: ToolCallRequest,
        *,
        context: ToolExecutionContext,
        expected_approver: str | None = None,
        now: datetime | None = None,
    ) -> ApprovalAuthorization:
        _validate_call_scope(call, context)
        checked_at = now or datetime.now(UTC)
        async with self._database.transaction() as session:
            row = await session.get(ApprovalRow, approval_id)
            if row is None:
                raise LookupError(f"approval {approval_id} does not exist")
            if checked_at >= row.expires_at:
                raise ApprovalExpired(f"approval {approval_id} expired")
            if row.status is not ApprovalStatus.APPROVED or not row.approver or not row.decided_at:
                raise ApprovalNotGranted(f"approval {approval_id} is not approved")
            if expected_approver is not None and row.approver != expected_approver:
                raise ApprovalBindingError("approval is bound to a different approver")
            candidate = ApprovalRequest(
                approval_id=row.id,
                call=call,
                project_id=context.project_id,
                repository_id=context.repository_id,
                baseline_commit=context.baseline_commit,
                expires_at=row.expires_at,
                status=row.status,
            )
            if (
                row.project_id != context.project_id
                or row.repository_id != context.repository_id
                or row.baseline_commit != context.baseline_commit
                or not hmac.compare_digest(row.call_hash, candidate.call_hash)
            ):
                raise ApprovalBindingError(
                    "approval is not bound to this exact call, repository, and baseline"
                )
            return ApprovalAuthorization(
                approval_id=row.id,
                call_hash=row.call_hash,
                approver=row.approver,
                decided_at=row.decided_at,
            )


def _request_from_row(row: ApprovalRow, call: ToolCallRequest) -> ApprovalRequest:
    request = ApprovalRequest(
        approval_id=row.id,
        call=call,
        project_id=row.project_id,
        repository_id=row.repository_id,
        baseline_commit=row.baseline_commit,
        expires_at=row.expires_at,
        status=row.status,
    )
    if not hmac.compare_digest(request.call_hash, row.call_hash):
        raise ApprovalBindingError("existing approval is bound to different call arguments")
    return request


def _validate_call_scope(call: ToolCallRequest, context: ToolExecutionContext) -> None:
    if (
        call.run_id != context.run_id
        or call.task_id != context.task_id
        or call.attempt_id != context.attempt_id
        or call.requested_by != context.agent_role
    ):
        raise ApprovalBindingError("tool call does not match its execution context")


def _execution_matches(row: ToolExecutionRow, call: ToolCallRequest) -> bool:
    return (
        row.id == call.call_id
        and row.run_id == call.run_id
        and row.task_id == call.task_id
        and row.attempt_id == call.attempt_id
        and row.requested_by == call.requested_by
        and row.tool_name == call.tool_name
        and row.tool_version == call.tool_version
        and row.arguments == call.arguments
        and row.idempotency_key == call.idempotency_key
    )
