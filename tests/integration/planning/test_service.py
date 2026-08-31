from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select

from agents.gateway import ModelResponse, ModelUsage
from agents.scripted import ScriptedGateway, ScriptedResponse
from domain.enums import RiskLevel, TaskStatus, TaskType
from domain.models import BudgetPolicy, PlanLimits, TaskPlan, TaskSpec
from knowledge.memory.fake import FakeMemoryPort
from persistence.repositories import DomainRepository
from persistence.tables import ModelCallRow, PlanRevisionRow, RunRow, RunStageAttemptRow, TaskRow
from planning.service import RunPlanningService


@pytest.mark.asyncio
async def test_planner_persists_dynamic_parallel_dag_and_only_roots_are_ready(database) -> None:
    repository = DomainRepository()
    project_id, repository_id, run_id = uuid4(), uuid4(), uuid4()
    research_id, implementation_id, test_id, integration_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    limits = PlanLimits(
        max_dynamic_tasks=4,
        max_plan_depth=4,
        max_total_budget_usd=10,
        max_total_execution_seconds=1_000,
    )

    def task(
        task_id,
        task_type: TaskType,
        dependencies=(),  # type: ignore[no-untyped-def]
    ) -> TaskSpec:
        tools = {
            TaskType.RESEARCH: ("read_file", "search_code"),
            TaskType.IMPLEMENTATION: ("read_file", "search_code", "apply_patch", "run_tests"),
            TaskType.TEST: ("read_file", "search_code", "apply_patch", "run_tests"),
            TaskType.VALIDATION: ("read_file", "search_code", "run_tests"),
        }[task_type]
        return TaskSpec(
            id=task_id,
            plan_revision=1,
            project_id=project_id,
            repository_id=repository_id,
            title=task_type.value,
            description=f"Execute {task_type.value}",
            task_type=task_type,
            dependencies=dependencies,
            assigned_capability=task_type.value.casefold(),
            acceptance_criteria=("Verified evidence exists",),
            allowed_tools=tools,
            risk_ceiling=RiskLevel.MEDIUM,
            budget=BudgetPolicy(cost_usd=1, wall_time_seconds=60),
        )

    plan = TaskPlan(
        run_id=run_id,
        project_id=project_id,
        repository_id=repository_id,
        baseline_commit="a" * 40,
        revision=1,
        tasks=(
            task(research_id, TaskType.RESEARCH),
            task(implementation_id, TaskType.IMPLEMENTATION),
            task(test_id, TaskType.TEST),
            task(
                integration_id,
                TaskType.VALIDATION,
                (research_id, implementation_id, test_id),
            ),
        ),
        limits=limits,
    )
    gateway = ScriptedGateway(
        responses=(
            ScriptedResponse(
                ModelResponse(
                    trace_id="replaced",
                    provider_request_id="scripted-plan",
                    model="scripted-model",
                    structured_output=plan.model_dump(mode="json"),
                    finish_reason="stop",
                    usage=ModelUsage(input_tokens=20, output_tokens=30, cost_usd=0.01),
                )
            ),
        )
    )
    async with database.transaction() as session:
        await repository.create_project(session, project_id=project_id, name="planned")
        await repository.create_repository(
            session,
            repository_id=repository_id,
            project_id=project_id,
            source_path="/imports/project.git",
            default_branch="main",
        )
        await repository.create_run(
            session,
            run_id=run_id,
            project_id=project_id,
            repository_id=repository_id,
            goal="Build a production SaaS feature",
            baseline_commit="a" * 40,
        )

    service = RunPlanningService(
        database=database,
        gateway=gateway,
        memory=FakeMemoryPort(),
        primary_model="scripted-model",
        fallback_models=(),
        limits=limits,
        repository_context=lambda _: {"adapter": "python", "source_files": []},
    )

    persisted = await service.plan_next()

    assert persisted == plan
    async with database.sessions() as session:
        run = await session.get(RunRow, run_id)
        rows = tuple(
            (
                await session.scalars(
                    select(TaskRow).where(TaskRow.run_id == run_id).order_by(TaskRow.id)
                )
            ).all()
        )
        revision = await session.scalar(
            select(PlanRevisionRow).where(PlanRevisionRow.run_id == run_id)
        )
        stage = await session.scalar(
            select(RunStageAttemptRow).where(RunStageAttemptRow.run_id == run_id)
        )
        calls = tuple(
            (await session.scalars(select(ModelCallRow).where(ModelCallRow.run_id == run_id))).all()
        )
    assert run is not None and run.state == "EXECUTING"
    assert revision is not None and TaskPlan.model_validate(revision.plan) == plan
    assert {row.id for row in rows if row.state is TaskStatus.READY} == {
        research_id,
        implementation_id,
        test_id,
    }
    assert {row.id for row in rows if row.state is TaskStatus.PENDING} == {integration_id}
    assert stage is not None and stage.status == "COMPLETED"
    assert len(calls) == 1 and calls[0].run_stage_attempt_id == stage.id


async def test_missing_repository_does_not_leave_run_stuck_planning(database):
    repository = DomainRepository()
    project_id, repository_id, run_id = uuid4(), uuid4(), uuid4()
    async with database.transaction() as session:
        await repository.create_project(session, project_id=project_id, name="missing repo")
        await repository.create_repository(
            session,
            repository_id=repository_id,
            project_id=project_id,
            source_path="/missing-autoswe-test-repository",
            default_branch="main",
        )
        await repository.create_run(
            session,
            run_id=run_id,
            project_id=project_id,
            repository_id=repository_id,
            goal="Fix repository",
            baseline_commit="a" * 40,
        )
    service = RunPlanningService(
        database=database,
        gateway=ScriptedGateway(responses=()),
        memory=FakeMemoryPort(),
        primary_model="scripted-model",
        fallback_models=(),
        limits=PlanLimits(
            max_dynamic_tasks=4,
            max_plan_depth=4,
            max_total_budget_usd=10,
            max_total_execution_seconds=1000,
        ),
    )
    with pytest.raises(FileNotFoundError):
        await service.plan_next()
    async with database.sessions() as session:
        assert (await session.get(RunRow, run_id)).state == "FAILED"
        stage = await session.scalar(
            select(RunStageAttemptRow).where(RunStageAttemptRow.run_id == run_id)
        )
        assert stage.status == "FAILED"
        assert stage.ended_at is not None
