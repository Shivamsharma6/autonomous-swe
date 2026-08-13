from __future__ import annotations

import pytest

from observability.slo import INITIAL_SLOS, burn_rate


def test_initial_slos_cover_every_approved_reliability_target() -> None:
    assert set(INITIAL_SLOS) == {
        "api_availability",
        "task_dispatch_latency",
        "checkpoint_durability",
        "event_delivery_latency",
        "approval_notification_latency",
        "cancellation_propagation",
        "artifact_integrity",
        "worker_failure_recovery",
    }
    assert all(slo.target > 0 for slo in INITIAL_SLOS.values())
    assert all(slo.window_days >= 1 for slo in INITIAL_SLOS.values())
    assert all(slo.eligibility.strip() for slo in INITIAL_SLOS.values())


def test_burn_rate_compares_observed_bad_fraction_to_error_budget() -> None:
    assert burn_rate(observed_bad_fraction=0.001, objective=0.999) == pytest.approx(1.0)
    assert burn_rate(observed_bad_fraction=0.01, objective=0.999) == pytest.approx(10.0)


@pytest.mark.parametrize("observed,objective", [(-0.1, 0.99), (1.1, 0.99), (0.1, 1.0)])
def test_burn_rate_rejects_invalid_inputs(observed: float, objective: float) -> None:
    with pytest.raises(ValueError):
        burn_rate(observed_bad_fraction=observed, objective=objective)
