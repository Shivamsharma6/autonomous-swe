from __future__ import annotations

from prometheus_client import CollectorRegistry, generate_latest

from observability.metrics import PlatformMetrics


def test_exact_time_in_state_metrics_use_only_bounded_labels() -> None:
    registry = CollectorRegistry()
    metrics = PlatformMetrics(registry=registry)

    metrics.observe_state_duration("task", "RUNNING", 18.0)
    metrics.observe_state_duration("workflow", "WAITING_FOR_APPROVAL", 47.0)
    metrics.observe_state_duration("approval", "PENDING", 11.0)
    metrics.observe_state_duration("uams", "WAITING", 4.0)

    payload = generate_latest(registry).decode()
    for metric_name in (
        "task_state_duration_seconds",
        "workflow_state_duration_seconds",
        "approval_wait_duration_seconds",
        "uams_wait_duration_seconds",
    ):
        assert f"# HELP {metric_name}" in payload
    assert 'state="RUNNING"' in payload
    for forbidden in ("run_id=", "task_id=", "project_id="):
        assert forbidden not in payload


def test_platform_metrics_cover_dispatch_delivery_and_resource_usage() -> None:
    registry = CollectorRegistry()
    metrics = PlatformMetrics(registry=registry)

    metrics.set_queue_depth("ready", 3)
    metrics.set_resource_usage("task", reserved=3, actual=2)
    metrics.set_slo_burn_rate("task_dispatch_latency", "1h", 0.4)
    metrics.observe_dispatch(0.5, outcome="dispatched")
    metrics.observe_event_delivery(0.25, outcome="delivered")
    metrics.observe_sandbox_usage(
        cpu_seconds=1.0,
        peak_memory_bytes=1_024,
        duration_seconds=2.0,
        exit_reason="completed",
    )

    payload = generate_latest(registry).decode()
    assert "autoswe_task_queue_depth" in payload
    assert 'autoswe_resource_reservations{resource="task"} 3.0' in payload
    assert 'autoswe_resource_actual_usage{resource="task"} 2.0' in payload
    assert 'slo="task_dispatch_latency",window="1h"' in payload
    assert "autoswe_task_dispatch_latency_seconds" in payload
    assert "autoswe_event_delivery_latency_seconds" in payload
    assert "autoswe_sandbox_peak_memory_bytes" in payload
    assert 'exit_reason="completed"' in payload
