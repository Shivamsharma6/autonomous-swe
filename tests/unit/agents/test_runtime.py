from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from pydantic import Field

from agents.base import (
    AgentBudgetExceeded,
    AgentConfigurationError,
    AgentInvocation,
    AgentRuntime,
    InMemoryUsageRecorder,
    StructuredOutputExhausted,
)
from agents.gateway import (
    FailureClass,
    GatewayError,
    ModelResponse,
    ModelUsage,
    ToolCall,
    ToolDefinition,
)
from agents.scripted import ScriptedGateway, ScriptedResponse
from agents.specs import AgentRole, build_agent_specs, instantiate_required_agents
from domain.enums import RiskLevel
from domain.models import AgentSpec, ContractModel
from knowledge.memory.fake import FakeMemoryPort
from knowledge.memory.port import RetrievedMemory


class VerifiedResult(ContractModel):
    schema_version: str = "1.0"
    answer: str = Field(min_length=1)


def spec(**overrides: object) -> AgentSpec:
    values: dict[str, object] = {
        "role": "validator",
        "purpose": "Return a verified structured result.",
        "input_schema": "AgentInvocation@1.0",
        "output_schema": "VerifiedResult@1.0",
        "primary_model": "primary-model",
        "fallback_models": ["fallback-model"],
        "tool_grants": ["read_file"],
        "maximum_risk": RiskLevel.LOW,
        "memory_policy": "none",
        "token_budget": 1_000,
        "cost_budget_usd": 1.0,
        "turn_budget": 4,
        "wall_time_seconds": 30,
        "sandbox_profile": "none",
        "network_profile": "none",
        "retry_policy": "one-schema-repair",
        "escalation_policy": "fail-visible",
        "termination_policy": "valid-output-or-budget",
    }
    values.update(overrides)
    return AgentSpec.model_validate(values)


def invocation() -> AgentInvocation:
    return AgentInvocation(
        trace_id=f"trace-{uuid4()}",
        run_id=uuid4(),
        task_id=uuid4(),
        attempt_id=uuid4(),
        project_id=uuid4(),
        repository_id=uuid4(),
        baseline_commit="a" * 40,
        goal="Validate the implementation result.",
        input_payload={"acceptance": ["result is typed"]},
        memory_budget_tokens=500,
    )


def response(
    output: dict[str, Any] | None = None,
    *,
    tool_calls: tuple[ToolCall, ...] = (),
    input_tokens: int = 10,
    output_tokens: int = 5,
    cost: float = 0.01,
) -> ModelResponse:
    return ModelResponse(
        trace_id="script-overridden-by-request",
        provider_request_id=str(uuid4()),
        model="primary-model",
        structured_output=output,
        raw_text=None,
        tool_calls=tool_calls,
        finish_reason="tool_calls" if tool_calls else "stop",
        usage=ModelUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
        ),
    )


@pytest.mark.asyncio
async def test_bounded_schema_repair_records_invalid_and_valid_attempts() -> None:
    gateway = ScriptedGateway(
        responses=(
            ScriptedResponse(response({"schema_version": "1.0", "answer": ""})),
            ScriptedResponse(response({"schema_version": "1.0", "answer": "verified"})),
        )
    )
    recorder = InMemoryUsageRecorder()
    runtime = AgentRuntime(
        spec(),
        gateway,
        input_type=AgentInvocation,
        output_type=VerifiedResult,
        usage_recorder=recorder,
        max_schema_repairs=1,
    )

    result = await runtime.run(invocation())

    assert result.output.answer == "verified"
    assert len(result.attempts) == 2
    assert result.attempts[0].validation_errors
    assert not result.attempts[1].validation_errors
    assert len(recorder.records) == 2
    assert "schema repair" in gateway.requests[1].messages[-1].content.casefold()
    assert result.usage.total_tokens == 30
    assert result.usage.cost_usd == pytest.approx(0.02)


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_transient_primary_failure_retries_same_model_then_fallback(monkeypatch) -> None:
    _instant_sleep(monkeypatch)
    gateway = ScriptedGateway(
        responses=(
            ScriptedResponse(
                error=GatewayError("primary overloaded", failure_class=FailureClass.TRANSIENT)
            ),
            ScriptedResponse(
                error=GatewayError("primary overloaded", failure_class=FailureClass.TRANSIENT)
            ),
            ScriptedResponse(
                error=GatewayError("primary overloaded", failure_class=FailureClass.TIMEOUT)
            ),
            ScriptedResponse(
                error=GatewayError("primary overloaded", failure_class=FailureClass.TRANSIENT)
            ),
            ScriptedResponse(response({"schema_version": "1.0", "answer": "fallback"})),
        )
    )
    runtime = AgentRuntime(
        spec(),
        gateway,
        input_type=AgentInvocation,
        output_type=VerifiedResult,
    )

    result = await runtime.run(invocation())

    assert [item.model for item in gateway.requests] == [
        "primary-model",
        "primary-model",
        "primary-model",
        "primary-model",
        "fallback-model",
    ]
    assert [item.failure_class for item in result.attempts[:4]] == [
        FailureClass.TRANSIENT,
        FailureClass.TRANSIENT,
        FailureClass.TIMEOUT,
        FailureClass.TRANSIENT,
    ]
    assert result.attempts[-1].model == "fallback-model"
    assert result.output.answer == "fallback"


