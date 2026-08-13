from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class SLOKind(StrEnum):
    AVAILABILITY = "availability"
    LATENCY = "latency"
    DURABILITY = "durability"
    INTEGRITY = "integrity"
    RECOVERY = "recovery"


@dataclass(frozen=True, slots=True)
class ServiceLevelObjective:
    name: str
    kind: SLOKind
    target: float
    window_days: int
    threshold_seconds: float | None
    percentile: float | None
    eligibility: str
    fast_burn_alert: float = 14.4
    slow_burn_alert: float = 6.0


INITIAL_SLOS: dict[str, ServiceLevelObjective] = {
    "api_availability": ServiceLevelObjective(
        "api_availability", SLOKind.AVAILABILITY, 0.995, 30, None, None,
        "Authenticated API requests excluding explicit client errors and maintenance windows.",
    ),
    "task_dispatch_latency": ServiceLevelObjective(
        "task_dispatch_latency", SLOKind.LATENCY, 0.95, 30, 2.0, 0.95,
        "READY tasks with available scheduler capacity.",
    ),
    "checkpoint_durability": ServiceLevelObjective(
        "checkpoint_durability", SLOKind.DURABILITY, 0.9999, 30, None, None,
        "Acknowledged synchronous LangGraph checkpoint writes.",
    ),
    "event_delivery_latency": ServiceLevelObjective(
        "event_delivery_latency", SLOKind.LATENCY, 0.99, 30, 5.0, 0.99,
        "Committed transactional-outbox events while Redis is available.",
    ),
    "approval_notification_latency": ServiceLevelObjective(
        "approval_notification_latency", SLOKind.LATENCY, 0.95, 30, 5.0, 0.95,
        "Committed approval requests with an active notification channel.",
    ),
    "cancellation_propagation": ServiceLevelObjective(
        "cancellation_propagation", SLOKind.LATENCY, 0.99, 30, 10.0, 0.99,
        "Accepted cancellation requests for active workers and sandboxes.",
    ),
    "artifact_integrity": ServiceLevelObjective(
        "artifact_integrity", SLOKind.INTEGRITY, 1.0, 30, None, None,
        "Artifacts presented as verified evidence.",
    ),
    "worker_failure_recovery": ServiceLevelObjective(
        "worker_failure_recovery", SLOKind.RECOVERY, 0.95, 30, 60.0, 0.95,
        "Worker failures with a durable checkpoint and remaining retry budget.",
    ),
}


def burn_rate(*, observed_bad_fraction: float, objective: float) -> float:
    if not 0 <= observed_bad_fraction <= 1:
        raise ValueError("observed_bad_fraction must be between zero and one")
    if not 0 < objective < 1:
        raise ValueError("objective must be strictly between zero and one")
    return observed_bad_fraction / (1 - objective)
