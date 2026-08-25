from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from domain.enums import GraphExecutionState, RetryCategory, RiskLevel, TaskStatus, TaskType
from observability.metrics import PlatformMetrics, platform_metrics
from persistence.database import Database
from persistence.repositories import DomainRepository
from persistence.tables import (
    GraphExecutionRow,
    LeaseRow,
    ProjectTaskResourceEstimateRow,
    RepositoryRow,
    ReservationRow,
    RunRow,
    TaskAttemptRow,
    TaskRow,
)


class SchedulerPolicyModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ConcurrencyPolicy(SchedulerPolicyModel):
    max_parallel_tasks: int = Field(ge=1)
    max_parallel_tasks_per_project: int = Field(ge=1)
    max_model_concurrency: int = Field(ge=1)
    max_sandbox_concurrency: int = Field(ge=1)


class AdmissionSnapshot(SchedulerPolicyModel):
    active_tasks: int = Field(ge=0)
    active_project_tasks: int = Field(ge=0)
    active_model_slots: int = Field(ge=0)
    active_sandbox_slots: int = Field(ge=0)


class ResourceRequest(SchedulerPolicyModel):
    task_slots: int = Field(default=1, ge=1, le=1)
    model_slots: int = Field(default=0, ge=0, le=1)
    sandbox_slots: int = Field(default=0, ge=0, le=1)


class ResourceObservation(SchedulerPolicyModel):
    cpu_time_ms: int = Field(ge=0)
    peak_memory_bytes: int = Field(ge=0)
    duration_ms: int = Field(ge=0)
    output_bytes: int = Field(ge=0)
    network_requests: int = Field(ge=0)
    model_tokens: int = Field(ge=0)
    cost_usd: float = Field(ge=0)


class AdmissionDecision(SchedulerPolicyModel):
    admitted: bool
    reason: str | None = None


class RetryBudget(SchedulerPolicyModel):
    max_attempts: int = Field(ge=1)
    max_cost_usd: float = Field(ge=0)


class RetryDecision(SchedulerPolicyModel):
    retry: bool
    category: RetryCategory
    reason: str


@dataclass(frozen=True, slots=True)
class TaskClaim:
    task_id: UUID
    project_id: UUID
    owner: str
    token: UUID
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class TaskExecutionLease:
    task_id: UUID
    run_id: UUID
    project_id: UUID
    repository_id: UUID
    attempt_id: UUID
    baseline_commit: str
    source_path: str
    task_type: TaskType
    goal: str
    allowed_tools: tuple[str, ...]
    risk_ceiling: RiskLevel
    dependencies: tuple[UUID, ...]
    already_terminal: bool = False


def dependencies_satisfied(dependencies: Sequence[UUID], states: dict[UUID, TaskStatus]) -> bool:
    return all(states.get(dependency) is TaskStatus.COMPLETED for dependency in dependencies)


def evaluate_admission(
    policy: ConcurrencyPolicy,
    snapshot: AdmissionSnapshot,
    request: ResourceRequest,
) -> AdmissionDecision:
    if snapshot.active_tasks + request.task_slots > policy.max_parallel_tasks:
        return AdmissionDecision(admitted=False, reason="MAX_PARALLEL_TASKS")
    if snapshot.active_project_tasks + request.task_slots > policy.max_parallel_tasks_per_project:
        return AdmissionDecision(admitted=False, reason="MAX_PROJECT_PARALLEL_TASKS")
    if snapshot.active_model_slots + request.model_slots > policy.max_model_concurrency:
        return AdmissionDecision(admitted=False, reason="MAX_MODEL_CONCURRENCY")
    if snapshot.active_sandbox_slots + request.sandbox_slots > policy.max_sandbox_concurrency:
        return AdmissionDecision(admitted=False, reason="MAX_SANDBOX_CONCURRENCY")
    return AdmissionDecision(admitted=True)


