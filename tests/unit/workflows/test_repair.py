from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from domain.enums import TaskType
from domain.models import (
    BudgetPolicy,
    PlanLimits,
    TaskPlan,
    TaskPlanMutation,
    TaskSpec,
)
from planning.validator import TaskPlanValidator
from workflows.repair import (
    RepairAction,
    RepairHistory,
    RepairPolicy,
    VerificationOutcome,
)

PROJECT_ID, REPOSITORY_ID, RUN_ID = uuid4(), uuid4(), uuid4()


def task(
    title: str,
    *,
    task_id: UUID | None = None,
    revision: int = 1,
    dependencies: tuple[UUID, ...] = (),
    cost: float = 1,
    seconds: int = 60,
) -> TaskSpec:
    return TaskSpec(
        id=task_id or uuid4(),
        plan_revision=revision,
        project_id=PROJECT_ID,
        repository_id=REPOSITORY_ID,
        title=title,
        description=f"Complete {title}",
        task_type=TaskType.IMPLEMENTATION,
        dependencies=dependencies,
        assigned_capability="debugger" if revision > 1 else "coder",
        acceptance_criteria=("Verification passes",),
        allowed_tools=("read_file", "apply_patch", "run_tests"),
        budget=BudgetPolicy(cost_usd=cost, wall_time_seconds=seconds),
    )


def plan(
    tasks: tuple[TaskSpec, ...],
    *,
    limits: PlanLimits | None = None,
) -> TaskPlan:
    return TaskPlan(
        run_id=RUN_ID,
        project_id=PROJECT_ID,
        repository_id=REPOSITORY_ID,
        baseline_commit="a" * 40,
        revision=1,
        tasks=tasks,
        limits=limits
        or PlanLimits(
            max_dynamic_tasks=5,
            max_plan_depth=5,
            max_total_budget_usd=10,
            max_total_execution_seconds=1_000,
        ),
    )


def failed(*, signature: str = "1" * 64, progress: str = "2" * 64) -> VerificationOutcome:
    return VerificationOutcome(
        passed=False,
        failure_signature=signature,
        progress_fingerprint=progress,
        artifact_ids=(uuid4(),),
    )


@pytest.fixture
def policy() -> RepairPolicy:
    return RepairPolicy(TaskPlanValidator(allowed_tools={"read_file", "apply_patch", "run_tests"}))


def test_failure_adds_one_typed_repair_and_next_success_completes(
    policy: RepairPolicy,
) -> None:
    original = task("implement")
    current = plan((original,))
    repair = task("repair", revision=2, dependencies=(original.id,))
    mutation = TaskPlanMutation(
        base_revision=1,
        reason="Targeted verification failed",
        tasks=(repair,),
    )

    repair_decision = policy.decide(current, failed(), mutation=mutation, history=())

    assert repair_decision.action is RepairAction.APPLY_MUTATION
    assert repair_decision.plan is not None
    assert repair_decision.plan.revision == 2
    assert repair_decision.plan.tasks[-1] == repair

    success = VerificationOutcome(
        passed=True,
        progress_fingerprint="3" * 64,
        artifact_ids=(uuid4(),),
    )
    complete = policy.decide(repair_decision.plan, success, mutation=None, history=())
    assert complete.action is RepairAction.COMPLETE


@pytest.mark.parametrize(
    ("limits", "repair_cost", "repair_seconds", "expected"),
    [
        (
            PlanLimits(
                max_dynamic_tasks=0,
                max_plan_depth=5,
                max_total_budget_usd=10,
                max_total_execution_seconds=1_000,
            ),
            1,
            60,
            "MAX_DYNAMIC_TASKS",
        ),
        (
            PlanLimits(
                max_dynamic_tasks=5,
                max_plan_depth=1,
                max_total_budget_usd=10,
                max_total_execution_seconds=1_000,
            ),
            1,
            60,
            "MAX_PLAN_DEPTH",
        ),
        (
            PlanLimits(
                max_dynamic_tasks=5,
                max_plan_depth=5,
                max_total_budget_usd=1,
                max_total_execution_seconds=1_000,
            ),
            1,
            60,
            "MAX_TOTAL_BUDGET",
        ),
        (
            PlanLimits(
                max_dynamic_tasks=5,
                max_plan_depth=5,
                max_total_budget_usd=10,
                max_total_execution_seconds=60,
            ),
            1,
            60,
            "MAX_TOTAL_EXECUTION_TIME",
        ),
    ],
)
def test_repair_terminates_at_every_plan_expansion_guard(
    policy: RepairPolicy,
    limits: PlanLimits,
    repair_cost: float,
    repair_seconds: int,
    expected: str,
) -> None:
    original = task("original")
    current = plan((original,), limits=limits)
    repair = task(
        "repair",
        revision=2,
        dependencies=(original.id,),
        cost=repair_cost,
        seconds=repair_seconds,
    )
    mutation = TaskPlanMutation(base_revision=1, reason="repair", tasks=(repair,))

    decision = policy.decide(current, failed(), mutation=mutation, history=())

    assert decision.action is RepairAction.TERMINATE
    assert expected in decision.reason_codes


def test_repeated_failure_signature_and_no_progress_terminate(
    policy: RepairPolicy,
) -> None:
    original = task("original")
    current = plan((original,))
    repair = task("repair", revision=2)
    mutation = TaskPlanMutation(base_revision=1, reason="repair", tasks=(repair,))
    history = (
        RepairHistory(
            failure_signature="1" * 64,
            progress_fingerprint="2" * 64,
            accepted_revision=2,
        ),
    )

    repeated = policy.decide(current, failed(), mutation=mutation, history=history)
    stalled = policy.decide(
        current,
        failed(signature="4" * 64, progress="2" * 64),
        mutation=mutation,
        history=history,
    )

    assert repeated.reason_codes == ("REPEATED_FAILURE_SIGNATURE",)
    assert stalled.reason_codes == ("NO_PROGRESS",)
