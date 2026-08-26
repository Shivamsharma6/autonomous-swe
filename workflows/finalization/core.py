from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import func, select

from agents.base import AgentInvocation, AgentRuntime
from agents.gateway import ModelGateway
from agents.specs import AgentRole, build_agent_specs
from domain.enums import ApprovalStatus, ArtifactState, RiskLevel, RunStatus, TaskStatus
from domain.events import require_run_transition
from domain.models import (
    AgentSpec,
    MemoryCandidate,
    PlanLimits,
    ReleaseDecision,
    TaskPlan,
    TaskPlanMutation,
    ToolCallRequest,
    canonical_sha256,
)
from execution.sandbox.worktrees import GitWorktreeManager
from execution.scheduler.service import SchedulerService
from knowledge.memory.port import MemoryPort
from knowledge.memory.promotion import (
    PromotionGate,
    PromotionOutcome,
    PromotionReview,
    PromotionService,
    deterministic_memory_id,
)
from persistence.artifacts import ArtifactService
from persistence.database import Database
from persistence.repositories import DomainRepository
from persistence.tables import (
    AgentMessageRow,
    ApprovalRow,
    ArtifactRow,
    MemoryCandidateRow,
    PlanRevisionRow,
    RepairMutationRow,
    RepositoryRow,
    RunRow,
    RunStageAttemptRow,
    TaskAttemptRow,
    TaskRow,
    utc_now,
)
from planning.service import RunStageUsageRecorder
from planning.validator import TaskPlanValidator
from tools.approval import ApprovalService
from tools.gateway import ToolGateway
from tools.registry import ToolExecutionContext, ToolRegistry
from workflows.finalization.evidence import (
    integration_sink,
    require_integration_plan,
    verification_evidence,
)
from workflows.finalization.promotion import build_memory_candidate, promotion_review
from workflows.repair import DurableRepairController, RepairAction, VerificationOutcome
from workflows.review import ReleaseReviewer, ReleaseReviewRequest