@pytest.mark.asyncio
async def test_permanent_failure_falls_back_without_retries(monkeypatch) -> None:
    _instant_sleep(monkeypatch)
    gateway = ScriptedGateway(
        responses=(
            ScriptedResponse(
                error=GatewayError("bad request shape", failure_class=FailureClass.PERMANENT)
            ),
            ScriptedResponse(response({"schema_version": "1.0", "answer": "fallback"})),
        )
    )
    runtime = AgentRuntime(
        spec(),
        gateway,
        input_type=AgentInvocation,
        output_type=VerifiedResult,
    )

    result = await runtime.run(invocation())

    assert [item.model for item in gateway.requests] == ["primary-model", "fallback-model"]
    assert result.attempts[0].failure_class is FailureClass.PERMANENT


def _instant_sleep(monkeypatch) -> None:
    async def _no_delay(_: float) -> None:
        return None

    monkeypatch.setattr("agents.base.asyncio.sleep", _no_delay)


@pytest.mark.asyncio
async def test_schema_repair_exhaustion_fails_without_substitute_output() -> None:
    gateway = ScriptedGateway(
        responses=(
            ScriptedResponse(response({"answer": ""})),
            ScriptedResponse(response({"answer": ""})),
        )
    )
    recorder = InMemoryUsageRecorder()
    runtime = AgentRuntime(
        spec(),
        gateway,
        input_type=AgentInvocation,
        output_type=VerifiedResult,
        usage_recorder=recorder,
        max_schema_repairs=1,
    )

    with pytest.raises(StructuredOutputExhausted) as raised:
        await runtime.run(invocation())

    assert len(raised.value.attempts) == 2
    assert len(recorder.records) == 2
    assert not hasattr(raised.value, "substitute_output")


@pytest.mark.asyncio
async def test_malformed_json_is_repaired_without_markdown_or_regex_parsing() -> None:
    malformed = response()
    malformed = malformed.model_copy(
        update={"raw_text": "```json\n{not valid}\n```", "structured_output": None}
    )
    gateway = ScriptedGateway(
        responses=(
            ScriptedResponse(malformed),
            ScriptedResponse(response({"schema_version": "1.0", "answer": "valid"})),
        )
    )
    runtime = AgentRuntime(
        spec(),
        gateway,
        input_type=AgentInvocation,
        output_type=VerifiedResult,
        max_schema_repairs=1,
    )

    result = await runtime.run(invocation())

    assert result.output.answer == "valid"
    assert len(result.attempts) == 2


@pytest.mark.asyncio
async def test_memory_policy_never_silently_runs_without_memory_port() -> None:
    gateway = ScriptedGateway(
        responses=(ScriptedResponse(response({"schema_version": "1.0", "answer": "invalid path"})),)
    )
    runtime = AgentRuntime(
        spec(memory_policy="project-and-procedure"),
        gateway,
        input_type=AgentInvocation,
        output_type=VerifiedResult,
    )

    with pytest.raises(AgentConfigurationError, match="no fallback"):
        await runtime.run(invocation())
    assert gateway.requests == []


@pytest.mark.asyncio
async def test_usage_budget_is_enforced_after_recording_actual_usage() -> None:
    gateway = ScriptedGateway(
        responses=(
            ScriptedResponse(
                response(
                    {"schema_version": "1.0", "answer": "too expensive"},
                    input_tokens=700,
                    output_tokens=400,
                )
            ),
        )
    )
    recorder = InMemoryUsageRecorder()
    runtime = AgentRuntime(
        spec(token_budget=1_000),
        gateway,
        input_type=AgentInvocation,
        output_type=VerifiedResult,
        usage_recorder=recorder,
    )

    with pytest.raises(AgentBudgetExceeded, match="token budget"):
        await runtime.run(invocation())
    assert recorder.records[0].usage.total_tokens == 1_100


