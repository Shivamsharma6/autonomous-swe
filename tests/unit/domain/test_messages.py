from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

import domain.messages as messages
from domain.messages import (
    AgentMessage,
    Blocker,
    ContextHandoff,
    MessageKind,
    PatchProposal,
    ResearchEvidence,
    ReviewFinding,
    TaskCompletion,
    ValidationResult,
)

MESSAGE_ADAPTER = TypeAdapter(AgentMessage)


def envelope() -> dict[str, Any]:
    return {
        "message_id": uuid4(),
        "schema_version": "1.0",
        "sender": "researcher",
        "recipient": "coder",
        "run_id": uuid4(),
        "task_id": uuid4(),
        "attempt_id": uuid4(),
        "created_at": datetime.now(UTC),
        "causation_id": uuid4(),
        "correlation_id": uuid4(),
        "artifact_ids": [uuid4()],
    }


@pytest.mark.parametrize(
    ("message_type", "payload"),
    [
        (ContextHandoff, {"summary": "Relevant files identified", "context_ids": [uuid4()]}),
        (
            ResearchEvidence,
            {
                "summary": "The endpoint uses bearer sessions",
                "findings": ["Session validation lives in src/auth.py"],
                "sources": ["src/auth.py:42"],
            },
        ),
        (
            PatchProposal,
            {
                "summary": "Add bounded session validation",
                "patch_artifact_id": uuid4(),
                "changed_paths": ["src/session.py"],
            },
        ),
        (
            messages.TestEvidence,
            {
                "passed": True,
                "commands": [["python", "-m", "pytest", "tests/test_session.py"]],
                "failures": [],
            },
        ),
        (
            ReviewFinding,
            {
                "severity": "LOW",
                "summary": "No blocking findings",
                "location": "src/session.py:1",
            },
        ),
        (
            ValidationResult,
            {"valid": True, "summary": "All acceptance checks passed", "checks": ["tests"]},
        ),
        (
            Blocker,
            {
                "code": "UAMS_UNAVAILABLE",
                "summary": "External memory is temporarily unavailable",
                "retryable": True,
            },
        ),
        (
            TaskCompletion,
            {"outcome": "COMPLETED", "summary": "Task evidence is complete"},
        ),
    ],
)
def test_message_union_round_trips_with_typed_envelope(
    message_type: type[Any], payload: dict[str, Any]
) -> None:
    message = message_type(**envelope(), **payload)
    restored = MESSAGE_ADAPTER.validate_json(message.model_dump_json())

    assert isinstance(restored, message_type)
    assert restored.kind in MessageKind
    assert restored.content_hash == message.content_hash
    assert len(restored.content_hash) == 64
    assert isinstance(restored.message_id, UUID)
    assert restored.created_at.tzinfo is not None
    assert restored.artifact_ids


def test_message_hash_detects_payload_tampering() -> None:
    message = ContextHandoff(**envelope(), summary="Original handoff", context_ids=[uuid4()])
    tampered = message.model_dump()
    tampered["summary"] = "Tampered handoff"

    with pytest.raises(ValidationError, match="content_hash"):
        ContextHandoff.model_validate(tampered)


def test_message_discriminator_rejects_unknown_kind() -> None:
    payload = ContextHandoff(
        **envelope(), summary="Original handoff", context_ids=[uuid4()]
    ).model_dump(mode="json")
    payload["kind"] = "untyped_chat"

    with pytest.raises(ValidationError, match="union_tag_invalid"):
        MESSAGE_ADAPTER.validate_python(payload)


def test_message_ids_are_immutable() -> None:
    message = ContextHandoff(**envelope(), summary="Original handoff", context_ids=[uuid4()])

    with pytest.raises(ValidationError, match="frozen"):
        message.message_id = uuid4()
