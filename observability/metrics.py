from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from prometheus_client import (
    REGISTRY,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    start_http_server,
)

STATE_BUCKETS = (0.1, 0.5, 1, 2, 5, 10, 30, 60, 300, 900, 3_600, 14_400, 86_400)
LATENCY_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 300)


class PlatformMetrics:
    """Low-cardinality platform metrics; execution IDs belong in traces and logs."""

    def __init__(self, *, registry: CollectorRegistry = REGISTRY) -> None:
        self.task_state_duration = Histogram(
            "task_state_duration_seconds",
            "Time spent in each authoritative scheduler task state.",
            ("state",),
            buckets=STATE_BUCKETS,
            registry=registry,
        )
        self.workflow_state_duration = Histogram(
            "workflow_state_duration_seconds",
            "Time spent in each LangGraph execution state.",
            ("state",),
            buckets=STATE_BUCKETS,
            registry=registry,
        )
        self.approval_wait_duration = Histogram(
            "approval_wait_duration_seconds",
            "Time spent waiting for an exact-call approval decision.",
            ("state",),
            buckets=STATE_BUCKETS,
            registry=registry,
        )
        self.uams_wait_duration = Histogram(
            "uams_wait_duration_seconds",
            "Time spent waiting for external UAMS operations.",
            ("state",),
            buckets=STATE_BUCKETS,
            registry=registry,
        )
        self.queue_depth = Gauge(
            "autoswe_task_queue_depth",
            "Current tasks by bounded queue state.",
            ("state",),
            registry=registry,
        )
        self.resource_reservations = Gauge(
            "autoswe_resource_reservations",
            "Authoritative scheduler reservations by bounded resource type.",
            ("resource",),
            registry=registry,
        )
        self.resource_actual_usage = Gauge(
            "autoswe_resource_actual_usage",
            "Observed concurrent use by bounded resource type.",
            ("resource",),
            registry=registry,
        )
        self.dispatch_latency = Histogram(
            "autoswe_task_dispatch_latency_seconds",
            "Latency from ready admission to dispatch.",
            ("outcome",),
            buckets=LATENCY_BUCKETS,
            registry=registry,
        )
        self.event_delivery_latency = Histogram(
            "autoswe_event_delivery_latency_seconds",
            "Transactional-outbox event delivery latency.",
            ("outcome",),
            buckets=LATENCY_BUCKETS,
            registry=registry,
        )
        self.sandbox_cpu = Histogram(
            "autoswe_sandbox_cpu_time_seconds",
            "Sandbox CPU time.",
            ("exit_reason",),
            buckets=STATE_BUCKETS,
            registry=registry,
        )
        self.sandbox_memory = Histogram(
            "autoswe_sandbox_peak_memory_bytes",
            "Sandbox peak memory in bytes.",
            ("exit_reason",),
            buckets=(1_048_576, 16_777_216, 67_108_864, 268_435_456, 1_073_741_824),
            registry=registry,
        )
        self.sandbox_duration = Histogram(
            "autoswe_sandbox_duration_seconds",
            "Sandbox wall-clock duration.",
            ("exit_reason",),
            buckets=STATE_BUCKETS,
            registry=registry,
        )
        self.dead_letters = Gauge(
            "autoswe_unresolved_dead_letters",
            "Current unresolved PostgreSQL dead letters.",
            registry=registry,
        )
        self.artifact_integrity_failures = Counter(
            "autoswe_artifact_integrity_failures_total",
            "Artifacts quarantined after integrity verification failure.",
            registry=registry,
        )
        self.slo_burn_rate = Gauge(
            "autoswe_slo_error_budget_burn_rate",
            "Current error-budget burn rate for an approved SLO and window.",
            ("slo", "window"),
            registry=registry,
        )

    def observe_state_duration(
        self, aggregate_type: str, state: str, duration_seconds: float
    ) -> None:
        if duration_seconds < 0:
            raise ValueError("state duration cannot be negative")
        instruments = {
            "task": self.task_state_duration,
            "workflow": self.workflow_state_duration,
            "approval": self.approval_wait_duration,
            "uams": self.uams_wait_duration,
        }
        try:
            instrument = instruments[aggregate_type]
        except KeyError as error:
            raise ValueError(f"unsupported state aggregate type: {aggregate_type}") from error
        instrument.labels(state=state).observe(duration_seconds)

    def set_queue_depth(self, state: str, value: int) -> None:
        if value < 0:
            raise ValueError("queue depth cannot be negative")
        self.queue_depth.labels(state=state).set(value)

    def set_resource_usage(self, resource: str, *, reserved: int, actual: int) -> None:
        self.set_resource_reservations(resource, reserved)
        self.set_resource_actual(resource, actual)

    def set_resource_reservations(self, resource: str, value: int) -> None:
        self._validate_resource_value(resource, value)
        self.resource_reservations.labels(resource=resource).set(value)

    def set_resource_actual(self, resource: str, value: int) -> None:
        self._validate_resource_value(resource, value)
        self.resource_actual_usage.labels(resource=resource).set(value)

    @staticmethod
    def _validate_resource_value(resource: str, value: int) -> None:
        if resource not in {"task", "model", "sandbox"}:
            raise ValueError("resource must be task, model, or sandbox")
        if value < 0:
            raise ValueError("resource usage cannot be negative")

    def set_slo_burn_rate(self, slo: str, window: str, value: float) -> None:
        if value < 0:
            raise ValueError("SLO burn rate cannot be negative")
        self.slo_burn_rate.labels(slo=slo, window=window).set(value)

    def observe_dispatch(self, duration_seconds: float, *, outcome: str) -> None:
        self.dispatch_latency.labels(outcome=outcome).observe(duration_seconds)

    def observe_event_delivery(self, duration_seconds: float, *, outcome: str) -> None:
        self.event_delivery_latency.labels(outcome=outcome).observe(duration_seconds)

    def observe_sandbox_usage(
        self,
        *,
        cpu_seconds: float,
        peak_memory_bytes: int,
        duration_seconds: float,
        exit_reason: str,
    ) -> None:
        self.sandbox_cpu.labels(exit_reason=exit_reason).observe(cpu_seconds)
        self.sandbox_memory.labels(exit_reason=exit_reason).observe(peak_memory_bytes)
        self.sandbox_duration.labels(exit_reason=exit_reason).observe(duration_seconds)


