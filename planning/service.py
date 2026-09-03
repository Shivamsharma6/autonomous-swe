from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from agents.base import (
    AgentAttemptRecord,
    AgentInvocation,
    AgentRunResult,
    AgentRuntime,
    StructuredOutputExhausted,
    UsageRecorder,
)
from agents.configuration import ModelRuntimeFactory
from agents.gateway import FailureClass, GatewayError, ModelGateway
from agents.specs import AgentRole, build_agent_specs
from domain.enums import RiskLevel, RunStatus, TaskStatus, TaskType
from domain.events import require_run_transition
from domain.models import PlanLimits, TaskPlan, TaskSpec
from domain.task_policy import (
    TASK_CAPABILITY_NAMES,
    TASK_REQUIRED_TOOLS,
    planner_execution_contract,
)
from execution.repositories import RepositoryAdapterRegistry
from knowledge.memory.port import MemoryPort
from persistence.database import Database
from persistence.repositories import DomainRepository
from persistence.tables import (
    ModelCallRow,
    RepositoryRow,
    RunRow,
    RunStageAttemptRow,
    TaskRow,
    utc_now,
)
from observability.logging import get_structured_logger
from planning.validator import TaskPlanValidator, ValidationIssue

logger = get_structured_logger("planning")

RepositoryContextProvider = Callable[[str], dict[str, Any]]
_OWNERSHIP_POLL_SECONDS = 0.25


class InvalidTaskPlan(ValueError):
    def __init__(self, issues: tuple[ValidationIssue, ...]) -> None:
        self.issues = issues
        reasons = ", ".join(
            f"{issue['code']}:{issue['message']}" for issue in _validation_feedback(issues)
        )
        super().__init__(f"architect produced an invalid task DAG: {reasons}")


class PlanningOwnershipLost(RuntimeError):
    pass


def _validation_feedback(issues: tuple[ValidationIssue, ...]) -> list[dict[str, Any]]:
    # These validator messages include arbitrary model-selected text. Keep
    # actionable policy guidance without publishing those values to Activity.
    safe_messages = {
        "UNSUPPORTED_TOOL": "Use only registered tools listed in allowed_tools.",
        "REPOSITORY_PATH_ESCAPE": "Use paths within the managed repository.",
    }
    return [
        {
            "code": issue.code,
            "message": safe_messages.get(issue.code, issue.message),
            "task_id": str(issue.task_id) if issue.task_id is not None else None,
        }
        for issue in issues[:50]
    ]