class RunFinalizationService:
    """Verify completed DAGs, bound repair, exact approval, Git, and UAMS promotion."""

    def __init__(
        self,
        *,
        database: Database,
        gateway: ModelGateway,
        memory: MemoryPort,
        artifacts: ArtifactService,
        scheduler: SchedulerService,
        worktrees: GitWorktreeManager,
        primary_model: str,
        fallback_models: tuple[str, ...],
        limits: PlanLimits,
        repository: DomainRepository | None = None,
    ) -> None:
        self._database = database
        self._gateway = gateway
        self._memory = memory
        self._artifacts = artifacts
        self._scheduler = scheduler
        self._worktrees = worktrees
        self._primary_model = primary_model
        self._fallback_models = fallback_models
        self._limits = limits
        self._repository = repository or DomainRepository()
        self._approvals = ApprovalService(database=database, repository=self._repository)
        self._tool_gateway = ToolGateway(
            database=database,
            registry=ToolRegistry(),
            approvals=self._approvals,
            repository=self._repository,
        )
        self._promotion = PromotionService(
            database,
            memory,
            PromotionGate(),
            repository=self._repository,
        )
        validator = TaskPlanValidator(
            allowed_tools={"read_file", "search_code", "apply_patch", "run_tests"},
            require_final_validation_sink=True,
        )
        self._repair = DurableRepairController(
            database,
            validator=validator,
            artifacts=artifacts,
            repository=self._repository,
        )

    async def advance_next(self) -> str | None:
        async with self._database.sessions() as session:
            run = await session.scalar(
                select(RunRow)
                .where(
                    RunRow.state.in_(
                        (
                            RunStatus.EXECUTING.value,
                            RunStatus.WAITING_FOR_APPROVAL.value,
                            RunStatus.WAITING_FOR_MEMORY.value,
                        )
                    )
                )
                .order_by(RunRow.created_at, RunRow.id)
                .limit(1)
            )
            if run is None:
                return None
            run_id = run.id
            state = run.state
        if state == RunStatus.EXECUTING.value:
            return await self._advance_executing(run_id)
        if state == RunStatus.WAITING_FOR_APPROVAL.value:
            return await self._advance_approval(run_id)
        return await self._advance_memory(run_id)

    async def _advance_executing(self, run_id: UUID) -> str | None:
        async with self._database.sessions() as session:
            tasks = tuple(
                (
                    await session.scalars(
                        select(TaskRow)
                        .where(TaskRow.run_id == run_id)
                        .order_by(TaskRow.plan_revision, TaskRow.id)
                    )
                ).all()
            )
        if not tasks or any(
            task.state
            in {
                TaskStatus.PENDING,
                TaskStatus.READY,
                TaskStatus.LEASED,
                TaskStatus.RUNNING,
                TaskStatus.BLOCKED,
            }
            for task in tasks
        ):
            return None
        if any(task.state in {TaskStatus.FAILED, TaskStatus.CANCELLED} for task in tasks):
            await self._transition_run(run_id, RunStatus.FAILED)
            return "FAILED"
        latest_revision = max(task.plan_revision for task in tasks)
        latest = tuple(task for task in tasks if task.plan_revision == latest_revision)
        failures, evidence_ids = await self._verification_evidence(latest)
        if failures:
            if not evidence_ids:
                await self._transition_run(run_id, RunStatus.FAILED)
                return "FAILED"
            await self._request_repair(
                run_id,
                latest=latest,
                failures=failures,
                evidence_ids=evidence_ids,
            )
            await self._scheduler.promote_dependency_ready()
            return "REPAIR_ADDED"
        decision = await self._review_release(
            run_id,
            latest=latest,
            evidence_ids=evidence_ids,
        )
        if not decision.approved:
            review_failures = decision.failure_reasons or (decision.summary,)
            await self._request_repair(
                run_id,
                latest=latest,
                failures=review_failures,
                evidence_ids=evidence_ids,
            )
            await self._scheduler.promote_dependency_ready()
            return "REPAIR_ADDED"
        await self._request_commit_approval(run_id, latest=latest)
        return "WAITING_FOR_APPROVAL"

    async def _review_release(
        self,
        run_id: UUID,
        *,
        latest: tuple[TaskRow, ...],
        evidence_ids: tuple[UUID, ...],
    ) -> ReleaseDecision:
        sink = self._integration_sink(latest)
        plan = await self._current_plan(run_id)
        self._require_integration_plan(plan)
        criteria = tuple(
            dict.fromkeys(
                criterion
                for task in plan.tasks
                for criterion in task.acceptance_criteria
            )
        )
        if not criteria:
            return ReleaseDecision(
                approved=False,
                summary="The release has no acceptance criteria.",
                failure_reasons=("release has no acceptance criteria",),
            )
        stage = f"final-review:{sink.plan_revision}"
        stage_attempt_id = uuid5(NAMESPACE_URL, f"run-stage:{run_id}:{stage}")
        artifact_id = uuid5(NAMESPACE_URL, f"release-decision:{run_id}:{sink.plan_revision}")
        replay = await self._load_release_decision(
            project_id=sink.project_id,
            artifact_id=artifact_id,
        )
        if replay is not None:
            return replay

        spec = build_agent_specs(
            primary_model=self._primary_model,
            fallback_models=self._fallback_models,
        )[AgentRole.FINAL_REVIEWER]
        async with self._database.transaction() as session:
            await session.merge(
                RunStageAttemptRow(
                    id=stage_attempt_id,
                    run_id=run_id,
                    stage=stage,
                    agent_spec_hash=spec.spec_hash,
                    status="RUNNING",
                )
            )
            run = await session.get(RunRow, run_id)
        if run is None:
            raise LookupError(f"run {run_id} does not exist")

        runtime = AgentRuntime(
            spec,
            self._gateway,
            input_type=AgentInvocation,
            output_type=ReleaseDecision,
            memory=self._memory,
            usage_recorder=RunStageUsageRecorder(
                self._database,
                stage_attempt_id=stage_attempt_id,
            ),
        )
        try:
            result = await runtime.run(
                AgentInvocation(
                    trace_id=f"run:{run_id}:stage:{stage}",
                    run_id=run_id,
                    task_id=sink.id,
                    attempt_id=stage_attempt_id,
                    project_id=run.project_id,
                    repository_id=run.repository_id,
                    baseline_commit=run.baseline_commit,
                    goal=run.goal,
                    input_payload={
                        "acceptance_criteria": list(criteria),
                        "verified_artifact_ids": [str(value) for value in evidence_ids],
                        "task_summaries": [
                            {
                                "task_id": str(task.id),
                                "title": task.title,
                                "task_type": task.task_type.value,
                                "acceptance_criteria": list(task.acceptance_criteria),
                            }
                            for task in plan.tasks
                        ],
                        "requirements": (
                            "Map every acceptance criterion to verified artifact IDs. Reject "
                            "the release if any criterion lacks direct evidence."
                        ),
                    },
                )
            )
            audited = await ReleaseReviewer(
                database=self._database,
                artifacts=self._artifacts,
            ).review(
                ReleaseReviewRequest(
                    project_id=run.project_id,
                    run_id=run.id,
                    acceptance_criteria=criteria,
                    proposed_evidence=result.output.acceptance_evidence,
                    summary=result.output.summary,
                )
            )
            failures = tuple(
                dict.fromkeys((*result.output.failure_reasons, *audited.failure_reasons))
            )
            approved = result.output.approved and audited.approved
            if not approved and not failures:
                failures = ("final reviewer rejected the release",)
            decision = ReleaseDecision(
                approved=approved,
                summary=result.output.summary,
                acceptance_evidence=audited.acceptance_evidence,
                failure_reasons=failures,
            )
            async with self._database.transaction() as session:
                await self._artifacts.put(
                    session,
                    artifact_id=artifact_id,
                    content=decision.model_dump_json().encode(),
                    media_type="application/vnd.autoswe.release-decision+json",
                    project_id=run.project_id,
                    run_id=run.id,
                    task_id=sink.id,
                )
                attempt = await session.get(RunStageAttemptRow, stage_attempt_id)
                if attempt is None:
                    raise RuntimeError("final review attempt disappeared")
                attempt.status = "COMPLETED"
                attempt.ended_at = utc_now()
                event_id = uuid5(NAMESPACE_URL, f"release-reviewed:{run.id}:{sink.plan_revision}")
                payload = {
                    "run_id": str(run.id),
                    "plan_revision": sink.plan_revision,
                    "approved": decision.approved,
                    "artifact_id": str(artifact_id),
                    "failure_reasons": list(decision.failure_reasons),
                }
                await self._repository.append_audit(
                    session,
                    event_id=event_id,
                    event_type="release.reviewed",
                    aggregate_type="run",
                    aggregate_id=run.id,
                    payload=payload,
                    correlation_id=run.id,
                    causation_id=stage_attempt_id,
                )
                await self._repository.enqueue_event(
                    session,
                    event_id=event_id,
                    topic="release-review",
                    payload=payload,
                )
            return decision
        except Exception:
            async with self._database.transaction() as session:
                attempt = await session.get(RunStageAttemptRow, stage_attempt_id)
                if attempt is not None:
                    attempt.status = "FAILED"
                    attempt.ended_at = utc_now()
            raise

    async def _load_release_decision(
        self, *, project_id: UUID, artifact_id: UUID
    ) -> ReleaseDecision | None:
        async with self._database.transaction() as session:
            row = await session.get(ArtifactRow, artifact_id)
            if row is None:
                return None
            content = await self._artifacts.get_verified(
                session,
                project_id=project_id,
                artifact_id=artifact_id,
            )
        return ReleaseDecision.model_validate_json(content)

    async def _verification_evidence(
        self, tasks: tuple[TaskRow, ...]
    ) -> tuple[tuple[str, ...], tuple[UUID, ...]]:
        return await verification_evidence(self._database, self._artifacts, tasks)

    async def _request_repair(
        self,
        run_id: UUID,
        *,
        latest: tuple[TaskRow, ...],
        failures: tuple[str, ...],
        evidence_ids: tuple[UUID, ...],
    ) -> None:
        async with self._database.sessions() as session:
            plan_row = await session.scalar(
                select(PlanRevisionRow)
                .where(PlanRevisionRow.run_id == run_id)
                .order_by(PlanRevisionRow.revision.desc())
                .limit(1)
            )
            run = await session.get(RunRow, run_id)
            repair_count = int(
                await session.scalar(
                    select(func.count()).select_from(RepairMutationRow).where(
                        RepairMutationRow.run_id == run_id
                    )
                )
                or 0
            )
        if plan_row is None or run is None:
            raise RuntimeError("repair run or plan is missing")
        current = TaskPlan.model_validate(plan_row.plan)
        stage = f"debugger:{current.revision + 1}"
        stage_attempt_id = uuid5(NAMESPACE_URL, f"run-stage:{run_id}:{stage}")
        spec = AgentSpec(
            role="debugger",
            purpose="Propose one bounded DAG mutation that repairs verified integration failures.",
            input_schema="AgentInvocation@1.0",
            output_schema="TaskPlanMutation@1.0",
            primary_model=self._primary_model,
            fallback_models=self._fallback_models,
            tool_grants=(),
            maximum_risk=RiskLevel.MEDIUM,
            memory_policy="verified-external-uams-context",
            token_budget=20_000,
            cost_budget_usd=2,
            turn_budget=6,
            wall_time_seconds=600,
            sandbox_profile="none",
            network_profile="none",
            retry_policy="transient-fallback-and-one-schema-repair",
            escalation_policy="terminate-on-bounds-or-no-progress",
            termination_policy="one-valid-mutation-or-visible-failure",
        )
        async with self._database.transaction() as session:
            await session.merge(
                RunStageAttemptRow(
                    id=stage_attempt_id,
                    run_id=run_id,
                    stage=stage,
                    agent_spec_hash=spec.spec_hash,
                    status="RUNNING",
                )
            )
        runtime = AgentRuntime(
            spec,
            self._gateway,
            input_type=AgentInvocation,
            output_type=TaskPlanMutation,
            memory=self._memory,
            usage_recorder=RunStageUsageRecorder(
                self._database,
                stage_attempt_id=stage_attempt_id,
            ),
        )
        result = await runtime.run(
            AgentInvocation(
                trace_id=f"run:{run_id}:stage:{stage}",
                run_id=run_id,
                task_id=run_id,
                attempt_id=stage_attempt_id,
                project_id=run.project_id,
                repository_id=run.repository_id,
                baseline_commit=run.baseline_commit,
                goal=run.goal,
                input_payload={
                    "current_plan": current.model_dump(mode="json"),
                    "verified_failures": list(failures),
                    "verified_artifact_ids": [str(value) for value in evidence_ids],
                    "next_revision": current.revision + 1,
                    "remaining_dynamic_tasks": max(
                        0, self._limits.max_dynamic_tasks - repair_count
                    ),
                },
            )
        )
        failure_signature = canonical_sha256({"failures": failures})
        progress = canonical_sha256(
            {
                "revision": current.revision,
                "evidence_ids": [str(value) for value in evidence_ids],
            }
        )
        decision = await self._repair.apply(
            run_id=run_id,
            outcome=VerificationOutcome(
                passed=False,
                failure_signature=failure_signature,
                progress_fingerprint=progress,
                artifact_ids=evidence_ids,
            ),
            mutation=result.output,
        )
        async with self._database.transaction() as session:
            attempt = await session.get(RunStageAttemptRow, stage_attempt_id)
            if attempt is not None:
                attempt.status = (
                    "COMPLETED"
                    if decision.action is RepairAction.APPLY_MUTATION
                    else "FAILED"
                )
                attempt.ended_at = utc_now()
        if decision.action is not RepairAction.APPLY_MUTATION:
            await self._transition_run(run_id, RunStatus.FAILED)
            raise RuntimeError(f"bounded repair terminated: {decision.reason_codes}")
        if decision.plan is None:
            raise RuntimeError("accepted repair is missing its durable plan revision")
        self._require_integration_plan(decision.plan)

    async def _request_commit_approval(
        self, run_id: UUID, *, latest: tuple[TaskRow, ...]
    ) -> None:
        sink = self._integration_sink(latest)
        async with self._database.sessions() as session:
            run = await session.get(RunRow, run_id)
            attempt = await session.scalar(
                select(TaskAttemptRow)
                .where(TaskAttemptRow.task_id == sink.id)
                .order_by(TaskAttemptRow.started_at.desc())
                .limit(1)
            )
        if run is None or attempt is None:
            raise RuntimeError("integration sink or attempt is missing")
        worktree = self._worktrees.managed_root / f"task-{sink.id}"
        call = self._commit_call(run, sink, attempt, worktree)
        await self._approvals.request(
            call,
            context=self._approval_context(run, sink, attempt, worktree),
        )
        await self._transition_run(run_id, RunStatus.WAITING_FOR_APPROVAL)

    async def _advance_approval(self, run_id: UUID) -> str | None:
        run, sink, attempt, worktree, call = await self._approval_identity(run_id)
        async with self._database.sessions() as session:
            approval = await session.scalar(
                select(ApprovalRow).where(ApprovalRow.call_id == call.call_id)
            )
        if approval is None or approval.status is ApprovalStatus.PENDING:
            return None
        if approval.status is not ApprovalStatus.APPROVED:
            await self._transition_run(run_id, RunStatus.FAILED)
            return "FAILED"
        await self._approvals.authorize(
            approval.id,
            call,
            context=self._approval_context(run, sink, attempt, worktree),
        )
        commit = await asyncio.to_thread(
            self._worktrees.commit_task_worktree,
            worktree,
            message=f"AutoSWE run {run.id}: {run.goal}",
        )
        # Finalize the consequential call through the gateway so the durable
        # audit record and outbox event are produced by the same authority
        # that governs every other tool side effect.
        await self._tool_gateway.complete_approved(
            call,
            output={"commit": commit},
            risk=RiskLevel.HIGH,
        )
        outcome = await self._promote_run_memory(run, sink, attempt, commit)
        if outcome is PromotionOutcome.PROMOTED:
            await self._transition_run(run_id, RunStatus.COMPLETED)
            await self._cleanup_run(run)
            return "COMPLETED"
        await self._transition_run(run_id, RunStatus.WAITING_FOR_MEMORY)
        return "WAITING_FOR_MEMORY"

    async def _advance_memory(self, run_id: UUID) -> str | None:
        async with self._database.sessions() as session:
            run = await session.get(RunRow, run_id)
            row = await session.scalar(
                select(MemoryCandidateRow)
                .where(MemoryCandidateRow.run_id == run_id)
                .order_by(MemoryCandidateRow.created_at.desc())
                .limit(1)
            )
        if run is None or row is None:
            raise RuntimeError("memory-waiting run has no candidate")
        candidate = MemoryCandidate.model_validate(row.candidate)
        _, sink, attempt = await self._final_stage_identity(run.id)
        outcome = await self._promotion.promote(
            candidate,
            await self._promotion_review(
                run_id=run.id,
                sink_id=sink.id,
                attempt_status=attempt.status,
            ),
        )
        if outcome.outcome is not PromotionOutcome.PROMOTED:
            return None
        await self._transition_run(run_id, RunStatus.COMPLETED)
        await self._cleanup_run(run)
        return "COMPLETED"

    async def _promote_run_memory(
        self,
        run: RunRow,
        sink: TaskRow,
        attempt: TaskAttemptRow,
        commit: str,
    ) -> PromotionOutcome:
        async with self._database.transaction() as session:
            artifacts = tuple(
                (
                    await session.scalars(
                        select(ArtifactRow).where(
                            ArtifactRow.run_id == run.id,
                            ArtifactRow.state == ArtifactState.VALID,
                        )
                    )
                ).all()
            )
            messages = tuple(
                (
                    await session.scalars(
                        select(AgentMessageRow).where(AgentMessageRow.run_id == run.id)
                    )
                ).all()
            )
            candidate = build_memory_candidate(
                run=run,
                sink=sink,
                attempt=attempt,
                commit=commit,
                artifacts=artifacts,
                messages=messages,
            )
            existing = await session.get(MemoryCandidateRow, candidate.candidate_id)
            if existing is None:
                # Content-identical knowledge from a prior run already
                # promoted? Skip re-promotion entirely; UAMS identity is
                # content-derived, so this is the same memory.
                promoted_twin = await session.scalar(
                    select(MemoryCandidateRow).where(
                        MemoryCandidateRow.deterministic_memory_id
                        == deterministic_memory_id(candidate),
                        MemoryCandidateRow.status == "PROMOTED",
                    )
                )
                if promoted_twin is not None:
                    return PromotionOutcome.PROMOTED
                await self._repository.create_memory_candidate(session, candidate)
        review = await self._promotion_review(
            run_id=run.id,
            sink_id=sink.id,
            attempt_status=attempt.status,
        )
        result = await self._promotion.promote(candidate, review)
        return result.outcome

    async def _promotion_review(
        self,
        *,
        run_id: UUID,
        sink_id: UUID,
        attempt_status: str,
    ) -> PromotionReview:
        return await promotion_review(
            self._database,
            run_id=run_id,
            sink_id=sink_id,
            attempt_status=attempt_status,
        )

    async def _final_stage_identity(self, run_id: UUID) -> tuple[RunRow, TaskRow, TaskAttemptRow]:
        async with self._database.sessions() as session:
            run = await session.get(RunRow, run_id)
            if run is None:
                raise LookupError(f"run {run_id} does not exist")
            revision = await session.scalar(
                select(func.max(TaskRow.plan_revision)).where(TaskRow.run_id == run_id)
            )
            tasks = tuple(
                (
                    await session.scalars(
                        select(TaskRow).where(
                            TaskRow.run_id == run_id,
                            TaskRow.plan_revision == revision,
                        )
                    )
                ).all()
            )
            sink = self._integration_sink(tasks)
            attempt = await session.scalar(
                select(TaskAttemptRow)
                .where(TaskAttemptRow.task_id == sink.id)
                .order_by(TaskAttemptRow.started_at.desc())
                .limit(1)
            )
        if attempt is None:
            raise RuntimeError("integration attempt is missing")
        return run, sink, attempt

    async def _approval_identity(
        self, run_id: UUID
    ) -> tuple[RunRow, TaskRow, TaskAttemptRow, Path, ToolCallRequest]:
        async with self._database.sessions() as session:
            run = await session.get(RunRow, run_id)
            if run is None:
                raise LookupError(f"run {run_id} does not exist")
            revision = await session.scalar(
                select(func.max(TaskRow.plan_revision)).where(TaskRow.run_id == run_id)
            )
            tasks = tuple(
                (
                    await session.scalars(
                        select(TaskRow).where(
                            TaskRow.run_id == run_id,
                            TaskRow.plan_revision == revision,
                        )
                    )
                ).all()
            )
            sink = self._integration_sink(tasks)
            attempt = await session.scalar(
                select(TaskAttemptRow)
                .where(TaskAttemptRow.task_id == sink.id)
                .order_by(TaskAttemptRow.started_at.desc())
                .limit(1)
            )
        if attempt is None:
            raise RuntimeError("integration attempt is missing")
        worktree = self._worktrees.managed_root / f"task-{sink.id}"
        return run, sink, attempt, worktree, self._commit_call(run, sink, attempt, worktree)

    @staticmethod
    def _integration_sink(tasks: tuple[TaskRow, ...]) -> TaskRow:
        return integration_sink(tasks)

    async def _current_plan(self, run_id: UUID) -> TaskPlan:
        async with self._database.sessions() as session:
            row = await session.scalar(
                select(PlanRevisionRow)
                .where(PlanRevisionRow.run_id == run_id)
                .order_by(PlanRevisionRow.revision.desc())
                .limit(1)
            )
        if row is None:
            raise LookupError(f"run {run_id} has no durable plan")
        return TaskPlan.model_validate(row.plan)

    @staticmethod
    def _require_integration_plan(plan: TaskPlan) -> None:
        require_integration_plan(plan)

    @staticmethod
    def _commit_call(
        run: RunRow, sink: TaskRow, attempt: TaskAttemptRow, worktree: Path
    ) -> ToolCallRequest:
        call_id = uuid5(NAMESPACE_URL, f"final-git-commit:{run.id}:{sink.id}")
        return ToolCallRequest(
            call_id=call_id,
            run_id=run.id,
            task_id=sink.id,
            attempt_id=attempt.id,
            requested_by="final-reviewer",
            tool_name="git_commit",
            arguments={
                "worktree": worktree.name,
                "message": f"AutoSWE run {run.id}: {' '.join(run.goal.split())[:200]}",
                "baseline_commit": run.baseline_commit,
            },
            idempotency_key=f"final-git-commit:{run.id}:{sink.id}",
        )

    @staticmethod
    def _approval_context(
        run: RunRow, sink: TaskRow, attempt: TaskAttemptRow, worktree: Path
    ) -> ToolExecutionContext:
        return ToolExecutionContext(
            project_id=run.project_id,
            repository_id=run.repository_id,
            run_id=run.id,
            task_id=sink.id,
            attempt_id=attempt.id,
            baseline_commit=run.baseline_commit,
            agent_role="final-reviewer",
            agent_capabilities=frozenset({"repository-write"}),
            risk_ceiling=RiskLevel.HIGH,
            worktree_root=worktree,
        )

    async def _transition_run(self, run_id: UUID, target: RunStatus) -> None:
        now = datetime.now(UTC)
        async with self._database.transaction() as session:
            run = await session.scalar(
                select(RunRow).where(RunRow.id == run_id).with_for_update()
            )
            if run is None:
                raise LookupError(f"run {run_id} does not exist")
            current = RunStatus(run.state)
            if current is target:
                return
            require_run_transition(current, target)
            await self._repository.record_state_duration(
                session,
                aggregate_type="workflow",
                aggregate_id=run.id,
                state=run.state,
                entered_at=run.state_entered_at,
                exited_at=now,
            )
            run.state = target.value
            run.state_entered_at = now

    async def _cleanup_run(self, run: RunRow) -> None:
        async with self._database.sessions() as session:
            task_ids = tuple(
                (await session.scalars(select(TaskRow.id).where(TaskRow.run_id == run.id))).all()
            )
            repository = await session.get(RepositoryRow, run.repository_id)
        if repository is None:
            return
        for task_id in task_ids:
            worktree = self._worktrees.managed_root / f"task-{task_id}"
            if worktree.exists():
                await asyncio.to_thread(
                    self._worktrees.cleanup,
                    Path(repository.source_path),
                    worktree,
                    terminal=True,
                )
