from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from agents.gateway import (
    FailureClass,
    GatewayError,
    ModelResponse,
    ModelUsage,
    OpenAICompatibleGateway,
    ProviderCapabilities,
)
from agents.scripted import ScriptedGateway, ScriptedResponse
from domain.enums import RiskLevel, RunStatus, TaskStatus, TaskType
from domain.models import BudgetPolicy, PlanLimits, TaskPlan, TaskSpec
from domain.task_policy import TASK_REQUIRED_TOOLS
from knowledge.memory.fake import FakeMemoryPort
from persistence.repositories import DomainRepository
from persistence.tables import (
    AuditEventRow,
    ModelCallRow,
    OutboxRow,
    PlanRevisionRow,
    RunRow,
    RunStageAttemptRow,
    TaskRow,
)
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


@pytest.fixture
async def planning_case(database):
    repository = DomainRepository()
    project_id, repository_id, run_id = uuid4(), uuid4(), uuid4()
    limits = PlanLimits(
        max_dynamic_tasks=8,
        max_plan_depth=8,
        max_total_budget_usd=10,
        max_total_execution_seconds=1_000,
    )
    async with database.transaction() as session:
        await repository.create_project(session, project_id=project_id, name="planner repair")
        await repository.create_repository(
            session,
            repository_id=repository_id,
            project_id=project_id,
            source_path="/imports/planner-repair.git",
            default_branch="main",
        )
        await repository.create_run(
            session,
            run_id=run_id,
            project_id=project_id,
            repository_id=repository_id,
            goal="Implement subtraction, update README and verify all tests",
            baseline_commit="a" * 40,
        )
    documentation = TaskSpec(
        id=uuid4(),
        plan_revision=1,
        project_id=project_id,
        repository_id=repository_id,
        title="Update documentation",
        description="Document subtraction with a verified README example",
        task_type=TaskType.DOCUMENTATION,
        assigned_capability="documentation",
        acceptance_criteria=("README documents subtraction with an example",),
        allowed_tools=("read_file", "apply_patch", "run_tests"),
        risk_ceiling=RiskLevel.MEDIUM,
        budget=BudgetPolicy(cost_usd=1, wall_time_seconds=60),
    )
    validation = documentation.model_copy(
        update={
            "id": uuid4(),
            "task_type": TaskType.VALIDATION,
            "assigned_capability": "validation",
            "title": "Verify subtraction",
            "description": "Verify the integrated implementation and README",
            "dependencies": (documentation.id,),
            "allowed_tools": ("read_file", "run_tests"),
        }
    )
    plan = TaskPlan(
        run_id=run_id,
        project_id=project_id,
        repository_id=repository_id,
        baseline_commit="a" * 40,
        revision=1,
        tasks=(documentation, validation),
        limits=limits,
    )

    def service_for(*candidates, gateway=None, **kwargs):
        gateway = gateway or ScriptedGateway(
            responses=tuple(
                ScriptedResponse(
                    ModelResponse(
                        trace_id="scripted",
                        model="scripted-model",
                        structured_output=candidate.model_dump(mode="json"),
                        usage=ModelUsage(input_tokens=20, output_tokens=30),
                        finish_reason="stop",
                    )
                )
                for candidate in candidates
            )
        )
        return RunPlanningService(
            database=database,
            gateway=gateway,
            memory=FakeMemoryPort(),
            primary_model="scripted-model",
            fallback_models=(),
            limits=limits,
            repository_context=lambda _: {"adapter": "python", "source_files": []},
            **kwargs,
        ), gateway

    return plan, service_for


