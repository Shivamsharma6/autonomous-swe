from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from domain.enums import RiskLevel, TaskType
from domain.models import (
    BudgetPolicy,
    PlanLimits,
    TaskPlan,
    TaskPlanMutation,
    TaskSpec,
)
from planning.validator import TaskPlanValidator

PROJECT_ID = uuid4()
REPOSITORY_ID = uuid4()
RUN_ID = uuid4()


def task(
    name: str,
    *,
    task_id: UUID | None = None,
    dependencies: tuple[UUID, ...] = (),
    plan_revision: int = 1,
    tools: tuple[str, ...] = ("read_file",),
    criteria: tuple[str, ...] = ("Evidence is produced",),
    paths: tuple[str, ...] = (),
    cost: float = 1,
    seconds: int = 60,
) -> TaskSpec:
    return TaskSpec(
        id=task_id or uuid4(),
        plan_revision=plan_revision,
        project_id=PROJECT_ID,
        repository_id=REPOSITORY_ID,
        title=name,
        description=f"Complete {name}",
        task_type=TaskType.IMPLEMENTATION,
        dependencies=dependencies,
        assigned_capability="coder",
        acceptance_criteria=criteria,
        allowed_tools=tools,
        risk_ceiling=RiskLevel.MEDIUM,
        repository_paths=paths,
        budget=BudgetPolicy(cost_usd=cost, wall_time_seconds=seconds),
    )


def plan(
    tasks: tuple[TaskSpec, ...],
    *,
    revision: int = 1,
    limits: PlanLimits | None = None,
) -> TaskPlan:
    return TaskPlan(
        run_id=RUN_ID,
        project_id=PROJECT_ID,
        repository_id=REPOSITORY_ID,
        baseline_commit="a" * 40,
        revision=revision,
        tasks=tasks,
        limits=limits
        or PlanLimits(
            max_dynamic_tasks=10,
            max_plan_depth=10,
            max_total_budget_usd=100,
            max_total_execution_seconds=10_000,
        ),
    )


@pytest.fixture
def validator() -> TaskPlanValidator:
    return TaskPlanValidator(allowed_tools={"read_file", "apply_patch", "run_tests"})


def issue_codes(result: object) -> set[str]:
    return {issue.code for issue in result.issues}  # type: ignore[attr-defined]


def test_accepts_acyclic_plan_and_returns_stable_topological_order(
    validator: TaskPlanValidator,
) -> None:
    first = task("first", task_id=UUID(int=1))
    second = task("second", task_id=UUID(int=2), dependencies=(first.id,))
    third = task("third", task_id=UUID(int=3), dependencies=(first.id,))

    result = validator.validate(plan((third, second, first)))

    assert result.valid is True
    assert result.topological_order == (first.id, second.id, third.id)
    assert result.max_depth == 2


def test_rejects_missing_dependency(validator: TaskPlanValidator) -> None:
    result = validator.validate(plan((task("orphan", dependencies=(uuid4(),)),)))

    assert "MISSING_DEPENDENCY" in issue_codes(result)


def test_rejects_self_dependency(validator: TaskPlanValidator) -> None:
    original = task("recursive")
    invalid = original.model_copy(update={"dependencies": (original.id,)})
    valid_plan = plan((original,))
    invalid_plan = TaskPlan.model_construct(
        schema_version=valid_plan.schema_version,
        run_id=valid_plan.run_id,
        project_id=valid_plan.project_id,
        repository_id=valid_plan.repository_id,
        baseline_commit=valid_plan.baseline_commit,
        revision=valid_plan.revision,
        tasks=(invalid,),
        limits=valid_plan.limits,
    )

    result = validator.validate(invalid_plan)

    assert "SELF_DEPENDENCY" in issue_codes(result)


def test_rejects_cycle(validator: TaskPlanValidator) -> None:
    first_id, second_id = uuid4(), uuid4()
    first = task("first", task_id=first_id, dependencies=(second_id,))
    second = task("second", task_id=second_id, dependencies=(first_id,))

    result = validator.validate(plan((first, second)))

    assert "CYCLE" in issue_codes(result)


def test_rejects_duplicate_task_ids(validator: TaskPlanValidator) -> None:
    duplicate_id = uuid4()
    first = task("first", task_id=duplicate_id)
    second = task("second", task_id=duplicate_id)
    invalid_plan = TaskPlan.model_construct(
        run_id=RUN_ID,
        project_id=PROJECT_ID,
        repository_id=REPOSITORY_ID,
        baseline_commit="a" * 40,
        revision=1,
        tasks=(first, second),
        limits=plan((first,)).limits,
        schema_version="1.0",
    )

    result = validator.validate(invalid_plan)

    assert "DUPLICATE_TASK_ID" in issue_codes(result)


