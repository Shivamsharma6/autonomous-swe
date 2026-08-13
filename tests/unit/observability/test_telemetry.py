from __future__ import annotations

from uuid import uuid4

from observability.logging import redact
from observability.tracing import CorrelationContext


def test_correlation_context_round_trips_all_execution_boundaries() -> None:
    correlation = CorrelationContext(
        request_id="request-1",
        trace_id="trace-1",
        run_id=uuid4(),
        task_id=uuid4(),
        graph_thread_id="thread-1",
        message_id=uuid4(),
        model_request_id="model-1",
        tool_call_id="tool-1",
        sandbox_execution_id=uuid4(),
        artifact_id=uuid4(),
        uams_memory_id=uuid4(),
    )

    restored = CorrelationContext.from_headers(correlation.to_headers())

    assert restored == correlation
    assert restored.span_attributes()["autoswe.graph.thread_id"] == "thread-1"


def test_recursive_redaction_handles_keys_and_embedded_credentials() -> None:
    value = {
        "authorization": "Bearer abc123",
        "nested": {
            "uams_token": "top-secret",
            "message": "request failed token=xyz password=hunter2",
        },
        "safe": "visible",
    }

    cleaned = redact(value)

    assert cleaned["authorization"] == "[REDACTED]"
    assert cleaned["nested"]["uams_token"] == "[REDACTED]"  # noqa: S105
    assert "xyz" not in cleaned["nested"]["message"]
    assert "hunter2" not in cleaned["nested"]["message"]
    assert cleaned["safe"] == "visible"