def _failure_details(error: Exception | None) -> dict[str, Any]:
    # Provider exceptions can include credentials, URLs and response bodies.
    # Classify them; never serialize raw exception text into prompts or events.
    if isinstance(error, InvalidTaskPlan):
        return {
            "error_code": "INVALID_TASK_PLAN",
            "message": "The generated task plan still violates execution policy after repair.",
            "validation_issues": _validation_feedback(error.issues),
            "validation_issue_count": len(error.issues),
        }
    if isinstance(error, StructuredOutputExhausted):
        return {
            "error_code": "INVALID_MODEL_OUTPUT",
            "message": "The planning model did not return a valid task plan. Check model support.",
        }
    if isinstance(error, GatewayError):
        if error.failure_class is FailureClass.TIMEOUT:
            return {
                "error_code": "MODEL_TIMEOUT",
                "message": (
                    "The planning model timed out. Check provider availability "
                    "and timeout settings."
                ),
            }
        return {
            "error_code": "MODEL_PROVIDER_ERROR",
            "message": (
                "The planning model provider failed. Check connection, credentials "
                "and model support."
            ),
            "failure_class": error.failure_class.value,
        }
    return {
        "error_code": "PLANNING_ERROR",
        "message": "Planning could not finish. Inspect dispatcher diagnostics before retrying.",
    }


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
        model_factory: ModelRuntimeFactory | None = None,
    ) -> None:
        self._database = database
        self._gateway = gateway
        self._model_factory = model_factory
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
            enforce_execution_policy=True,
        )

    async def plan_next(self) -> TaskPlan | None:
        claimed = await self._claim_run()
        if claimed is None:
            return None
        run, repository, stage_attempt_id, fence_started_at = claimed
        configured = (
            self._model_factory.resolve(run.model_configuration) if self._model_factory else None
        )
        runtime = AgentRuntime(
            configured.apply_spec(self._spec) if configured else self._spec,
            configured.gateway if configured else self._gateway,
            input_type=AgentInvocation,
            output_type=TaskPlan,
            memory=self._memory,
            usage_recorder=RunStageUsageRecorder(
                self._database,
                stage_attempt_id=stage_attempt_id,
            ),
        )
        last_error: Exception | None = None
        previous_candidate: TaskPlan | None = None
        validation_issues: tuple[ValidationIssue, ...] = ()
        try:
            repository_context = await asyncio.to_thread(
                self._repository_context, repository.source_path
            )
        except Exception:
            await self._fail_run(
                run.id,
                stage_attempt_id=stage_attempt_id,
                fence_started_at=fence_started_at,
                repository_context_failed=True,
            )
            raise
        base_payload: dict[str, object] = {
            "available_task_types": [
                "RESEARCH",
                "IMPLEMENTATION",
                "TEST",
                "REFACTOR",
                "DOCUMENTATION",
                "VALIDATION",
            ],
            "allowed_tools": sorted(self._validator.allowed_tools),
            "task_execution_contract": planner_execution_contract(),
            "platform_limits": self._limits.model_dump(mode="json"),
            "max_risk_ceiling": self._validator.max_risk_ceiling.value,
            "repository": repository_context,
            "requirements": (
                "Create the smallest sufficient typed DAG. Independent tasks should "
                "have no artificial dependencies. Tasks that run tests against newly "
                "implemented behavior must depend on that implementation so they receive "
                "its code, rather than testing an unchanged baseline. Keep tightly coupled "
                "edits together instead of assigning duplicate changes to parallel tasks. "
                "Every task needs testable acceptance "
                "criteria, bounded budgets, and only registered tools from allowed_tools. "
                "The DAG must end in exactly one VALIDATION task that transitively depends "
                "on every other task. Use only the task types needed for this goal; "
                "they are alternatives, not a required checklist. Choose assigned_capability, "
                "required tools and risk from task_execution_contract. Ground tasks in the "
                "repository manifest; do not invent issue trackers or unrelated source paths."
            ),
        }
        for attempt in range(3):
            if not await self._is_current_owner(run.id, stage_attempt_id, fence_started_at):
                return None
            try:
                payload: dict[str, object] = dict(base_payload)
                if last_error is not None:
                    payload["previous_attempt_errors"] = _failure_details(last_error)
                if previous_candidate is not None:
                    payload["previous_candidate"] = previous_candidate.model_dump(mode="json")
                    payload["validation_issues"] = _validation_feedback(validation_issues)
                    payload["repair_hint"] = (
                        "Repair previous_candidate using validation_issues. Preserve task IDs, "
                        "valid tasks and dependencies unless a reported issue requires changing "
                        "them. Return the complete repaired plan; do not regenerate unrelated work."
                    )
                result = await self._run_while_owned(
                    runtime,
                    AgentInvocation(
                        trace_id=f"run:{run.id}:stage:architect:attempt:{attempt}",
                        run_id=run.id,
                        task_id=run.id,
                        attempt_id=stage_attempt_id,
                        project_id=run.project_id,
                        repository_id=run.repository_id,
                        baseline_commit=run.baseline_commit,
                        goal=run.goal,
                        input_payload=payload,
                    ),
                    fence_started_at=fence_started_at,
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
                            self._assign_required_tools(task).model_copy(
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
                previous_candidate = plan
                self._validate_scope(plan, run)
                validation = self._validator.validate(plan)
                validation_issues = validation.issues
                if not validation.valid:
                    logger.warning(
                        "plan_validation_failed",
                        run_id=str(run.id),
                        attempt=attempt,
                        issues=[
                            {
                                "code": i.code,
                                "message": i.message,
                                "task_id": str(i.task_id) if i.task_id else None,
                            }
                            for i in validation_issues
                        ],
                    )
                    raise InvalidTaskPlan(validation_issues)
                return await self._persist_plan(
                    plan,
                    stage_attempt_id=stage_attempt_id,
                    fence_started_at=fence_started_at,
                )
            except PlanningOwnershipLost:
                return None
            except Exception as error:
                last_error = error
                if attempt == 2:
                    await self._fail_run(
                        run.id,
                        stage_attempt_id=stage_attempt_id,
                        fence_started_at=fence_started_at,
                        error=error,
                        attempts=attempt + 1,
                    )
                    raise
                # Retry with validation error feedback
                continue
        # Should be unreachable — loop either returns or raises
        assert last_error is not None
        await self._fail_run(
            run.id,
            stage_attempt_id=stage_attempt_id,
            fence_started_at=fence_started_at,
            error=last_error,
            attempts=3,
        )
        raise last_error

    async def _run_while_owned(
        self,
        runtime: AgentRuntime[TaskPlan],
        invocation: AgentInvocation,
        *,
        fence_started_at: datetime,
    ) -> AgentRunResult[TaskPlan]:
        # Scope cancellation around the whole runtime, including model admission,
        # HTTP calls and retry backoff. Cancelling its task propagates through the
        # gateway's awaited HTTP task without creating a separate gateway watcher.
        operation = asyncio.create_task(runtime.run(invocation))
        ownership = asyncio.create_task(
            self._watch_planning_owner(invocation.run_id, invocation.attempt_id, fence_started_at)
        )
        try:
            done, _ = await asyncio.wait(
                {operation, ownership}, return_when=asyncio.FIRST_COMPLETED
            )
            if ownership in done:
                await ownership
                raise PlanningOwnershipLost("run is no longer owned by the planning stage")
            return await operation
        finally:
            # Also drain both children on caller cancellation and provider/poll
            # failures. No old runtime may retain a model slot or keep retrying.
            for task in (operation, ownership):
                if not task.done():
                    task.cancel()
            await asyncio.gather(operation, ownership, return_exceptions=True)

    async def _watch_planning_owner(
        self, run_id: UUID, stage_attempt_id: UUID, fence_started_at: datetime
    ) -> None:
        while await self._is_current_owner(  # noqa: ASYNC110 - ownership changes in other processes
            run_id, stage_attempt_id, fence_started_at
        ):
            await asyncio.sleep(_OWNERSHIP_POLL_SECONDS)

    def _assign_required_tools(self, task: TaskSpec) -> TaskSpec:
        # The model does not decide whether an assigned stage can execute its
        # mandatory operations. Fill only its static, registered requirements;
        # keep unknown tools and invalid capability/risk selections for rejection.
        if task.assigned_capability not in TASK_CAPABILITY_NAMES[task.task_type]:
            return task
        missing = (TASK_REQUIRED_TOOLS[task.task_type] & self._validator.allowed_tools) - set(
            task.allowed_tools
        )
        return task.model_copy(update={"allowed_tools": (*task.allowed_tools, *sorted(missing))})

    async def _is_current_owner(
        self, run_id: UUID, stage_attempt_id: UUID, fence_started_at: datetime
    ) -> bool:
        async with self._database.sessions() as session:
            return bool(
                await session.scalar(
                    select(RunRow.id)
                    .join(RunStageAttemptRow, RunStageAttemptRow.run_id == RunRow.id)
                    .where(
                        RunRow.id == run_id,
                        RunRow.state == RunStatus.PLANNING.value,
                        RunRow.cancellation_requested_at.is_(None),
                        RunStageAttemptRow.id == stage_attempt_id,
                        RunStageAttemptRow.status == "RUNNING",
                        RunStageAttemptRow.started_at == fence_started_at,
                    )
                )
            )

    async def _claim_run(
        self,
    ) -> tuple[RunRow, RepositoryRow, UUID, datetime] | None:
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
                .where(RunRow.cancellation_requested_at.is_(None))
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
            configured = (
                self._model_factory.resolve(run.model_configuration)
                if self._model_factory
                else None
            )
            spec = configured.apply_spec(self._spec) if configured else self._spec
            await session.execute(
                insert(RunStageAttemptRow)
                .values(
                    id=attempt_id,
                    run_id=run.id,
                    stage="architect",
                    agent_spec_hash=spec.spec_hash,
                    status="RUNNING",
                    started_at=now,
                )
                .on_conflict_do_update(
                    index_elements=[RunStageAttemptRow.run_id, RunStageAttemptRow.stage],
                    # A stale-takeover restarts the stage clock; this timestamp
                    # doubles as the ownership fence checked at persist time.
                    set_={"status": "RUNNING", "started_at": now, "ended_at": None},
                )
            )
            return run, repository, attempt_id, now

    async def _persist_plan(
        self,
        plan: TaskPlan,
        *,
        stage_attempt_id: UUID,
        fence_started_at: datetime,
    ) -> TaskPlan:
        now = datetime.now(UTC)
        async with self._database.transaction() as session:
            run = await session.scalar(
                select(RunRow).where(RunRow.id == plan.run_id).with_for_update()
            )
            if (
                run is None
                or run.state != RunStatus.PLANNING.value
                or run.cancellation_requested_at is not None
            ):
                raise PlanningOwnershipLost("run is no longer owned by the planning stage")
            existing = await session.scalar(
                select(RunStageAttemptRow).where(RunStageAttemptRow.id == stage_attempt_id)
            )
            if existing is None or existing.run_id != run.id or existing.status != "RUNNING":
                raise PlanningOwnershipLost("planning attempt is no longer running")
            if existing.started_at != fence_started_at:
                raise PlanningOwnershipLost("planning stage was taken over by another dispatcher")

            # Remap any task IDs that already exist in the database from other runs
            task_ids = [t.id for t in plan.tasks]
            existing_task_ids = set(
                await session.scalars(
                    select(TaskRow.id).where(TaskRow.id.in_(task_ids))
                )
            )
            if existing_task_ids:
                id_map = {
                    t.id: (uuid5(run.id, str(t.id)) if t.id in existing_task_ids else t.id)
                    for t in plan.tasks
                }
                resolved_tasks = []
                for task_spec in plan.tasks:
                    new_id = id_map[task_spec.id]
                    new_deps = tuple(id_map.get(dep, dep) for dep in task_spec.dependencies)
                    resolved_tasks.append(
                        task_spec.model_copy(
                            update={"id": new_id, "dependencies": new_deps}
                        )
                    )
                plan = plan.model_copy(update={"tasks": tuple(resolved_tasks)})

            self._require_integration_sink(plan)

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
            return plan

    async def _fail_run(
        self,
        run_id: UUID,
        *,
        stage_attempt_id: UUID,
        fence_started_at: datetime,
        error: Exception | None = None,
        attempts: int = 0,
        repository_context_failed: bool = False,
    ) -> None:
        now = datetime.now(UTC)
        async with self._database.transaction() as session:
            run = await session.scalar(select(RunRow).where(RunRow.id == run_id).with_for_update())
            attempt = await session.get(RunStageAttemptRow, stage_attempt_id)
            if (
                run is None
                or run.state != RunStatus.PLANNING.value
                or run.cancellation_requested_at is not None
                or attempt is None
                or attempt.run_id != run.id
                or attempt.status != "RUNNING"
                or attempt.started_at != fence_started_at
            ):
                return
            require_run_transition(RunStatus(run.state), RunStatus.FAILED)
            await self._record_run_duration(session, run, exited_at=now)
            run.state = RunStatus.FAILED.value
            run.state_entered_at = now
            attempt.status = "FAILED"
            attempt.ended_at = now
            details = _failure_details(error)
            if repository_context_failed:
                details = {
                    "error_code": "REPOSITORY_CONTEXT_ERROR",
                    "message": (
                        "Planning could not inspect the repository. Check its path, access "
                        "permissions and supported project files before retrying."
                    ),
                }
            payload = {
                "run_id": str(run.id),
                "stage": "architect",
                "stage_attempt_id": str(attempt.id),
                "attempts": attempts,
                **details,
            }
            event_id = uuid5(
                NAMESPACE_URL,
                f"run-planning-failed:{run.id}:{attempt.id}:{fence_started_at.isoformat()}",
            )
            await self._repository.append_audit(
                session,
                event_id=event_id,
                event_type="run.planning_failed",
                aggregate_type="run",
                aggregate_id=run.id,
                payload=payload,
                correlation_id=run.id,
                causation_id=attempt.id,
            )
            await self._repository.enqueue_event(
                session,
                event_id=event_id,
                topic="run-state",
                payload=payload,
            )

    async def _record_run_duration(self, session: Any, run: RunRow, *, exited_at: datetime) -> None:
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
            sink.task_type is TaskType.VALIDATION and ancestors(sink.id) | {sink.id} == all_ids
            for sink in sinks
        ):
            raise ValueError(
                "task DAG requires a final VALIDATION sink transitively depending on every task"
            )


def default_repository_context(source_path: str) -> dict[str, Any]:
    root = Path(source_path).resolve(strict=True)
    adapter = RepositoryAdapterRegistry.default().detect(root)
    manifest = adapter.inspect(root)
    # Adapter source lists describe their own language. Keep a bounded broader
    # inventory so e.g. a Python harness does not hide the actual HTML game.
    files: list[str] = []
    excluded = {"node_modules", "vendor", "dist", "build", "__pycache__"}
    for directory, subdirectories, filenames in root.walk(follow_symlinks=False):
        subdirectories[:] = sorted(
            name for name in subdirectories if not name.startswith(".") and name not in excluded
        )
        for name in sorted(filenames):
            candidate = directory / name
            if not name.startswith(".") and not candidate.is_symlink():
                files.append(candidate.relative_to(root).as_posix())
                if len(files) >= 2_000:
                    break
        if len(files) >= 2_000:
            break
    return {
        "adapter": manifest.adapter,
        "lockfile": manifest.lockfile,
        "source_files": list(manifest.source_files[:5_000]),
        "test_files": list(manifest.test_files[:5_000]),
        "metadata_files": list(manifest.metadata_files),
        "dependencies": sorted(manifest.dependencies)[:5_000],
        "repository_files": files,
    }