async def test_planner_uses_run_model_snapshot_and_records_effective_spec(database, planning_case):
    import httpx

    from agents.configuration import ModelRuntimeFactory
    from persistence.model_settings import ModelConfiguration
    from tests.integration.api.test_control_plane import settings

    plan, service_for = planning_case
    snapshot = ModelConfiguration(
        base_url="http://selected.test/v1",
        primary_model="selected-model",
        timeout_seconds=81,
        temperature=0.2,
    )
    async with database.transaction() as session:
        run = await session.get(RunRow, plan.run_id)
        run.model_configuration = snapshot.private_storage()
    seen = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"capabilities": ["structured_outputs"]})
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "selected-model",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": plan.model_dump_json(),
                        },
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        factory = ModelRuntimeFactory(settings(), client=client)
        planner, ignored_gateway = service_for(plan, model_factory=factory)
        assert await planner.plan_next() == plan
        await factory.close()
    assert seen[0]["model"] == "selected-model"
    assert seen[0]["temperature"] == 0.2
    assert not ignored_gateway.requests
    async with database.sessions() as session:
        stage = await session.scalar(
            select(RunStageAttemptRow).where(
                RunStageAttemptRow.run_id == plan.run_id,
            )
        )
        call = await session.scalar(select(ModelCallRow).where(ModelCallRow.run_id == plan.run_id))
        assert call.model == "selected-model"
        assert stage.agent_spec_hash == call.agent_spec_hash


@pytest.mark.parametrize(
    ("task_type", "omitted"),
    [
        (TaskType.DOCUMENTATION, "run_tests"),
        (TaskType.VALIDATION, "read_file"),
        (TaskType.RESEARCH, "search_code"),
    ],
)
async def test_planner_assigns_omitted_required_tools_from_static_policy(
    database, planning_case, task_type, omitted
):
    plan, service_for = planning_case
    index = 1 if task_type is TaskType.VALIDATION else 0
    changed = plan.tasks[index].model_copy(
        update={
            "task_type": task_type,
            "assigned_capability": task_type.value.lower(),
            "allowed_tools": tuple(sorted(TASK_REQUIRED_TOOLS[task_type] - {omitted})),
        }
    )
    candidate = plan.model_copy(
        update={
            "tasks": tuple(changed if i == index else task for i, task in enumerate(plan.tasks))
        }
    )
    service, gateway = service_for(candidate, candidate, candidate)

    persisted = await service.plan_next()

    assert persisted is not None
    assert set(persisted.tasks[index].allowed_tools) == TASK_REQUIRED_TOOLS[task_type]
    assert persisted.tasks[index].risk_ceiling == changed.risk_ceiling
    assert persisted.tasks[index].assigned_capability == changed.assigned_capability
    assert len(gateway.requests) == 1
    async with database.sessions() as session:
        row = await session.get(TaskRow, changed.id)
        assert set(row.allowed_tools) == TASK_REQUIRED_TOOLS[task_type]


async def test_planner_repair_reuses_candidate_with_task_specific_feedback(planning_case):
    plan, service_for = planning_case
    invalid = plan.model_copy(
        update={
            "tasks": (plan.tasks[0].model_copy(update={"acceptance_criteria": ()}), plan.tasks[1])
        }
    )
    service, gateway = service_for(invalid, plan)

    assert await service.plan_next() == plan

    repair = json.loads(gateway.requests[1].messages[1].content.split("Input:\n", 1)[1])
    assert repair["previous_candidate"] == invalid.model_dump(mode="json")
    assert repair["validation_issues"] == [
        {
            "code": "MISSING_ACCEPTANCE_CRITERIA",
            "message": "task must define at least one testable acceptance criterion",
            "task_id": str(plan.tasks[0].id),
        }
    ]
    assert "preserve" in repair["repair_hint"].lower()
    assert "task IDs" in repair["repair_hint"]


@pytest.mark.parametrize(
    ("changes", "max_risk", "code"),
    [
        ({"allowed_tools": ("read_file", "host_shell")}, RiskLevel.HIGH, "UNSUPPORTED_TOOL"),
        ({"assigned_capability": "arbitrary-admin"}, RiskLevel.HIGH, "INVALID_TASK_CAPABILITY"),
        ({"risk_ceiling": RiskLevel.LOW}, RiskLevel.HIGH, "INSUFFICIENT_TASK_RISK"),
        ({"risk_ceiling": RiskLevel.HIGH}, RiskLevel.MEDIUM, "RISK_CEILING_EXCEEDS_POLICY"),
    ],
)
async def test_policy_assignment_preserves_validator_rejection(
    database, planning_case, changes, max_risk, code
):
    plan, service_for = planning_case
    invalid = plan.model_copy(
        update={"tasks": (plan.tasks[0].model_copy(update=changes), plan.tasks[1])}
    )
    service, gateway = service_for(invalid, invalid, invalid, max_risk_ceiling=max_risk)

    with pytest.raises(ValueError, match=code):
        await service.plan_next()

    assert len(gateway.requests) == 3
    async with database.sessions() as session:
        assert (await session.get(RunRow, plan.run_id)).state == "FAILED"
        assert await session.scalar(select(TaskRow).where(TaskRow.run_id == plan.run_id)) is None


