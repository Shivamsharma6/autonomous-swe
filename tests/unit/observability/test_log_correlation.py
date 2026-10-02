"""Correlation identifiers must reach the logs the operator actually reads.

The platform computes a rich `CorrelationContext` and forwards it on every
outbound HTTP call, but structlog reads its own contextvars and nothing ever
called `bind_contextvars`. Every emitted line therefore carried only the event
name plus whatever the call site passed explicitly, so the debugging procedure
in `docs/task-failure-investigation.md` could not identify the run it described.
OTel does not close this gap: the exporter endpoint is empty by default.
"""

from __future__ import annotations

import io
import json
from uuid import UUID

import structlog

from observability.tracing import (
    CorrelationContext,
    bind_correlation,
    current_correlation,
    reset_correlation,
)

RUN_ID = "11111111-1111-1111-1111-111111111111"
TASK_ID = "22222222-2222-2222-2222-222222222222"


def capture(action) -> list[dict]:
    buffer = io.StringIO()
    structlog.configure(
        processors=(
            structlog.contextvars.merge_contextvars,
            structlog.processors.JSONRenderer(sort_keys=True),
        ),
        logger_factory=structlog.PrintLoggerFactory(file=buffer),
    )
    action(structlog.get_logger("test"))
    return [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]


def test_bound_identifiers_appear_on_log_lines() -> None:
    def emit(log) -> None:
        token = bind_correlation(
            CorrelationContext(trace_id="run:abc:task:def", run_id=RUN_ID, task_id=TASK_ID)
        )
        try:
            log.info("run_planning_failed", error_type="ValidationError")
        finally:
            reset_correlation(token)

    (line,) = capture(emit)

    assert line["run_id"] == RUN_ID
    assert line["task_id"] == TASK_ID
    assert line["trace_id"] == "run:abc:task:def"
    assert line["error_type"] == "ValidationError"


def test_an_unbound_logger_emits_no_inherited_identifiers() -> None:
    """A previous scope must not leak into the next unit of work."""
    token = bind_correlation(CorrelationContext(run_id=RUN_ID, task_id=TASK_ID))
    reset_correlation(token)

    (line,) = capture(lambda log: log.info("next_scope"))

    assert "run_id" not in line
    assert "task_id" not in line


def test_the_graph_thread_is_not_logged_but_still_propagates() -> None:
    """The thread id is long and derivable; it stays available for propagation."""
    context = CorrelationContext(run_id=RUN_ID, graph_thread_id="run:x:task:y:attempt:z")

    def emit(log) -> None:
        token = bind_correlation(context)
        try:
            log.info("inside")
        finally:
            reset_correlation(token)

    (line,) = capture(emit)

    assert "graph_thread_id" not in line
    assert "x-autoswe-graph-thread-id" in context.to_headers()


def test_nested_scopes_restore_the_outer_context() -> None:
    outer = bind_correlation(CorrelationContext(run_id=RUN_ID))
    inner = bind_correlation(CorrelationContext(run_id=TASK_ID))
    observed_inner = current_correlation().run_id
    reset_correlation(inner)
    observed_outer = current_correlation().run_id
    reset_correlation(outer)

    assert observed_inner == UUID(TASK_ID)
    assert observed_outer == UUID(RUN_ID)
    assert current_correlation().run_id is None


def test_absent_identifiers_are_not_emitted_as_null() -> None:
    def emit(log) -> None:
        token = bind_correlation(CorrelationContext(run_id=RUN_ID))
        try:
            log.info("sparse")
        finally:
            reset_correlation(token)

    (line,) = capture(emit)

    assert line["run_id"] == RUN_ID
    assert "task_id" not in line
    assert "graph_thread_id" not in line
