"""Authoritative scheduler API.

The prototype in-memory scheduler and fixed planner were deliberately removed. Planning now
produces validated ``domain.models.TaskPlan`` values; this module exposes only the durable
PostgreSQL scheduler.
"""

from domain.enums import RetryCategory, TaskStatus
from execution.scheduler.reconciliation import (
    ReconciliationAction,
    ReconciliationService,
)
from execution.scheduler.service import (
    AdmissionDecision,
    AdmissionSnapshot,
    ConcurrencyPolicy,
    ResourceObservation,
    ResourceRequest,
    RetryBudget,
    RetryDecision,
    SchedulerService,
    TaskClaim,
    dependencies_satisfied,
    evaluate_admission,
    evaluate_retry,
)

__all__ = [
    "AdmissionDecision",
    "AdmissionSnapshot",
    "ConcurrencyPolicy",
    "ReconciliationAction",
    "ReconciliationService",
    "ResourceObservation",
    "ResourceRequest",
    "RetryBudget",
    "RetryCategory",
    "RetryDecision",
    "SchedulerService",
    "TaskClaim",
    "TaskStatus",
    "dependencies_satisfied",
    "evaluate_admission",
    "evaluate_retry",
]