async def test_terminal_planning_failure_is_actionable_and_transactional(database, planning_case):
    plan, service_for = planning_case
    invalid = plan.model_copy(
        update={"tasks": (plan.tasks[0], plan.tasks[1].model_copy(update={"dependencies": ()}))}
    )
    service, gateway = service_for(invalid, invalid, invalid)

    with pytest.raises(ValueError, match="FINAL_VALIDATION_SINK"):
        await service.plan_next()

    async with database.sessions() as session:
        run = await session.get(RunRow, plan.run_id)
        stage = await session.scalar(
            select(RunStageAttemptRow).where(RunStageAttemptRow.run_id == plan.run_id)
        )
        event = await session.scalar(
            select(AuditEventRow).where(AuditEventRow.event_type == "run.planning_failed")
        )
        assert run.state == "FAILED"
        assert stage.status == "FAILED"
        assert event is not None
        outbox = await session.get(OutboxRow, event.id)
        assert outbox is not None and outbox.topic == "run-state"
        assert outbox.payload == event.payload
        assert event.aggregate_id == plan.run_id
        assert event.causation_id == stage.id
        assert event.payload["attempts"] == 3
        assert event.payload["error_code"] == "INVALID_TASK_PLAN"
        assert event.payload["validation_issues"][0]["code"] == "FINAL_VALIDATION_SINK"
        assert "VALIDATION" in event.payload["validation_issues"][0]["message"]
        assert await session.scalar(select(TaskRow).where(TaskRow.run_id == plan.run_id)) is None
    assert len(gateway.requests) == 3


async def test_provider_failures_do_not_expose_raw_errors_in_events_or_repair(
    planning_case, database
):
    plan, service_for = planning_case
    secret = "Bearer test-secret-planner-credential"  # noqa: S105 - redaction fixture
    gateway = ScriptedGateway(
        responses=tuple(
            ScriptedResponse(
                error=GatewayError(
                    f"Provider rejected Authorization: {secret}",
                    failure_class=FailureClass.PERMANENT,
                )
            )
            for _ in range(3)
        )
    )
    service, _ = service_for(gateway=gateway)

    with pytest.raises(GatewayError):
        await service.plan_next()

    assert all(secret not in message.content for r in gateway.requests for message in r.messages)
    async with database.sessions() as session:
        event = await session.scalar(
            select(AuditEventRow).where(AuditEventRow.aggregate_id == plan.run_id)
        )
        assert event is not None
        assert event.event_type == "run.planning_failed"
        assert event.payload["error_code"] == "MODEL_PROVIDER_ERROR"
        assert "provider" in event.payload["message"].lower()
        assert secret not in json.dumps(event.payload)


@pytest.mark.parametrize(
    "state", ["cancellation_requested", "CANCELLED", "EXECUTING", "stale", "missing"]
)
async def test_planning_failure_cannot_overwrite_lost_ownership(database, planning_case, state):
    plan, service_for = planning_case
    service, _ = service_for()
    run, _, attempt_id, fence = await service._claim_run()
    expected_run, expected_stage = RunStatus.PLANNING.value, "RUNNING"
    async with database.transaction() as session:
        run_row = await session.get(RunRow, run.id)
        stage = await session.get(RunStageAttemptRow, attempt_id)
        if state == "stale":
            stage.started_at = fence + timedelta(seconds=1)
        elif state == "missing":
            await session.delete(stage)
            expected_stage = None
        elif state == "cancellation_requested":
            run_row.cancellation_requested_at = datetime.now(UTC)
        else:
            expected_run = run_row.state = state
            expected_stage = stage.status = "CANCELLED" if state == "CANCELLED" else "COMPLETED"

    await service._fail_run(run.id, stage_attempt_id=attempt_id, fence_started_at=fence)

    async with database.sessions() as session:
        assert (await session.get(RunRow, run.id)).state == expected_run
        stage = await session.get(RunStageAttemptRow, attempt_id)
        assert (stage.status if stage else None) == expected_stage
        assert await session.scalar(select(AuditEventRow)) is None
        assert await session.scalar(select(OutboxRow)) is None


