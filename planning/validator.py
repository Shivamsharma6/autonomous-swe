from __future__ import annotations

import heapq
from dataclasses import dataclass
from pathlib import PurePosixPath
from uuid import UUID

from domain.models import TaskPlan, TaskPlanMutation, TaskSpec


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    code: str
    message: str
    task_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class PlanValidationResult:
    valid: bool
    issues: tuple[ValidationIssue, ...]
    topological_order: tuple[UUID, ...]
    max_depth: int
    total_budget_usd: float
    total_execution_seconds: int


@dataclass(frozen=True, slots=True)
class MutationValidationResult:
    valid: bool
    issues: tuple[ValidationIssue, ...]
    proposed_plan: TaskPlan | None


class TaskPlanValidator:
    def __init__(self, *, allowed_tools: set[str] | frozenset[str]) -> None:
        self.allowed_tools = frozenset(allowed_tools)

    def validate(self, plan: TaskPlan) -> PlanValidationResult:
        issues: list[ValidationIssue] = []
        tasks_by_id: dict[UUID, TaskSpec] = {}
        duplicate_ids: set[UUID] = set()
        for task in plan.tasks:
            if task.id in tasks_by_id:
                duplicate_ids.add(task.id)
            else:
                tasks_by_id[task.id] = task

        for task_id in sorted(duplicate_ids, key=lambda value: value.int):
            issues.append(
                ValidationIssue(
                    code="DUPLICATE_TASK_ID",
                    message=f"task ID {task_id} occurs more than once",
                    task_id=task_id,
                )
            )

        for task in sorted(plan.tasks, key=lambda item: item.id.int):
            issues.extend(self._validate_task_scope(plan, task))
            issues.extend(self._validate_task_semantics(task, tasks_by_id))

        topological_order = self._topological_order(tasks_by_id)
        if len(topological_order) != len(tasks_by_id):
            issues.append(
                ValidationIssue(
                    code="CYCLE",
                    message="task dependencies contain at least one cycle",
                )
            )

        max_depth = self._maximum_depth(tasks_by_id, topological_order)
        if max_depth > plan.limits.max_plan_depth:
            issues.append(
                ValidationIssue(
                    code="MAX_PLAN_DEPTH",
                    message=(f"plan depth {max_depth} exceeds limit {plan.limits.max_plan_depth}"),
                )
            )

        dynamic_tasks = sum(task.plan_revision > 1 for task in plan.tasks)
        if dynamic_tasks > plan.limits.max_dynamic_tasks:
            issues.append(
                ValidationIssue(
                    code="MAX_DYNAMIC_TASKS",
                    message=(
                        f"dynamic task count {dynamic_tasks} exceeds limit "
                        f"{plan.limits.max_dynamic_tasks}"
                    ),
                )
            )

        total_budget = sum(task.budget.cost_usd for task in plan.tasks)
        if total_budget > plan.limits.max_total_budget_usd:
            issues.append(
                ValidationIssue(
                    code="MAX_TOTAL_BUDGET",
                    message=(
                        f"total budget {total_budget:.6f} exceeds limit "
                        f"{plan.limits.max_total_budget_usd:.6f}"
                    ),
                )
            )

        total_execution = sum(task.budget.wall_time_seconds for task in plan.tasks)
        if total_execution > plan.limits.max_total_execution_seconds:
            issues.append(
                ValidationIssue(
                    code="MAX_TOTAL_EXECUTION_TIME",
                    message=(
                        f"total execution time {total_execution} exceeds limit "
                        f"{plan.limits.max_total_execution_seconds}"
                    ),
                )
            )

        return PlanValidationResult(
            valid=not issues,
            issues=tuple(issues),
            topological_order=topological_order,
            max_depth=max_depth,
            total_budget_usd=total_budget,
            total_execution_seconds=total_execution,
        )

    def validate_mutation(
        self, current: TaskPlan, mutation: TaskPlanMutation
    ) -> MutationValidationResult:
        issues: list[ValidationIssue] = []
        if mutation.base_revision != current.revision:
            issues.append(
                ValidationIssue(
                    code="STALE_PLAN_REVISION",
                    message=(
                        f"mutation targets revision {mutation.base_revision}; "
                        f"current revision is {current.revision}"
                    ),
                )
            )

        current_ids = {task.id for task in current.tasks}
        mutation_ids: set[UUID] = set()
        next_revision = current.revision + 1
        for task in mutation.tasks:
            if task.id in current_ids:
                issues.append(
                    ValidationIssue(
                        code="EXISTING_TASK_MUTATION",
                        message="a mutation cannot replace or edit an existing task",
                        task_id=task.id,
                    )
                )
            if task.id in mutation_ids:
                issues.append(
                    ValidationIssue(
                        code="DUPLICATE_TASK_ID",
                        message="a mutation cannot add the same task twice",
                        task_id=task.id,
                    )
                )
            mutation_ids.add(task.id)
            if task.plan_revision != next_revision:
                issues.append(
                    ValidationIssue(
                        code="PLAN_REVISION",
                        message=f"new tasks must be created in revision {next_revision}",
                        task_id=task.id,
                    )
                )

        if issues:
            return MutationValidationResult(valid=False, issues=tuple(issues), proposed_plan=None)

        proposed = TaskPlan(
            run_id=current.run_id,
            project_id=current.project_id,
            repository_id=current.repository_id,
            baseline_commit=current.baseline_commit,
            revision=next_revision,
            tasks=current.tasks + mutation.tasks,
            limits=current.limits,
        )
        validation = self.validate(proposed)
        if not validation.valid:
            return MutationValidationResult(
                valid=False,
                issues=validation.issues,
                proposed_plan=None,
            )
        return MutationValidationResult(valid=True, issues=(), proposed_plan=proposed)

    def _validate_task_scope(self, plan: TaskPlan, task: TaskSpec) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        if task.project_id != plan.project_id:
            issues.append(
                ValidationIssue(
                    code="PROJECT_SCOPE",
                    message="task project does not match its plan",
                    task_id=task.id,
                )
            )
        if task.repository_id != plan.repository_id:
            issues.append(
                ValidationIssue(
                    code="REPOSITORY_SCOPE",
                    message="task repository does not match its plan",
                    task_id=task.id,
                )
            )
        if task.plan_revision > plan.revision:
            issues.append(
                ValidationIssue(
                    code="PLAN_REVISION",
                    message="task creation revision exceeds the current plan revision",
                    task_id=task.id,
                )
            )
        return issues

    def _validate_task_semantics(
        self, task: TaskSpec, tasks_by_id: dict[UUID, TaskSpec]
    ) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        if not task.acceptance_criteria:
            issues.append(
                ValidationIssue(
                    code="MISSING_ACCEPTANCE_CRITERIA",
                    message="task must define at least one testable acceptance criterion",
                    task_id=task.id,
                )
            )
        for tool in sorted(set(task.allowed_tools) - self.allowed_tools):
            issues.append(
                ValidationIssue(
                    code="UNSUPPORTED_TOOL",
                    message=f"tool {tool!r} is not registered for planning",
                    task_id=task.id,
                )
            )
        for path in task.repository_paths:
            if not self._is_repository_relative(path):
                issues.append(
                    ValidationIssue(
                        code="REPOSITORY_PATH_ESCAPE",
                        message=f"path {path!r} escapes the managed repository",
                        task_id=task.id,
                    )
                )
        for dependency in task.dependencies:
            if dependency == task.id:
                issues.append(
                    ValidationIssue(
                        code="SELF_DEPENDENCY",
                        message="task cannot depend on itself",
                        task_id=task.id,
                    )
                )
            elif dependency not in tasks_by_id:
                issues.append(
                    ValidationIssue(
                        code="MISSING_DEPENDENCY",
                        message=f"dependency {dependency} does not exist in the plan",
                        task_id=task.id,
                    )
                )
        return issues

    @staticmethod
    def _is_repository_relative(value: str) -> bool:
        normalized = value.replace("\\", "/")
        path = PurePosixPath(normalized)
        return (
            bool(normalized)
            and not path.is_absolute()
            and ".." not in path.parts
            and ":" not in path.parts[0]
        )

    @staticmethod
    def _topological_order(tasks_by_id: dict[UUID, TaskSpec]) -> tuple[UUID, ...]:
        indegree = {task_id: 0 for task_id in tasks_by_id}
        dependents: dict[UUID, list[UUID]] = {task_id: [] for task_id in tasks_by_id}
        for task in tasks_by_id.values():
            for dependency in task.dependencies:
                if dependency in tasks_by_id:
                    indegree[task.id] += 1
                    dependents[dependency].append(task.id)

        ready: list[tuple[int, UUID]] = [
            (task_id.int, task_id) for task_id, degree in indegree.items() if degree == 0
        ]
        heapq.heapify(ready)
        result: list[UUID] = []
        while ready:
            _, task_id = heapq.heappop(ready)
            result.append(task_id)
            for dependent in sorted(dependents[task_id], key=lambda value: value.int):
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    heapq.heappush(ready, (dependent.int, dependent))
        return tuple(result)

    @staticmethod
    def _maximum_depth(
        tasks_by_id: dict[UUID, TaskSpec], topological_order: tuple[UUID, ...]
    ) -> int:
        depths: dict[UUID, int] = {}
        for task_id in topological_order:
            dependencies = [
                depths[dependency]
                for dependency in tasks_by_id[task_id].dependencies
                if dependency in depths
            ]
            depths[task_id] = 1 + max(dependencies, default=0)
        return max(depths.values(), default=0)
