from __future__ import annotations

import asyncio
from enum import StrEnum
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domain.models import ContractModel
from execution.sandbox.runner import SandboxRequest, SandboxResult
from observability.metrics import track_actual_resource
from persistence.database import Database
from persistence.repositories import DomainRepository
from persistence.tables import SandboxContainerRow, SandboxExecutionRow, utc_now


class SandboxRunStatus(StrEnum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    FINISHED = "FINISHED"
    INTERRUPTED = "INTERRUPTED"


class SandboxRunRecord(ContractModel):
    execution_id: UUID
    run_id: UUID
    task_id: UUID
    attempt_id: UUID
    container_id: str
    status: SandboxRunStatus
    cancellation_requested: bool


class SandboxRunConflict(RuntimeError):
    """A replay differs from the durable sandbox execution identity or outcome."""


class SandboxRunnerPort(Protocol):
    def create(self, request: SandboxRequest) -> str: ...

    def run_created(self, request: SandboxRequest, container_id: str) -> SandboxResult: ...

    def cancel(self, execution_id: UUID) -> bool: ...

    def kill_container(self, container_id: str) -> None: ...

    def remove_container(self, container_id: str) -> None: ...

    def release(self, execution_id: UUID, container_id: str, *, remove: bool = True) -> None: ...


class PostgresSandboxRunStore:
    def __init__(
        self,
        database: Database,
        *,
        repository: DomainRepository | None = None,
    ) -> None:
        self._database = database
        self._repository = repository or DomainRepository()

    async def register(self, request: SandboxRequest, container_id: str) -> SandboxRunRecord:
        async with self._database.transaction() as session:
            existing = await session.get(SandboxContainerRow, request.execution_id)
            if existing is not None:
                _require_identity(existing, request, container_id)
                return _record(existing)
            row = SandboxContainerRow(
                execution_id=request.execution_id,
                run_id=request.run_id,
                task_id=request.task_id,
                attempt_id=request.attempt_id,
                container_id=container_id,
                status=SandboxRunStatus.CREATED.value,
                cancellation_requested=False,
            )
            session.add(row)
            await session.flush()
            await self._event(
                session,
                request.execution_id,
                request.run_id,
                "sandbox.container_created",
                {"container_id": container_id, "status": row.status},
            )
            return _record(row)

    async def mark_running(self, execution_id: UUID) -> SandboxRunRecord:
        async with self._database.transaction() as session:
            row = await _locked_row(session, execution_id)
            if row.status == SandboxRunStatus.CREATED.value:
                row.status = SandboxRunStatus.RUNNING.value
                row.started_at = utc_now()
                await session.flush()
            elif row.status not in {
                SandboxRunStatus.RUNNING.value,
                SandboxRunStatus.CANCEL_REQUESTED.value,
            }:
                raise SandboxRunConflict(f"cannot run sandbox in state {row.status}")
            return _record(row)

    async def request_cancellation(self, execution_id: UUID) -> SandboxRunRecord:
        async with self._database.transaction() as session:
            row = await _locked_row(session, execution_id)
            if row.status in {
                SandboxRunStatus.FINISHED.value,
                SandboxRunStatus.INTERRUPTED.value,
            }:
                return _record(row)
            row.cancellation_requested = True
            row.status = SandboxRunStatus.CANCEL_REQUESTED.value
            await session.flush()
            await self._event(
                session,
                row.execution_id,
                row.run_id,
                "sandbox.cancellation_requested",
                {"container_id": row.container_id, "status": row.status},
            )
            return _record(row)

    async def cancellation_requested(self, execution_id: UUID) -> bool:
        async with self._database.sessions() as session:
            value = await session.scalar(
                select(SandboxContainerRow.cancellation_requested).where(
                    SandboxContainerRow.execution_id == execution_id
                )
            )
            return bool(value)

    async def finish(self, request: SandboxRequest, result: SandboxResult) -> SandboxRunRecord:
        async with self._database.transaction() as session:
            row = await _locked_row(session, request.execution_id)
            _require_identity(row, request, row.container_id)
            existing_execution = await session.get(SandboxExecutionRow, request.execution_id)
            if existing_execution is not None:
                if _execution_values(existing_execution) != result.execution.model_dump(
                    mode="json",
                    exclude={"schema_version"},
                ):
                    raise SandboxRunConflict("sandbox replay produced a different outcome")
            else:
                await self._repository.record_sandbox_execution(
                    session,
                    result.execution,
                    attempt_id=request.attempt_id,
                )
            if row.status != SandboxRunStatus.FINISHED.value:
                row.status = SandboxRunStatus.FINISHED.value
                row.finished_at = utc_now()
                row.error = None
                await session.flush()
                await self._event(
                    session,
                    request.execution_id,
                    request.run_id,
                    "sandbox.execution_finished",
                    {
                        "container_id": row.container_id,
                        "exit_reason": result.execution.exit_reason,
                        "limit_triggered": result.execution.limit_triggered,
                    },
                )
            return _record(row)

    async def interrupt(self, execution_id: UUID, error: str) -> SandboxRunRecord:
        async with self._database.transaction() as session:
            row = await _locked_row(session, execution_id)
            if row.status == SandboxRunStatus.FINISHED.value:
                return _record(row)
            row.status = SandboxRunStatus.INTERRUPTED.value
            row.finished_at = utc_now()
            row.error = error[:20_000]
            await session.flush()
            await self._event(
                session,
                row.execution_id,
                row.run_id,
                "sandbox.execution_interrupted",
                {"container_id": row.container_id, "error": row.error},
            )
            return _record(row)

    async def active(self) -> tuple[SandboxRunRecord, ...]:
        async with self._database.sessions() as session:
            rows = tuple(
                (
                    await session.scalars(
                        select(SandboxContainerRow)
                        .where(
                            SandboxContainerRow.status.in_(
                                (
                                    SandboxRunStatus.CREATED.value,
                                    SandboxRunStatus.RUNNING.value,
                                    SandboxRunStatus.CANCEL_REQUESTED.value,
                                )
                            )
                        )
                        .order_by(SandboxContainerRow.created_at, SandboxContainerRow.execution_id)
                    )
                ).all()
            )
            return tuple(_record(row) for row in rows)

    async def get(self, execution_id: UUID) -> SandboxRunRecord | None:
        async with self._database.sessions() as session:
            row = await session.get(SandboxContainerRow, execution_id)
            return _record(row) if row is not None else None

    async def _event(
        self,
        session: AsyncSession,
        execution_id: UUID,
        run_id: UUID,
        event_type: str,
        payload: dict[str, object],
    ) -> None:
        event_id = uuid4()
        await self._repository.append_audit(
            session,
            event_id=event_id,
            event_type=event_type,
            aggregate_type="sandbox_execution",
            aggregate_id=execution_id,
            payload=payload,
            correlation_id=run_id,
            causation_id=event_id,
        )
        await self._repository.enqueue_event(
            session,
            event_id=event_id,
            topic="sandbox-executions",
            payload={**payload, "execution_id": str(execution_id)},
        )


class SandboxManager:
    def __init__(
        self,
        runner: SandboxRunnerPort,
        store: PostgresSandboxRunStore,
    ) -> None:
        self._runner = runner
        self._store = store

    async def execute(self, request: SandboxRequest) -> SandboxResult:
        container_id = await asyncio.to_thread(self._runner.create, request)
        registered = False
        try:
            await self._store.register(request, container_id)
            registered = True
            await self._store.mark_running(request.execution_id)
            with track_actual_resource("sandbox"):
                result = await asyncio.to_thread(
                    self._runner.run_created,
                    request,
                    container_id,
                )
            if await self._store.cancellation_requested(request.execution_id):
                execution = result.execution.model_copy(
                    update={
                        "exit_reason": "CANCELLED",
                        "limit_triggered": "cancellation",
                    }
                )
                result = result.model_copy(update={"execution": execution})
            await self._store.finish(request, result)
            return result
        except BaseException as exc:
            if registered:
                await asyncio.shield(
                    self._store.interrupt(
                        request.execution_id,
                        f"{type(exc).__name__}: {exc}",
                    )
                )
            await asyncio.to_thread(self._runner.kill_container, container_id)
            raise
        finally:
            await asyncio.to_thread(
                self._runner.release,
                request.execution_id,
                container_id,
                remove=True,
            )

    async def cancel(self, execution_id: UUID) -> bool:
        record = await self._store.request_cancellation(execution_id)
        if record.status in {SandboxRunStatus.FINISHED, SandboxRunStatus.INTERRUPTED}:
            return False
        cancelled_locally = self._runner.cancel(execution_id)
        if not cancelled_locally:
            await asyncio.to_thread(self._runner.kill_container, record.container_id)
        return True

    async def reconcile_after_worker_restart(self) -> int:
        records = await self._store.active()
        for record in records:
            await asyncio.to_thread(self._runner.kill_container, record.container_id)
            await asyncio.to_thread(self._runner.remove_container, record.container_id)
            await self._store.interrupt(record.execution_id, "worker_restart_reconciliation")
        return len(records)


async def _locked_row(session: AsyncSession, execution_id: UUID) -> SandboxContainerRow:
    row = await session.scalar(
        select(SandboxContainerRow)
        .where(SandboxContainerRow.execution_id == execution_id)
        .with_for_update()
    )
    if row is None:
        raise LookupError(f"sandbox execution {execution_id} is not registered")
    return row


def _record(row: SandboxContainerRow) -> SandboxRunRecord:
    return SandboxRunRecord(
        execution_id=row.execution_id,
        run_id=row.run_id,
        task_id=row.task_id,
        attempt_id=row.attempt_id,
        container_id=row.container_id,
        status=SandboxRunStatus(row.status),
        cancellation_requested=row.cancellation_requested,
    )


def _require_identity(
    row: SandboxContainerRow,
    request: SandboxRequest,
    container_id: str,
) -> None:
    actual = (row.run_id, row.task_id, row.attempt_id, row.container_id)
    expected = (request.run_id, request.task_id, request.attempt_id, container_id)
    if actual != expected:
        raise SandboxRunConflict("sandbox execution identity changed during replay")


def _execution_values(row: SandboxExecutionRow) -> dict[str, object]:
    return {
        "execution_id": str(row.id),
        "task_id": str(row.task_id),
        "cpu_time_ms": row.cpu_time_ms,
        "peak_memory_bytes": row.peak_memory_bytes,
        "peak_processes": row.peak_processes,
        "processes_created": row.processes_created,
        "stdout_bytes": row.stdout_bytes,
        "stderr_bytes": row.stderr_bytes,
        "duration_ms": row.duration_ms,
        "network_requests": row.network_requests,
        "network_bytes_sent": row.network_bytes_sent,
        "network_bytes_received": row.network_bytes_received,
        "exit_code": row.exit_code,
        "exit_reason": row.exit_reason,
        "limit_triggered": row.limit_triggered,
        "measurement_source": row.measurement_source,
        "measurement_complete": row.measurement_complete,
    }