@pytest.mark.parametrize("lost_owner", ["cancellation_requested", "stale"])
async def test_plan_persistence_rejects_cancelled_or_superseded_owner(
    database, planning_case, lost_owner
):
    plan, service_for = planning_case
    service, _ = service_for()
    run, _, attempt_id, fence = await service._claim_run()
    async with database.transaction() as session:
        if lost_owner == "stale":
            stage = await session.get(RunStageAttemptRow, attempt_id)
            stage.started_at = fence + timedelta(seconds=1)
        else:
            row = await session.get(RunRow, run.id)
            row.cancellation_requested_at = datetime.now(UTC)

    with pytest.raises(RuntimeError):
        await service._persist_plan(plan, stage_attempt_id=attempt_id, fence_started_at=fence)

    async with database.sessions() as session:
        assert (await session.get(RunRow, run.id)).state == "PLANNING"
        assert await session.scalar(select(TaskRow)) is None
        assert await session.scalar(select(PlanRevisionRow)) is None


async def test_failed_event_outbox_error_rolls_back_terminal_state(database, planning_case):
    class BrokenOutboxRepository(DomainRepository):
        async def enqueue_event(self, session, **kwargs):
            raise RuntimeError("outbox unavailable")

    plan, service_for = planning_case
    service, _ = service_for(repository=BrokenOutboxRepository())
    run, _, attempt_id, fence = await service._claim_run()

    with pytest.raises(RuntimeError, match="outbox unavailable"):
        await service._fail_run(run.id, stage_attempt_id=attempt_id, fence_started_at=fence)

    async with database.sessions() as session:
        assert (await session.get(RunRow, run.id)).state == "PLANNING"
        assert (await session.get(RunStageAttemptRow, attempt_id)).status == "RUNNING"
        assert await session.scalar(select(AuditEventRow)) is None
        assert await session.scalar(select(OutboxRow)) is None


@pytest.mark.parametrize("lost_owner", ["cancelled", "stale"])
async def test_planner_stops_repair_after_ownership_changes(database, planning_case, lost_owner):
    plan, service_for = planning_case
    invalid = plan.model_copy(
        update={
            "tasks": (plan.tasks[0].model_copy(update={"acceptance_criteria": ()}), plan.tasks[1])
        }
    )

    class OwnershipChangingGateway(ScriptedGateway):
        async def complete(self, request, *, cancel=None):
            async with database.transaction() as session:
                run = await session.get(RunRow, plan.run_id)
                stage = await session.scalar(
                    select(RunStageAttemptRow).where(RunStageAttemptRow.run_id == plan.run_id)
                )
                if lost_owner == "cancelled":
                    run.state = "CANCELLED"
                    run.cancellation_requested_at = datetime.now(UTC)
                    stage.status = "CANCELLED"
                else:
                    stage.started_at += timedelta(seconds=1)
            return await super().complete(request, cancel=cancel)

    gateway = OwnershipChangingGateway(
        responses=tuple(
            ScriptedResponse(
                ModelResponse(
                    trace_id="scripted",
                    model="scripted-model",
                    structured_output=invalid.model_dump(mode="json"),
                    usage=ModelUsage(),
                    finish_reason="stop",
                )
            )
            for _ in range(3)
        )
    )
    service, _ = service_for(gateway=gateway)

    assert await service.plan_next() is None
    assert len(gateway.requests) == 1
    async with database.sessions() as session:
        assert await session.scalar(select(TaskRow)) is None
        assert await session.scalar(select(AuditEventRow)) is None


async def test_validation_failure_event_omits_untrusted_tool_and_path_values(
    database, planning_case
):
    plan, service_for = planning_case
    secret = "test-private-credential"  # noqa: S105 - redaction fixture
    invalid = plan.model_copy(
        update={
            "tasks": (
                plan.tasks[0].model_copy(
                    update={"allowed_tools": (secret,), "repository_paths": (f"../{secret}",)}
                ),
                plan.tasks[1],
            )
        }
    )
    service, _ = service_for(invalid, invalid, invalid)

    with pytest.raises(ValueError):
        await service.plan_next()

    async with database.sessions() as session:
        event = await session.scalar(select(AuditEventRow))
        assert event is not None
        assert secret not in json.dumps(event.payload)
        assert {issue["code"] for issue in event.payload["validation_issues"]} == {
            "UNSUPPORTED_TOOL",
            "REPOSITORY_PATH_ESCAPE",
        }


