from __future__ import annotations

from datetime import timedelta
from random import Random
from uuid import uuid4

from messaging.consumer import DeliveryRetryPolicy, causation_chain


def test_exponential_retry_is_bounded_and_jittered() -> None:
    policy = DeliveryRetryPolicy(
        max_attempts=8,
        base_delay=timedelta(seconds=2),
        max_delay=timedelta(seconds=20),
    )
    random = Random(7)  # noqa: S311 - deterministic retry-jitter test

    delays = [policy.delay_for(attempt, random=random) for attempt in range(1, 9)]

    assert all(timedelta(0) <= delay <= timedelta(seconds=20) for delay in delays)
    assert delays[-1] < timedelta(seconds=20)
    assert len(set(delays)) > 1


def test_causation_chain_preserves_valid_ids_and_event_id() -> None:
    parent, event_id = uuid4(), uuid4()

    assert causation_chain(
        event_id,
        {"causation_chain": [str(parent), "not-a-uuid", str(parent)]},
    ) == (parent, event_id)
