from __future__ import annotations

from uuid import uuid4

import pytest

from domain.enums import RetryCategory, TaskStatus
from execution.scheduler.service import (
    AdmissionSnapshot,
    ConcurrencyPolicy,
    ResourceRequest,
    RetryBudget,
    dependencies_satisfied,
    evaluate_admission,
    evaluate_retry,
)


def policy() -> ConcurrencyPolicy:
    return ConcurrencyPolicy(
        max_parallel_tasks=4,
        max_parallel_tasks_per_project=2,
        max_model_concurrency=3,
        max_sandbox_concurrency=1,
    )


def test_dependencies_are_ready_only_when_all_are_completed() -> None:
    first, second = uuid4(), uuid4()

    assert dependencies_satisfied((), {})
    assert dependencies_satisfied(
        (first, second),
        {first: TaskStatus.COMPLETED, second: TaskStatus.COMPLETED},
    )
    assert not dependencies_satisfied(
        (first, second),
        {first: TaskStatus.COMPLETED, second: TaskStatus.RUNNING},
    )
    assert not dependencies_satisfied((first,), {})


@pytest.mark.parametrize(
    ("snapshot", "resource_request", "reason"),
    [
        (
            AdmissionSnapshot(
                active_tasks=4,
                active_project_tasks=0,
                active_model_slots=0,
                active_sandbox_slots=0,
            ),
            ResourceRequest(),
            "MAX_PARALLEL_TASKS",
        ),
        (
            AdmissionSnapshot(
                active_tasks=1,
                active_project_tasks=2,
                active_model_slots=0,
                active_sandbox_slots=0,
            ),
            ResourceRequest(),
            "MAX_PROJECT_PARALLEL_TASKS",
        ),
        (
            AdmissionSnapshot(
                active_tasks=1,
                active_project_tasks=1,
                active_model_slots=3,
                active_sandbox_slots=0,
            ),
            ResourceRequest(model_slots=1),
            "MAX_MODEL_CONCURRENCY",
        ),
        (
            AdmissionSnapshot(
                active_tasks=1,
                active_project_tasks=1,
                active_model_slots=0,
                active_sandbox_slots=1,
            ),
            ResourceRequest(sandbox_slots=1),
            "MAX_SANDBOX_CONCURRENCY",
        ),
    ],
)
def test_each_concurrency_ceiling_is_authoritative(
    snapshot: AdmissionSnapshot,
    resource_request: ResourceRequest,
    reason: str,
) -> None:
    decision = evaluate_admission(policy(), snapshot, resource_request)

    assert decision.admitted is False
    assert decision.reason == reason


def test_admission_accepts_request_with_capacity() -> None:
    decision = evaluate_admission(
        policy(),
        AdmissionSnapshot(
            active_tasks=1,
            active_project_tasks=1,
            active_model_slots=1,
            active_sandbox_slots=0,
        ),
        ResourceRequest(model_slots=1, sandbox_slots=1),
    )

    assert decision.admitted is True
    assert decision.reason is None


@pytest.mark.parametrize(
    "category",
    [
        RetryCategory.PERMANENT,
        RetryCategory.POLICY,
        RetryCategory.BUDGET,
        RetryCategory.CANCELLATION,
        RetryCategory.UNCERTAIN_SIDE_EFFECT,
    ],
)
def test_non_transient_retry_categories_are_not_retried(category: RetryCategory) -> None:
    decision = evaluate_retry(
        category,
        attempts=1,
        consumed_cost_usd=0.1,
        budget=RetryBudget(max_attempts=3, max_cost_usd=1),
    )

    assert decision.retry is False
    assert decision.category is category


def test_transient_retry_requires_attempt_and_cost_budget() -> None:
    budget = RetryBudget(max_attempts=3, max_cost_usd=1)

    assert evaluate_retry(
        RetryCategory.TRANSIENT, attempts=2, consumed_cost_usd=0.5, budget=budget
    ).retry
    assert not evaluate_retry(
        RetryCategory.TRANSIENT, attempts=3, consumed_cost_usd=0.5, budget=budget
    ).retry
    assert not evaluate_retry(
        RetryCategory.TRANSIENT, attempts=1, consumed_cost_usd=1, budget=budget
    ).retry