def active_planning_coroutines():
    return [
        task
        for task in asyncio.all_tasks()
        if not task.done()
        and any(
            owner in task.get_coro().__qualname__
            for owner in ("RunPlanningService.", "AgentRuntime.run", "OpenAICompatibleGateway.")
        )
    ]


@pytest.mark.parametrize("phase", ["request", "retry_backoff"])
@pytest.mark.parametrize("ownership_change", ["cancelled", "cancellation_requested", "stale"])
async def test_active_planning_interrupts_model_and_internal_retries_for_lost_owner(
    database, planning_case, phase, ownership_change
):
    plan, service_for = planning_case
    next_run_id = uuid4()
    next_plan = plan.model_copy(update={"run_id": next_run_id})
    async with database.transaction() as session:
        await DomainRepository().create_run(
            session,
            run_id=next_run_id,
            project_id=plan.project_id,
            repository_id=plan.repository_id,
            goal="A queued run must not wait for cancelled inference",
            baseline_commit=plan.baseline_commit,
        )
    started, request_exited = asyncio.Event(), asyncio.Event()
    release = asyncio.Event()
    requests = []

    async def handle(request):
        requests.append(request)
        if len(requests) == 1:
            started.set()
            try:
                if phase == "retry_backoff":
                    raise httpx.ReadTimeout("simulated model timeout", request=request)
                await release.wait()
            finally:
                request_exited.set()
        return httpx.Response(
            200,
            json={
                "id": "queued-plan-response",
                "model": "scripted-model",
                "choices": [
                    {
                        "message": {"content": json.dumps(next_plan.model_dump(mode="json"))},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        gateway = OpenAICompatibleGateway(
            base_url="http://model.test/v1",
            max_concurrency=1,
            client=client,
            default_capabilities=ProviderCapabilities.all_supported(),
        )
        service, _ = service_for(gateway=gateway)
        planning = asyncio.create_task(service.plan_next())
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            async with database.transaction() as session:
                run = await session.get(RunRow, plan.run_id)
                stage = await session.scalar(
                    select(RunStageAttemptRow).where(RunStageAttemptRow.run_id == plan.run_id)
                )
                if ownership_change == "stale":
                    stage.started_at += timedelta(seconds=1)
                else:
                    run.cancellation_requested_at = datetime.now(UTC)
                    if ownership_change == "cancelled":
                        run.state = "CANCELLED"
                        stage.status = "CANCELLED"
            # Less than the runtime's first retry delay, much less than inference
            # timeout: the planner must poll ownership during runtime.run itself.
            done, _ = await asyncio.wait({planning}, timeout=0.75)
            assert planning in done, "cancelled planning remained inside model request/retry"
            assert await planning is None
            assert request_exited.is_set()
            assert gateway.admission.in_flight == 0
            assert len(requests) == 1
            assert active_planning_coroutines() == []

            persisted = await asyncio.wait_for(service.plan_next(), timeout=2)
            assert persisted == next_plan
            assert len(requests) == 2
            assert gateway.admission.in_flight == 0
            assert active_planning_coroutines() == []
        finally:
            planning.cancel()
            await asyncio.gather(planning, return_exceptions=True)

    async with database.sessions() as session:
        run = await session.get(RunRow, plan.run_id)
        stage = await session.scalar(
            select(RunStageAttemptRow).where(RunStageAttemptRow.run_id == plan.run_id)
        )
        assert run.state == ("CANCELLED" if ownership_change == "cancelled" else "PLANNING")
        assert stage.status == ("CANCELLED" if ownership_change == "cancelled" else "RUNNING")
        assert (await session.get(RunRow, next_run_id)).state == "EXECUTING"
        assert await session.scalar(select(TaskRow).where(TaskRow.run_id == plan.run_id)) is None
        assert (
            await session.scalar(
                select(AuditEventRow).where(
                    AuditEventRow.aggregate_id == plan.run_id,
                    AuditEventRow.event_type == "run.planning_failed",
                )
            )
            is None
        )


async def test_cancelling_planning_caller_drains_runtime_http_and_owner_watch(
    database, planning_case
):
    _, service_for = planning_case
    started, request_exited = asyncio.Event(), asyncio.Event()
    release = asyncio.Event()

    async def handle(request):
        started.set()
        try:
            await release.wait()
        finally:
            request_exited.set()
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        gateway = OpenAICompatibleGateway(
            base_url="http://model.test/v1",
            max_concurrency=1,
            client=client,
            default_capabilities=ProviderCapabilities.all_supported(),
        )
        service, _ = service_for(gateway=gateway)
        planning = asyncio.create_task(service.plan_next())
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            planning.cancel()
            with pytest.raises(asyncio.CancelledError):
                await planning
            assert request_exited.is_set()
            assert gateway.admission.in_flight == 0
            assert active_planning_coroutines() == []
        finally:
            planning.cancel()
            await asyncio.gather(planning, return_exceptions=True)


async def test_provider_failure_drains_planning_owner_watch(planning_case):
    _, service_for = planning_case
    gateway = ScriptedGateway(
        responses=tuple(
            ScriptedResponse(
                error=GatewayError("provider unavailable", failure_class=FailureClass.PERMANENT)
            )
            for _ in range(3)
        )
    )
    service, _ = service_for(gateway=gateway)

    with pytest.raises(GatewayError):
        await service.plan_next()

    assert active_planning_coroutines() == []


async def test_planning_timeout_then_success_keeps_usage_and_replay_idempotency(
    database, planning_case, monkeypatch
):
    from agents.base import AgentAttemptRecord
    from planning.service import RunStageUsageRecorder

    monkeypatch.setattr("agents.base._RETRY_BACKOFF_BASE", 0)
    plan, service_for = planning_case
    gateway = ScriptedGateway(
        responses=(
            ScriptedResponse(
                error=GatewayError("first timeout", failure_class=FailureClass.TIMEOUT)
            ),
            ScriptedResponse(
                ModelResponse(
                    trace_id="scripted",
                    model="scripted-model",
                    provider_request_id="retry-success",
                    structured_output=plan.model_dump(mode="json"),
                    finish_reason="stop",
                    usage=ModelUsage(
                        input_tokens=40, output_tokens=20, cached_input_tokens=10, cost_usd=0.12
                    ),
                )
            ),
        )
    )
    service, _ = service_for(gateway=gateway)

    assert await service.plan_next() == plan
    async with database.sessions() as session:
        rows = (
            await session.scalars(
                select(ModelCallRow)
                .where(ModelCallRow.run_id == plan.run_id)
                .order_by(ModelCallRow.turn)
            )
        ).all()
    assert len(rows) == 2
    assert [row.turn for row in rows] == [1, 2]
    assert [row.failure_class for row in rows] == ["TIMEOUT", None]
    assert sum(row.input_tokens + row.output_tokens for row in rows) == 60
    assert sum(row.cached_input_tokens for row in rows) == 10
    assert sum(row.cost_usd for row in rows) == pytest.approx(0.12)

    for row in rows:
        recorder = RunStageUsageRecorder(database, stage_attempt_id=row.run_stage_attempt_id)
        attempt = AgentAttemptRecord(
            run_id=plan.run_id,
            task_id=plan.run_id,
            attempt_id=row.run_stage_attempt_id,
            agent_spec_hash=row.agent_spec_hash,
            turn=row.turn,
            model=row.model,
            trace_id=row.trace_id,
            provider_request_id=row.provider_request_id,
            usage=ModelUsage(
                input_tokens=row.input_tokens,
                output_tokens=row.output_tokens,
                cached_input_tokens=row.cached_input_tokens,
                cost_usd=row.cost_usd,
            ),
            failure_class=FailureClass(row.failure_class) if row.failure_class else None,
        )
        await recorder.record(attempt)
        await recorder.record(attempt)
    async with database.sessions() as session:
        replayed = (
            await session.scalars(select(ModelCallRow).where(ModelCallRow.run_id == plan.run_id))
        ).all()
    assert len(replayed) == 2
    assert sum(row.input_tokens + row.output_tokens for row in replayed) == 60
    assert sum(row.cost_usd for row in replayed) == pytest.approx(0.12)