def test_rejects_task_outside_repository_scope(validator: TaskPlanValidator) -> None:
    invalid = task("foreign").model_copy(update={"repository_id": uuid4()})
    valid_plan = plan((task("valid"),))
    invalid_plan = TaskPlan.model_construct(
        schema_version=valid_plan.schema_version,
        run_id=valid_plan.run_id,
        project_id=valid_plan.project_id,
        repository_id=valid_plan.repository_id,
        baseline_commit=valid_plan.baseline_commit,
        revision=valid_plan.revision,
        tasks=(invalid,),
        limits=valid_plan.limits,
    )

    result = validator.validate(invalid_plan)

    assert "REPOSITORY_SCOPE" in issue_codes(result)


def test_rejects_empty_acceptance_criteria(validator: TaskPlanValidator) -> None:
    result = validator.validate(plan((task("vague", criteria=()),)))

    assert "MISSING_ACCEPTANCE_CRITERIA" in issue_codes(result)


def test_rejects_unsupported_tool(validator: TaskPlanValidator) -> None:
    result = validator.validate(plan((task("unsafe", tools=("host_shell",)),)))

    assert "UNSUPPORTED_TOOL" in issue_codes(result)


@pytest.mark.parametrize("path", ["/etc/passwd", "../secrets.env", "src/../../outside"])
def test_rejects_repository_path_escape(validator: TaskPlanValidator, path: str) -> None:
    result = validator.validate(plan((task("escape", paths=(path,)),)))

    assert "REPOSITORY_PATH_ESCAPE" in issue_codes(result)


def test_rejects_plan_depth_limit(validator: TaskPlanValidator) -> None:
    first = task("first")
    second = task("second", dependencies=(first.id,))
    third = task("third", dependencies=(second.id,))
    limits = PlanLimits(
        max_dynamic_tasks=10,
        max_plan_depth=2,
        max_total_budget_usd=100,
        max_total_execution_seconds=10_000,
    )

    result = validator.validate(plan((first, second, third), limits=limits))

    assert "MAX_PLAN_DEPTH" in issue_codes(result)


def test_rejects_total_budget_limit(validator: TaskPlanValidator) -> None:
    limits = PlanLimits(
        max_dynamic_tasks=10,
        max_plan_depth=10,
        max_total_budget_usd=1,
        max_total_execution_seconds=10_000,
    )

    result = validator.validate(plan((task("expensive", cost=2),), limits=limits))

    assert "MAX_TOTAL_BUDGET" in issue_codes(result)


def test_rejects_total_execution_time_limit(validator: TaskPlanValidator) -> None:
    limits = PlanLimits(
        max_dynamic_tasks=10,
        max_plan_depth=10,
        max_total_budget_usd=100,
        max_total_execution_seconds=30,
    )

    result = validator.validate(plan((task("slow", seconds=31),), limits=limits))

    assert "MAX_TOTAL_EXECUTION_TIME" in issue_codes(result)


def test_valid_mutation_preserves_old_task_creation_revision(
    validator: TaskPlanValidator,
) -> None:
    original = task("original", task_id=UUID(int=1))
    current = plan((original,))
    repair = task(
        "repair",
        task_id=UUID(int=2),
        dependencies=(original.id,),
        plan_revision=2,
    )
    mutation = TaskPlanMutation(base_revision=1, reason="Tests failed", tasks=(repair,))

    result = validator.validate_mutation(current, mutation)

    assert result.valid is True
    assert result.proposed_plan is not None
    assert result.proposed_plan.revision == 2
    assert result.proposed_plan.tasks == (original, repair)


def test_mutation_cannot_replace_existing_task(validator: TaskPlanValidator) -> None:
    original = task("original")
    current = plan((original,))
    replacement = task("changed", task_id=original.id, plan_revision=2)
    mutation = TaskPlanMutation(base_revision=1, reason="Replace history", tasks=(replacement,))

    result = validator.validate_mutation(current, mutation)

    assert "EXISTING_TASK_MUTATION" in issue_codes(result)
    assert result.proposed_plan is None
    assert current.tasks == (original,)


def test_mutation_limit_is_checked_before_acceptance(validator: TaskPlanValidator) -> None:
    original = task("original")
    limits = PlanLimits(
        max_dynamic_tasks=1,
        max_plan_depth=10,
        max_total_budget_usd=100,
        max_total_execution_seconds=10_000,
    )
    current = plan((original,), limits=limits)
    first = task("repair one", plan_revision=2)
    second = task("repair two", plan_revision=2)
    mutation = TaskPlanMutation(base_revision=1, reason="Too many repairs", tasks=(first, second))

    result = validator.validate_mutation(current, mutation)

    assert "MAX_DYNAMIC_TASKS" in issue_codes(result)
    assert result.proposed_plan is None
