from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from agents.base import AgentAttemptRecord, AgentInvocation, AgentRuntime, UsageRecorder
from agents.gateway import ModelGateway
from agents.specs import AgentRole, build_agent_specs
from domain.enums import RiskLevel, RunStatus, TaskStatus, TaskType
from domain.events import require_run_transition
from domain.models import PlanLimits, TaskPlan
from execution.repositories import RepositoryAdapterRegistry
from knowledge.memory.port import MemoryPort
from persistence.database import Database
from persistence.repositories import DomainRepository
from persistence.tables import ModelCallRow, RepositoryRow, RunRow, RunStageAttemptRow, utc_now
from planning.validator import TaskPlanValidator

RepositoryContextProvider = Callable[[str], dict[str, Any]]


class RunStageUsageRecorder(UsageRecorder):
    def __init__(self, database: Database, *, stage_attempt_id: UUID) -> None:
        self._database = database
        self._stage_attempt_id = stage_attempt_id

    async def record(self, attempt: AgentAttemptRecord) -> None:
        record_id = uuid5(
            NAMESPACE_URL,
            f"run-model-call:{attempt.run_id}:{attempt.trace_id}:{attempt.turn}",
        )
        async with self._database.transaction() as session:
            await session.execute(
                insert(ModelCallRow)
                .values(
                    id=record_id,
                    run_id=attempt.run_id,
                    task_id=None,
                    attempt_id=None,
                    run_stage_attempt_id=self._stage_attempt_id,
                    trace_id=attempt.trace_id,
                    provider_request_id=attempt.provider_request_id,
                    model=attempt.model,
                    turn=attempt.turn,
                    agent_spec_hash=attempt.agent_spec_hash,
                    input_tokens=attempt.usage.input_tokens,
                    output_tokens=attempt.usage.output_tokens,
                    cached_input_tokens=attempt.usage.cached_input_tokens,
                    cost_usd=attempt.usage.cost_usd,
                    failure_class=attempt.failure_class.value if attempt.failure_class else None,
                    validation_errors=list(attempt.validation_errors),
                    tool_call_ids=list(attempt.tool_call_ids),
                    created_at=utc_now(),
                )
                .on_conflict_do_nothing(constraint="uq_model_call_invocation_turn")
            )