class RecordingToolDispatcher:
    def __init__(self) -> None:
        self.calls: list[ToolCall] = []

    async def dispatch(self, call: ToolCall, *, invocation: AgentInvocation) -> dict[str, Any]:
        self.calls.append(call)
        return {"path": call.arguments["path"], "content": "verified source"}


@pytest.mark.asyncio
async def test_native_tool_call_is_grant_checked_dispatched_and_correlated() -> None:
    tool_call = ToolCall(
        call_id="call-read-1",
        name="read_file",
        arguments={"path": "src/app.py"},
    )
    gateway = ScriptedGateway(
        responses=(
            ScriptedResponse(response(tool_calls=(tool_call,))),
            ScriptedResponse(response({"schema_version": "1.0", "answer": "verified"})),
        )
    )
    dispatcher = RecordingToolDispatcher()
    runtime = AgentRuntime(
        spec(),
        gateway,
        input_type=AgentInvocation,
        output_type=VerifiedResult,
        tool_definitions=(
            ToolDefinition(
                name="read_file",
                description="Read repository text.",
                input_schema={"type": "object"},
            ),
        ),
        tool_dispatcher=dispatcher,
    )

    result = await runtime.run(invocation())

    assert dispatcher.calls == [tool_call]
    assert result.tool_calls == (tool_call,)
    tool_message = gateway.requests[1].messages[-1]
    assert tool_message.role == "tool"
    assert tool_message.tool_call_id == "call-read-1"


@pytest.mark.asyncio
async def test_runtime_gathers_bounded_uams_context_with_provenance() -> None:
    call = invocation()
    memory = RetrievedMemory(
        memory_id=uuid4(),
        revision_id=str(uuid4()),
        text="Use the verified repository convention.",
        score=1,
        memory_type="procedural",
        source_id="Tasks/convention.md",
        evidence_ids=("evidence-1",),
        project_id=call.project_id,
        repository_id=call.repository_id,
        baseline_commit=call.baseline_commit,
        verified_at=datetime.now(UTC),
    )
    gateway = ScriptedGateway(
        responses=(ScriptedResponse(response({"schema_version": "1.0", "answer": "verified"})),)
    )
    runtime = AgentRuntime(
        spec(memory_policy="project-and-procedure"),
        gateway,
        input_type=AgentInvocation,
        output_type=VerifiedResult,
        memory=FakeMemoryPort(seed=(memory,)),
    )

    result = await runtime.run(call)

    assert result.context_memory_ids == (memory.memory_id,)
    prompt = gateway.requests[0].messages[-1].content
    assert str(memory.memory_id) in prompt
    assert memory.revision_id in prompt
    assert len(prompt) < 4_000


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("role", "reviewer"),
        ("purpose", "Review a structured result."),
        ("input_schema", "OtherInput@2.0"),
        ("output_schema", "OtherOutput@2.0"),
        ("primary_model", "other-primary"),
        ("fallback_models", ("other-fallback",)),
        ("tool_grants", ("run_tests",)),
        ("maximum_risk", RiskLevel.MEDIUM),
        ("memory_policy", "project-and-procedure"),
        ("token_budget", 999),
        ("cost_budget_usd", 0.5),
        ("turn_budget", 3),
        ("wall_time_seconds", 29),
        ("sandbox_profile", "python"),
        ("network_profile", "allowlist"),
        ("retry_policy", "no-retry"),
        ("escalation_policy", "human"),
        ("termination_policy", "first-valid"),
    ],
)
def test_agent_spec_hash_covers_every_execution_policy(field: str, changed: object) -> None:
    original = spec()

    assert original.model_copy(update={field: changed}).spec_hash != original.spec_hash


def test_only_required_declarative_roles_are_instantiated() -> None:
    specs = build_agent_specs(
        primary_model="primary-model",
        fallback_models=("fallback-model",),
    )
    created: list[AgentRole] = []

    def factory(role: AgentRole, _: AgentSpec) -> str:
        created.append(role)
        return role.value

    result = instantiate_required_agents(
        (AgentRole.RESEARCHER, AgentRole.VALIDATION),
        specs,
        factory,
    )

    assert created == [AgentRole.RESEARCHER, AgentRole.VALIDATION]
    assert result == {
        AgentRole.RESEARCHER: "researcher",
        AgentRole.VALIDATION: "validation",
    }