platform_metrics = PlatformMetrics()


@contextmanager
def track_actual_resource(resource: str) -> Iterator[None]:
    """Track active model/sandbox work in the process that owns the resource."""
    platform_metrics._validate_resource_value(resource, 0)
    gauge = platform_metrics.resource_actual_usage.labels(resource=resource)
    gauge.inc()
    try:
        yield
    finally:
        gauge.dec()


def start_metrics_endpoint(port: int = 9_100) -> None:
    """Expose process-local metrics on an internal Compose network."""
    start_http_server(port)


@dataclass(slots=True)
class TokenCostTracker:
    """Compatibility accumulator for callers that do not yet use ModelUsage rows."""

    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_cost_usd: float = 0.0
    pricing_table: dict[str, dict[str, float]] = field(
        default_factory=lambda: {
            "gemini-3.6-flash": {"prompt": 0.0001, "completion": 0.0004},
            "gemini-1.5-pro": {"prompt": 0.00125, "completion": 0.005},
            "gpt-4o": {"prompt": 0.0025, "completion": 0.010},
            "gpt-4o-mini": {"prompt": 0.00015, "completion": 0.0006},
        }
    )

    def record_usage(self, model_name: str, prompt_tokens: int, completion_tokens: int) -> float:
        if prompt_tokens < 0 or completion_tokens < 0:
            raise ValueError("token usage cannot be negative")
        self.total_prompt_tokens += prompt_tokens
        self.total_completion_tokens += completion_tokens
        rates = self.pricing_table.get(model_name, {"prompt": 0.0005, "completion": 0.0015})
        cost = (
            prompt_tokens / 1_000 * rates["prompt"]
            + completion_tokens / 1_000 * rates["completion"]
        )
        self.total_cost_usd += cost
        return cost