class RunPlanningService:
    """Durably turn accepted runs into bounded, scheduler-ready dynamic task DAGs."""

    def __init__(
        self,
        *,
        database: Database,
        gateway: ModelGateway,
        memory: MemoryPort,
        primary_model: str,
        fallback_models: tuple[str, ...],
        limits: PlanLimits,
        max_risk_ceiling: RiskLevel = RiskLevel.HIGH,
        repository_context: RepositoryContextProvider | None = None,
        repository: DomainRepository | None = None,
        stale_after: timedelta = timedelta(seconds=30),
    ) -> None:
        self._database = database
        self._gateway = gateway
        self._memory = memory
        self._limits = limits
        self._repository_context = repository_context or default_repository_context
        self._repository = repository or DomainRepository()
        self._stale_after = stale_after
        self._spec = build_agent_specs(
            primary_model=primary_model,
            fallback_models=fallback_models,
        )[AgentRole.ARCHITECT]
        self._validator = TaskPlanValidator(
            allowed_tools={"read_file", "search_code", "apply_patch", "run_tests"},
            require_final_validation_sink=True,
            max_risk_ceiling=max_risk_ceiling,
        )

    async def plan_next(self) -> TaskPlan | None:
        claimed = await self._claim_run()
        if claimed is None:
            return None
        run, repository, stage_attempt_id = claimed
        runtime = AgentRuntime(
            self._spec,
            self._gateway,
            input_type=AgentInvocation,
            output_type=TaskPlan,
            memory=self._memory,
            usage_recorder=RunStageUsageRecorder(
                self._database,
                stage_attempt_id=stage_attempt_id,
            ),
        )
        try:
            result = await runtime.run(
                AgentInvocation(
                    trace_id=f"run:{run.id}:stage:architect",
                    run_id=run.id,
                    task_id=run.id,
                    attempt_id=stage_attempt_id,
                    project_id=run.project_id,
                    repository_id=run.repository_id,
                    baseline_commit=run.baseline_commit,
                    goal=run.goal,
                    input_payload={
                        "required_task_types": [
                            "RESEARCH",
                            "IMPLEMENTATION",
                            "TEST",
                            "REFACTOR",
                            "DOCUMENTATION",
                            "VALIDATION",
                        ],
                        "platform_limits": self._limits.model_dump(mode="json"),
                        "repository": self._repository_context(repository.source_path),
                        "requirements": (
                            "Create the smallest sufficient typed DAG. Independent tasks should "
                            "have no artificial dependencies. Every task needs testable acceptance "
                            "criteria, bounded budgets, and only registered tools."
                        ),
                    },
                )
            )
            plan = result.output.model_copy(
                update={
                    "run_id": run.id,
                    "project_id": run.project_id,
                    "repository_id": run.repository_id,
                    "baseline_commit": run.baseline_commit,
                    "revision": 1,
                    "limits": self._limits,
                    "tasks": tuple(
                        task.model_copy(
                            update={
                                "project_id": run.project_id,
                                "repository_id": run.repository_id,
                                "plan_revision": 1,
                            }
                        )
                        for task in result.output.tasks
                    ),
                }
            )
            self._validate_scope(plan, run)
            validation = self._validator.validate(plan)
            if not validation.valid:
                reasons = ", ".join(issue.code for issue in validation.issues)
                raise ValueError(f"architect produced an invalid task DAG: {reasons}")
            self._require_integration_sink(plan)
            await self._persist_plan(plan, stage_attempt_id=stage_attempt_id)
            return plan
        except Exception:
            await self._fail_run(run.id, stage_attempt_id=stage_attempt_id)
            raise

    async def _claim_run(self) -> tuple[RunRow, RepositoryRow, UUID] | None:
        now = datetime.now(UTC)
        stale_before = now - self._stale_after
        async with self._database.transaction() as session:
            run = await session.scalar(
                select(RunRow)
                .where(
                    (RunRow.state == RunStatus.PENDING.value)
                    | (
                        (RunRow.state == RunStatus.PLANNING.value)
                        & (RunRow.state_entered_at < stale_before)
                    )
                )
                .order_by(RunRow.created_at, RunRow.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if run is None:
                return None
            repository = await session.get(RepositoryRow, run.repository_id)
            if repository is None:
                raise RuntimeError("run repository is missing")
            if run.state != RunStatus.PLANNING.value:
                require_run_transition(RunStatus(run.state), RunStatus.PLANNING)
                await self._record_run_duration(session, run, exited_at=now)
                run.state = RunStatus.PLANNING.value
                run.state_entered_at = now
            attempt_id = uuid5(NAMESPACE_URL, f"run-stage:{run.id}:architect")
            await session.execute(
                insert(RunStageAttemptRow)
                .values(
                    id=attempt_id,
                    run_id=run.id,
                    stage="architect",
                    agent_spec_hash=self._spec.spec_hash,
                    status="RUNNING",
                    started_at=now,
                )
                .on_conflict_do_update(
                    index_elements=[RunStageAttemptRow.run_id, RunStageAttemptRow.stage],
                    set_={"status": "RUNNING", "ended_at": None},
                )
            )
            return run, repository, attempt_id

    async def _persist_plan(self, plan: TaskPlan, *, stage_attempt_id: UUID) -> None:
        now = datetime.now(UTC)
        async with self._database.transaction() as session:
            run = await session.scalar(
                select(RunRow).where(RunRow.id == plan.run_id).with_for_update()
            )
            if run is None or run.state != RunStatus.PLANNING.value:
                raise RuntimeError("run is no longer owned by the planning stage")
            existing = await session.scalar(
                select(RunStageAttemptRow).where(RunStageAttemptRow.id == stage_attempt_id)
            )
            if existing is None:
                raise RuntimeError("planning attempt disappeared")
            await self._repository.create_plan_revision(
                session,
                run_id=run.id,
                revision=plan.revision,
                plan=plan.model_dump(mode="json"),
            )
            for task_spec in plan.tasks:
                task = await self._repository.create_task(
                    session,
                    run_id=run.id,
                    task=task_spec,
                )
                if not task_spec.dependencies:
                    await self._repository.transition_task(
                        session,
                        project_id=task.project_id,
                        task_id=task.id,
                        expected_version=task.version,
                        target=TaskStatus.READY,
                    )
            await self._record_run_duration(session, run, exited_at=now)
            require_run_transition(RunStatus(run.state), RunStatus.EXECUTING)
            run.state = RunStatus.EXECUTING.value
            run.state_entered_at = now
            existing.status = "COMPLETED"
            existing.ended_at = now
            event_id = uuid5(NAMESPACE_URL, f"plan-created:{run.id}:{plan.revision}")
            payload = {
                "run_id": str(run.id),
                "revision": plan.revision,
                "task_ids": [str(task.id) for task in plan.tasks],
            }
            await self._repository.append_audit(
                session,
                event_id=event_id,
                event_type="plan.created",
                aggregate_type="run",
                aggregate_id=run.id,
                payload=payload,
                correlation_id=run.id,
                causation_id=stage_attempt_id,
            )
            await self._repository.enqueue_event(
                session,
                event_id=event_id,
                topic="plan-created",
                payload=payload,
            )

    async def _fail_run(self, run_id: UUID, *, stage_attempt_id: UUID) -> None:
        now = datetime.now(UTC)
        async with self._database.transaction() as session:
            run = await session.scalar(
                select(RunRow).where(RunRow.id == run_id).with_for_update()
            )
            attempt = await session.get(RunStageAttemptRow, stage_attempt_id)
            if run is not None and run.state == RunStatus.PLANNING.value:
                require_run_transition(RunStatus(run.state), RunStatus.FAILED)
                await self._record_run_duration(session, run, exited_at=now)
                run.state = RunStatus.FAILED.value
                run.state_entered_at = now
            if attempt is not None:
                attempt.status = "FAILED"
                attempt.ended_at = now

    async def _record_run_duration(
        self, session: Any, run: RunRow, *, exited_at: datetime
    ) -> None:
        await self._repository.record_state_duration(
            session,
            aggregate_type="workflow",
            aggregate_id=run.id,
            state=run.state,
            entered_at=run.state_entered_at,
            exited_at=exited_at,
        )

    @staticmethod
    def _validate_scope(plan: TaskPlan, run: RunRow) -> None:
        if (
            plan.run_id != run.id
            or plan.project_id != run.project_id
            or plan.repository_id != run.repository_id
            or plan.baseline_commit != run.baseline_commit
            or plan.revision != 1
        ):
            raise ValueError("architect plan identity does not match the claimed run")

    @staticmethod
    def _require_integration_sink(plan: TaskPlan) -> None:
        depended_on = {dependency for task in plan.tasks for dependency in task.dependencies}
        sinks = tuple(task for task in plan.tasks if task.id not in depended_on)
        tasks_by_id = {task.id: task for task in plan.tasks}

        def ancestors(task_id: UUID) -> set[UUID]:
            found: set[UUID] = set()
            pending = list(tasks_by_id[task_id].dependencies)
            while pending:
                dependency = pending.pop()
                if dependency in found:
                    continue
                found.add(dependency)
                pending.extend(tasks_by_id[dependency].dependencies)
            return found

        all_ids = set(tasks_by_id)
        if not any(
            sink.task_type is TaskType.VALIDATION
            and ancestors(sink.id) | {sink.id} == all_ids
            for sink in sinks
        ):
            raise ValueError(
                "task DAG requires a final VALIDATION sink transitively depending on every task"
            )


def default_repository_context(source_path: str) -> dict[str, Any]:
    root = Path(source_path).resolve(strict=True)
    adapter = RepositoryAdapterRegistry.default().detect(root)
    manifest = adapter.inspect(root)
    return {
        "adapter": manifest.adapter,
        "lockfile": manifest.lockfile,
        "source_files": list(manifest.source_files[:5_000]),
        "test_files": list(manifest.test_files[:5_000]),
        "metadata_files": list(manifest.metadata_files),
        "dependencies": sorted(manifest.dependencies)[:5_000],
    }
