from __future__ import annotations

import os
import time
from contextvars import ContextVar, Token
from typing import Any, ClassVar, Self
from uuid import UUID

from pydantic import BaseModel, SecretStr


class CorrelationContext(BaseModel):
    request_id: str | None = None
    trace_id: str | None = None
    run_id: UUID | None = None
    task_id: UUID | None = None
    graph_thread_id: str | None = None
    message_id: UUID | None = None
    model_request_id: str | None = None
    tool_call_id: str | None = None
    sandbox_execution_id: UUID | None = None
    artifact_id: UUID | None = None
    uams_memory_id: UUID | None = None

    _HEADERS: ClassVar[dict[str, str]] = {
        "request_id": "x-autoswe-request-id",
        "trace_id": "x-autoswe-trace-id",
        "run_id": "x-autoswe-run-id",
        "task_id": "x-autoswe-task-id",
        "graph_thread_id": "x-autoswe-graph-thread-id",
        "message_id": "x-autoswe-message-id",
        "model_request_id": "x-autoswe-model-request-id",
        "tool_call_id": "x-autoswe-tool-call-id",
        "sandbox_execution_id": "x-autoswe-sandbox-id",
        "artifact_id": "x-autoswe-artifact-id",
        "uams_memory_id": "x-autoswe-uams-memory-id",
    }

    def to_headers(self) -> dict[str, str]:
        values = self.model_dump(exclude_none=True, mode="json")
        return {self._HEADERS[key]: str(value) for key, value in values.items()}

    @classmethod
    def from_headers(cls, headers: dict[str, str]) -> Self:
        lowered = {key.casefold(): value for key, value in headers.items()}
        values = {
            field: lowered[header]
            for field, header in cls._HEADERS.items()
            if header in lowered
        }
        return cls.model_validate(values)

    def span_attributes(self) -> dict[str, str]:
        prefixes = {
            "request_id": "autoswe.request.id",
            "trace_id": "autoswe.trace.id",
            "run_id": "autoswe.run.id",
            "task_id": "autoswe.task.id",
            "graph_thread_id": "autoswe.graph.thread_id",
            "message_id": "autoswe.message.id",
            "model_request_id": "autoswe.model.request_id",
            "tool_call_id": "autoswe.tool.call_id",
            "sandbox_execution_id": "autoswe.sandbox.execution_id",
            "artifact_id": "autoswe.artifact.id",
            "uams_memory_id": "autoswe.uams.memory_id",
        }
        values = self.model_dump(exclude_none=True, mode="json")
        return {prefixes[key]: str(value) for key, value in values.items()}


_correlation: ContextVar[CorrelationContext | None] = ContextVar(
    "autoswe_correlation", default=None
)


def bind_correlation(context: CorrelationContext) -> Token[CorrelationContext | None]:
    return _correlation.set(context)


def current_correlation() -> CorrelationContext:
    return _correlation.get() or CorrelationContext()


def reset_correlation(token: Token[CorrelationContext | None]) -> None:
    _correlation.reset(token)


class ObservabilityConfig(BaseModel):
    tracing_enabled: bool = False
    project_name: str = "autonomous-swe-platform"
    langchain_api_key: SecretStr | None = None


class LangSmithTracer:
    """Optional compatibility metadata; OpenTelemetry remains the primary trace path."""

    def __init__(self, project_name: str = "autonomous-swe-platform") -> None:
        self.project_name = project_name
        self._api_key = os.environ.get("LANGSMITH_API_KEY") or os.environ.get(
            "LANGCHAIN_API_KEY"
        )
        enabled = os.environ.get("LANGSMITH_TRACING") or os.environ.get(
            "LANGCHAIN_TRACING_V2"
        )
        self.is_enabled = str(enabled).casefold() == "true" and bool(self._api_key)

    def get_run_metadata(
        self, task_id: str, agent_role: str, model_name: str
    ) -> dict[str, Any]:
        return {
            "langsmith_project": self.project_name,
            "tracing_active": self.is_enabled,
            "task_id": task_id,
            "agent_role": agent_role,
            "model_name": model_name,
            "timestamp": time.time(),
        }


def setup_observability() -> ObservabilityConfig:
    raw_key = os.environ.get("LANGSMITH_API_KEY") or os.environ.get("LANGCHAIN_API_KEY")
    enabled = os.environ.get("LANGSMITH_TRACING") or os.environ.get("LANGCHAIN_TRACING_V2")
    return ObservabilityConfig(
        tracing_enabled=str(enabled).casefold() == "true" and bool(raw_key),
        project_name=os.environ.get("LANGSMITH_PROJECT")
        or os.environ.get("LANGCHAIN_PROJECT", "autonomous-swe-platform"),
        langchain_api_key=SecretStr(raw_key) if raw_key else None,
    )


def configure_telemetry(
    *,
    service_name: str,
    endpoint: str,
    application: Any | None = None,
    sqlalchemy_engine: Any | None = None,
) -> None:
    """Configure OTLP tracing and supported library instrumentation once per process."""
    if not endpoint.strip():
        return
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.redis import RedisInstrumentor
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(
                endpoint=endpoint,
                insecure=endpoint.startswith("http://"),
            )
        )
    )
    trace.set_tracer_provider(provider)
    HTTPXClientInstrumentor().instrument()
    RedisInstrumentor().instrument()
    if sqlalchemy_engine is not None:
        SQLAlchemyInstrumentor().instrument(engine=sqlalchemy_engine.sync_engine)
    if application is not None:
        FastAPIInstrumentor.instrument_app(application)
