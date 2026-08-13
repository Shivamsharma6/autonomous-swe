from __future__ import annotations

from uuid import UUID

from hypothesis import given, settings
from hypothesis import strategies as st

from domain.enums import RiskLevel, TaskType
from domain.models import BudgetPolicy, PlanLimits, TaskPlan, TaskPlanMutation, TaskSpec
from planning.validator import TaskPlanValidator

PROJECT_ID = UUID(int=10_001)
REPOSITORY_ID = UUID(int=10_002)
RUN_ID = UUID(int=10_003)
VALIDATOR = TaskPlanValidator(allowed_tools={"read_file"})


def make_task(index: int, dependencies: tuple[UUID, ...], revision: int = 1) -> TaskSpec:
    return TaskSpec(
        id=UUID(int=index + 1),
        plan_revision=revision,
        project_id=PROJECT_ID,
        repository_id=REPOSITORY_ID,
        title=f"task-{index}",
        description=f"Complete task {index}",
        task_type=TaskType.VALIDATION,
        dependencies=dependencies,
        assigned_capability="validator",
        acceptance_criteria=("Evidence is valid",),
        allowed_tools=("read_file",),
        risk_ceiling=RiskLevel.LOW,
        budget=BudgetPolicy(cost_usd=0.1, wall_time_seconds=1),
    )


def make_plan(tasks: tuple[TaskSpec, ...], revision: int = 1) -> TaskPlan:
    return TaskPlan(
        run_id=RUN_ID,
        project_id=PROJECT_ID,
        repository_id=REPOSITORY_ID,
        baseline_commit="a" * 40,
        revision=revision,
        tasks=tasks,
        limits=PlanLimits(
            max_dynamic_tasks=20,
            max_plan_depth=20,
            max_total_budget_usd=100,
            max_total_execution_seconds=1_000,
        ),
    )


@settings(max_examples=50, deadline=None)
@given(st.integers(min_value=1, max_value=15))
def test_generated_forward_dags_are_accepted_and_topologically_ordered(size: int) -> None:
    tasks = tuple(
        make_task(index, () if index == 0 else (UUID(int=index),)) for index in range(size)
    )

    result = VALIDATOR.validate(make_plan(tuple(reversed(tasks))))

    assert result.valid
    positions = {task_id: position for position, task_id in enumerate(result.topological_order)}
    for item in tasks:
        assert all(positions[dependency] < positions[item.id] for dependency in item.dependencies)


@settings(max_examples=50, deadline=None)
@given(st.integers(min_value=1, max_value=10), st.integers(min_value=1, max_value=5))
def test_accepted_mutation_never_changes_existing_tasks(size: int, added: int) -> None:
    existing = tuple(
        make_task(index, () if index == 0 else (UUID(int=index),)) for index in range(size)
    )
    current = make_plan(existing)
    additions = tuple(
        TaskSpec(
            id=UUID(int=size + index + 1),
            plan_revision=2,
            project_id=PROJECT_ID,
            repository_id=REPOSITORY_ID,
            title=f"repair-{index}",
            description=f"Repair task {index}",
            task_type=TaskType.VALIDATION,
            dependencies=(existing[-1].id,),
            assigned_capability="validator",
            acceptance_criteria=("Repair evidence is valid",),
            allowed_tools=("read_file",),
            risk_ceiling=RiskLevel.LOW,
            budget=BudgetPolicy(cost_usd=0.1, wall_time_seconds=1),
        )
        for index in range(added)
    )

    result = VALIDATOR.validate_mutation(
        current,
        TaskPlanMutation(base_revision=1, reason="Repair", tasks=additions),
    )

    assert result.valid
    assert result.proposed_plan is not None
    assert result.proposed_plan.tasks[:size] == existing
    assert len(result.proposed_plan.tasks) <= size + current.limits.max_dynamic_tasks
