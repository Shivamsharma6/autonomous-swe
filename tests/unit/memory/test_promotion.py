from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from domain.models import MemoryCandidate
from knowledge.memory.promotion import (
    PromotionGate,
    PromotionReview,
    deterministic_memory_id,
)


def candidate(**overrides: object) -> MemoryCandidate:
    now = datetime.now(UTC)
    values: dict[str, object] = {
        "candidate_id": uuid4(),
        "project_id": uuid4(),
        "source_run_id": uuid4(),
        "source_task_id": uuid4(),
        "source_attempt_id": uuid4(),
        "source_agent": "reviewer",
        "classification": "procedural",
        "content": "Run targeted tests before the complete verification suite.",
        "observed_at": now,
        "verified_at": now,
        "repository_id": uuid4(),
        "baseline_commit": "a" * 40,
        "originating_message_ids": [uuid4()],
        "artifact_hashes": ["b" * 64],
        "verification_commands": [["python", "-m", "pytest", "-q"]],
        "confidence": 0.95,
    }
    values.update(overrides)
    return MemoryCandidate.model_validate(values)


def accepted_review(**overrides: object) -> PromotionReview:
    values: dict[str, object] = {
        "outcome_verified": True,
        "artifact_evidence_verified": True,
        "verification_passed": True,
        "contradiction_found": False,
        "duplicate_found": False,
        "structural_quality": 0.9,
        "evidence_quality": 0.9,
        "source_kind": "distilled",
        "sensitive_categories": [],
        "cross_project": False,
        "human_approved": False,
    }
    values.update(overrides)
    return PromotionReview.model_validate(values)


@pytest.mark.parametrize(
    ("review_overrides", "reason"),
    [
        ({"outcome_verified": False}, "outcome_not_verified"),
        ({"artifact_evidence_verified": False}, "artifact_evidence_not_verified"),
        ({"verification_passed": False}, "verification_failed"),
        ({"contradiction_found": True}, "contradiction_detected"),
        ({"duplicate_found": True}, "duplicate_detected"),
        ({"structural_quality": 0.1}, "quality_below_threshold"),
        ({"source_kind": "raw_transcript"}, "non_distilled_source"),
    ],
)
def test_gate_rejects_unverified_or_low_quality_candidates(
    review_overrides: dict[str, object], reason: str
) -> None:
    decision = PromotionGate(minimum_quality=0.75).evaluate(
        candidate(), accepted_review(**review_overrides)
    )

    assert decision.accepted is False
    assert reason in decision.reasons


def test_gate_rejects_secrets_and_pii_before_uams() -> None:
    secret = candidate(content="Use password=super-secret-value for deploy@example.com")

    decision = PromotionGate().evaluate(secret, accepted_review())

    assert decision.accepted is False
    assert "sensitive_data_detected" in decision.reasons


@pytest.mark.parametrize("category", ["identity", "preference", "security"])
def test_sensitive_and_cross_project_knowledge_requires_human_approval(category: str) -> None:
    gate = PromotionGate()

    blocked = gate.evaluate(candidate(), accepted_review(sensitive_categories=[category]))
    approved = gate.evaluate(
        candidate(),
        accepted_review(sensitive_categories=[category], human_approved=True),
    )

    assert "human_approval_required" in blocked.reasons
    assert approved.accepted is True


def test_cross_project_knowledge_requires_human_approval() -> None:
    gate = PromotionGate()

    blocked = gate.evaluate(candidate(), accepted_review(cross_project=True))
    approved = gate.evaluate(candidate(), accepted_review(cross_project=True, human_approved=True))

    assert "human_approval_required" in blocked.reasons
    assert approved.accepted is True


def test_memory_id_is_uuid5_stable_for_content_and_independent_of_source_run() -> None:
    original = candidate(content="Run   TARGETED tests.\n")
    normalized = original.model_copy(update={"content": "run targeted tests."})
    other_run = original.model_copy(update={"source_run_id": uuid4(), "source_task_id": uuid4()})
    changed = original.model_copy(update={"content": "Deploy the service."})

    assert deterministic_memory_id(original) == deterministic_memory_id(normalized)
    # Identical knowledge from different runs collapses onto one memory.
    assert deterministic_memory_id(original) == deterministic_memory_id(other_run)
    assert deterministic_memory_id(original) != deterministic_memory_id(changed)
    assert deterministic_memory_id(original).version == 5