def evaluate_retry(
    category: RetryCategory,
    *,
    attempts: int,
    consumed_cost_usd: float,
    budget: RetryBudget,
) -> RetryDecision:
    if category is not RetryCategory.TRANSIENT:
        return RetryDecision(
            retry=False,
            category=category,
            reason=f"{category.value} failures are not automatically retried",
        )
    if attempts >= budget.max_attempts:
        return RetryDecision(
            retry=False,
            category=category,
            reason="attempt budget exhausted",
        )
    if consumed_cost_usd >= budget.max_cost_usd:
        return RetryDecision(
            retry=False,
            category=category,
            reason="cost budget exhausted",
        )
    return RetryDecision(retry=True, category=category, reason="transient failure within budget")


class SchedulerService:
    _ADMISSION_LOCK_KEY = 4_283_057_995_119_945_555

    def __init__(
        self,
        *,
        database: Database,
        policy: ConcurrencyPolicy,
        lease_ttl: timedelta,
        repository: DomainRepository | None = None,
        metrics: PlatformMetrics | None = None,
    ) -> None:
        if lease_ttl.total_seconds() <= 0:
            raise ValueError("lease_ttl must be positive")
        self.database = database
        self.policy = policy
        self.lease_ttl = lease_ttl
        self.repository = repository or DomainRepository()
        self.metrics = metrics or platform_metrics

    async def publish_metrics(self) -> None:
        """Publish a low-cardinality scheduler snapshot from PostgreSQL authority."""
        async with self.database.sessions() as session:
            reservation_rows = (
                await session.execute(
                    select(ReservationRow.resource, func.sum(ReservationRow.units))
                    .where(ReservationRow.released_at.is_(None))
                    .group_by(ReservationRow.resource)
                )
            ).all()
            reservation_counts = {
                str(resource): int(units or 0) for resource, units in reservation_rows
            }
            state_rows = (
                await session.execute(
                    select(TaskRow.state, func.count()).group_by(TaskRow.state)
                )
            ).all()
            state_counts = {TaskStatus(state): int(count) for state, count in state_rows}
        for resource in ("task", "model", "sandbox"):
            self.metrics.set_resource_reservations(
                resource, reservation_counts.get(resource, 0)
            )
        self.metrics.set_resource_actual(
            "task", state_counts.get(TaskStatus.RUNNING, 0)
        )
        for state in TaskStatus:
            self.metrics.set_queue_depth(state.value, state_counts.get(state, 0))

    async def claim_ready(
        self,
        *,
        owner: str,
        limit: int,
        now: datetime | None = None,
    ) -> tuple[TaskClaim, ...]:
        if limit < 1:
            return ()
        current_time = now or datetime.now(UTC)
        claims: list[TaskClaim] = []
        async with self.database.transaction() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:lock_key)"),
                {"lock_key": self._ADMISSION_LOCK_KEY},
            )
            candidates = tuple(
                (
                    await session.scalars(
                        select(TaskRow)
                        .where(TaskRow.state == TaskStatus.READY)
                        .order_by(TaskRow.priority.desc(), TaskRow.created_at, TaskRow.id)
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            for task in candidates:
                request = self._resource_request(task)
                snapshot = await self._admission_snapshot(session, task.project_id)
                if not evaluate_admission(self.policy, snapshot, request).admitted:
                    continue
                expires_at = current_time + self.lease_ttl
                token = uuid4()
                await self.repository.transition_task(
                    session,
                    project_id=task.project_id,
                    task_id=task.id,
                    expected_version=task.version,
                    target=TaskStatus.LEASED,
                )
                await self.repository.create_lease(
                    session,
                    task_id=task.id,
                    owner=owner,
                    token=token,
                    expires_at=expires_at,
                )
                await self.repository.create_reservation(
                    session,
                    task_id=task.id,
                    project_id=task.project_id,
                    resource="task",
                    units=1,
                )
                if request.model_slots:
                    await self.repository.create_reservation(
                        session,
                        task_id=task.id,
                        project_id=task.project_id,
                        resource="model",
                        units=request.model_slots,
                    )
                if request.sandbox_slots:
                    await self.repository.create_reservation(
                        session,
                        task_id=task.id,
                        project_id=task.project_id,
                        resource="sandbox",
                        units=request.sandbox_slots,
                    )
                claims.append(
                    TaskClaim(
                        task_id=task.id,
                        project_id=task.project_id,
                        owner=owner,
                        token=token,
                        expires_at=expires_at,
                    )
                )
        return tuple(claims)

    async def promote_dependency_ready(self, *, limit: int = 500) -> int:
        """Promote PENDING tasks only after every durable dependency is COMPLETED."""
        promoted = 0
        async with self.database.transaction() as session:
            candidates = tuple(
                (
                    await session.scalars(
                        select(TaskRow)
                        .where(TaskRow.state == TaskStatus.PENDING)
                        .order_by(TaskRow.created_at, TaskRow.id)
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            for task in candidates:
                dependency_ids = tuple(UUID(value) for value in task.dependencies)
                if not dependency_ids:
                    ready = True
                else:
                    states = {
                        row.id: row.state
                        for row in (
                            await session.scalars(
                                select(TaskRow).where(TaskRow.id.in_(dependency_ids))
                            )
                        ).all()
                    }
                    ready = dependencies_satisfied(dependency_ids, states)
                if ready:
                    await self.repository.transition_task(
                        session,
                        project_id=task.project_id,
                        task_id=task.id,
                        expected_version=task.version,
                        target=TaskStatus.READY,
                    )
                    promoted += 1
        return promoted

    async def heartbeat(
        self,
        *,
        task_id: UUID,
        owner: str,
        token: UUID,
        now: datetime | None = None,
    ) -> bool:
        current_time = now or datetime.now(UTC)
        async with self.database.transaction() as session:
            task_state = await session.scalar(
                select(TaskRow.state).where(TaskRow.id == task_id)
            )
            if task_state in {
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            }:
                return False
            result = await session.execute(
                update(LeaseRow)
                .where(
                    LeaseRow.task_id == task_id,
                    LeaseRow.owner == owner,
                    LeaseRow.token == token,
                )
                .values(
                    heartbeat_at=current_time,
                    expires_at=current_time + self.lease_ttl,
                )
            )
            return bool(getattr(result, "rowcount", 0))

    async def start_claim(
        self,
        *,
        task_id: UUID,
        project_id: UUID,
        owner: str,
        token: UUID,
        attempt_id: UUID,
        agent_spec_hash: str,
        now: datetime | None = None,
    ) -> TaskExecutionLease:
        """Validate an exact dispatch lease and atomically enter RUNNING."""
        current_time = now or datetime.now(UTC)
        async with self.database.transaction() as session:
            task = await session.scalar(
                select(TaskRow)
                .where(TaskRow.id == task_id, TaskRow.project_id == project_id)
                .with_for_update()
            )
            if task is None:
                raise LookupError(f"task {task_id} does not exist in project {project_id}")
            run = await session.get(RunRow, task.run_id)
            repository = await session.get(RepositoryRow, task.repository_id)
            if run is None or repository is None:
                raise RuntimeError("task run or repository is missing")
            if task.state is TaskStatus.COMPLETED:
                return self._execution_lease(
                    task, run, repository, attempt_id, already_terminal=True
                )
            lease = await session.scalar(
                select(LeaseRow).where(LeaseRow.task_id == task_id).with_for_update()
            )
            if (
                lease is None
                or lease.owner != owner
                or lease.token != token
                or lease.expires_at <= current_time
            ):
                raise PermissionError("worker dispatch lease is missing, expired, or mismatched")
            if task.state is TaskStatus.LEASED:
                await self.repository.transition_task(
                    session,
                    project_id=project_id,
                    task_id=task_id,
                    expected_version=task.version,
                    target=TaskStatus.RUNNING,
                )
            elif task.state is not TaskStatus.RUNNING:
                raise PermissionError(f"worker cannot start task in {task.state.value}")
            attempt = await session.get(TaskAttemptRow, attempt_id)
            if attempt is None:
                await self.repository.create_attempt(
                    session,
                    attempt_id=attempt_id,
                    task_id=task_id,
                    agent_spec_hash=agent_spec_hash,
                )
            elif attempt.task_id != task_id or attempt.agent_spec_hash != agent_spec_hash:
                raise PermissionError("worker attempt identity does not match dispatch")
            return self._execution_lease(task, run, repository, attempt_id)

    async def finish_claim(
        self,
        *,
        task_id: UUID,
        project_id: UUID,
        owner: str,
        token: UUID,
        attempt_id: UUID,
        target: TaskStatus,
    ) -> None:
        if target not in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
            raise ValueError("worker claim may finish only in a terminal state")
        current_time = datetime.now(UTC)
        async with self.database.transaction() as session:
            task = await session.scalar(
                select(TaskRow)
                .where(TaskRow.id == task_id, TaskRow.project_id == project_id)
                .with_for_update()
            )
            if task is None:
                raise LookupError(f"task {task_id} does not exist in project {project_id}")
            lease = await session.scalar(
                select(LeaseRow).where(LeaseRow.task_id == task_id).with_for_update()
            )
            if task.state is target:
                # Idempotent replay must not skip ownership validation or leak
                # reservations: clean up any surviving lease state before returning.
                if lease is not None:
                    if lease.owner != owner or lease.token != token:
                        raise PermissionError(
                            "worker cannot finish a claim owned by another dispatcher"
                        )
                    await self._release_reservations(session, task_id, current_time)
                    await session.delete(lease)
                return
            if lease is None or lease.owner != owner or lease.token != token:
                raise PermissionError("worker cannot finish a claim owned by another dispatcher")
            graph = await session.scalar(
                select(GraphExecutionRow).where(GraphExecutionRow.task_id == task_id)
            )
            required_graph_state = {
                TaskStatus.COMPLETED: GraphExecutionState.COMPLETED,
                TaskStatus.FAILED: GraphExecutionState.FAILED,
                TaskStatus.CANCELLED: GraphExecutionState.CANCELLED,
            }[target]
            if graph is None or graph.state is not required_graph_state:
                raise RuntimeError(
                    f"cannot finish task {target.value} before graph is "
                    f"{required_graph_state.value}"
                )
            await self.repository.transition_task(
                session,
                project_id=project_id,
                task_id=task_id,
                expected_version=task.version,
                target=target,
            )
            attempt = await session.get(TaskAttemptRow, attempt_id)
            if attempt is None or attempt.task_id != task_id:
                raise PermissionError("worker attempt does not belong to task")
            attempt.status = target.value
            attempt.ended_at = current_time
            await self._release_reservations(session, task_id, current_time)
            await session.delete(lease)

    @staticmethod
    def _execution_lease(
        task: TaskRow,
        run: RunRow,
        repository: RepositoryRow,
        attempt_id: UUID,
        *,
        already_terminal: bool = False,
    ) -> TaskExecutionLease:
        return TaskExecutionLease(
            task_id=task.id,
            run_id=task.run_id,
            project_id=task.project_id,
            repository_id=task.repository_id,
            attempt_id=attempt_id,
            baseline_commit=run.baseline_commit,
            source_path=repository.source_path,
            task_type=task.task_type,
            goal=task.description,
            allowed_tools=tuple(str(value) for value in task.allowed_tools),
            risk_ceiling=RiskLevel(task.risk_ceiling),
            dependencies=tuple(UUID(value) for value in task.dependencies),
            already_terminal=already_terminal,
        )

    async def reclaim_expired(self, *, now: datetime | None = None) -> int:
        current_time = now or datetime.now(UTC)
        reclaimed = 0
        async with self.database.transaction() as session:
            leases = tuple(
                (
                    await session.scalars(
                        select(LeaseRow)
                        .where(LeaseRow.expires_at <= current_time)
                        .order_by(LeaseRow.expires_at, LeaseRow.task_id)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            for lease in leases:
                task = await session.scalar(
                    select(TaskRow).where(TaskRow.id == lease.task_id).with_for_update()
                )
                if task is not None and task.state is TaskStatus.RUNNING:
                    # RUNNING work may have a completed or divergent LangGraph checkpoint.
                    # Reconciliation is the only authority allowed to resolve that pair.
                    continue
                if task is not None and task.state is TaskStatus.LEASED:
                    await self.repository.transition_task(
                        session,
                        project_id=task.project_id,
                        task_id=task.id,
                        expected_version=task.version,
                        target=TaskStatus.READY,
                    )
                await self._release_reservations(session, lease.task_id, current_time)
                await session.delete(lease)
                reclaimed += 1
        return reclaimed

    async def cancel_task(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        notify: Callable[[UUID], Awaitable[None]],
    ) -> None:
        current_time = datetime.now(UTC)
        async with self.database.transaction() as session:
            task = await session.scalar(
                select(TaskRow)
                .where(TaskRow.id == task_id, TaskRow.project_id == project_id)
                .with_for_update()
            )
            if task is None:
                raise LookupError(f"task {task_id} does not exist in project {project_id}")
            if task.state is not TaskStatus.CANCELLED:
                await self.repository.transition_task(
                    session,
                    project_id=project_id,
                    task_id=task_id,
                    expected_version=task.version,
                    target=TaskStatus.CANCELLED,
                )
            await self._release_reservations(session, task_id, current_time)
            await session.execute(delete(LeaseRow).where(LeaseRow.task_id == task_id))
        await notify(task_id)

    async def record_observed_usage(
        self,
        *,
        project_id: UUID,
        task_type: TaskType,
        observation: ResourceObservation,
    ) -> None:
        async with self.database.transaction() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:lock_key)"),
                {"lock_key": self._ADMISSION_LOCK_KEY},
            )
            row = await session.scalar(
                select(ProjectTaskResourceEstimateRow)
                .where(
                    ProjectTaskResourceEstimateRow.project_id == project_id,
                    ProjectTaskResourceEstimateRow.task_type == task_type,
                )
                .with_for_update()
            )
            if row is None:
                session.add(
                    ProjectTaskResourceEstimateRow(
                        project_id=project_id,
                        task_type=task_type,
                        sample_count=1,
                        average_cpu_time_ms=observation.cpu_time_ms,
                        peak_memory_bytes=observation.peak_memory_bytes,
                        average_duration_ms=observation.duration_ms,
                        average_output_bytes=observation.output_bytes,
                        average_network_requests=observation.network_requests,
                        average_model_tokens=observation.model_tokens,
                        average_cost_usd=observation.cost_usd,
                    )
                )
                return

            count = row.sample_count
            next_count = count + 1

            def average(previous: float, observed: int | float) -> float:
                return ((previous * count) + observed) / next_count

            row.sample_count = next_count
            row.average_cpu_time_ms = average(row.average_cpu_time_ms, observation.cpu_time_ms)
            row.peak_memory_bytes = max(row.peak_memory_bytes, observation.peak_memory_bytes)
            row.average_duration_ms = average(row.average_duration_ms, observation.duration_ms)
            row.average_output_bytes = average(row.average_output_bytes, observation.output_bytes)
            row.average_network_requests = average(
                row.average_network_requests, observation.network_requests
            )
            row.average_model_tokens = average(row.average_model_tokens, observation.model_tokens)
            row.average_cost_usd = average(row.average_cost_usd, observation.cost_usd)
            row.updated_at = datetime.now(UTC)

    @staticmethod
    def _resource_request(task: TaskRow) -> ResourceRequest:
        return ResourceRequest(
            model_slots=1 if int(task.estimate.get("model_tokens", 0)) > 0 else 0,
            sandbox_slots=min(1, int(task.estimate.get("sandbox_slots", 0))),
        )

    async def _admission_snapshot(
        self, session: AsyncSession, project_id: UUID
    ) -> AdmissionSnapshot:
        active = ReservationRow.released_at.is_(None)

        async def total(resource: str, *, scoped: bool = False) -> int:
            statement = select(func.coalesce(func.sum(ReservationRow.units), 0)).where(
                active,
                ReservationRow.resource == resource,
            )
            if scoped:
                statement = statement.where(ReservationRow.project_id == project_id)
            value = await session.scalar(statement)
            return int(value or 0)

        return AdmissionSnapshot(
            active_tasks=await total("task"),
            active_project_tasks=await total("task", scoped=True),
            active_model_slots=await total("model"),
            active_sandbox_slots=await total("sandbox"),
        )

    @staticmethod
    async def _release_reservations(
        session: AsyncSession, task_id: UUID, released_at: datetime
    ) -> None:
        await session.execute(
            update(ReservationRow)
            .where(
                ReservationRow.task_id == task_id,
                ReservationRow.released_at.is_(None),
            )
            .values(released_at=released_at)
        )
