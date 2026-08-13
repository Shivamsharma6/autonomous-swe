from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, Field, ValidationError

from agents.gateway import (
    FailureClass,
    GatewayError,
    ModelGateway,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    ToolCall,
    ToolDefinition,
)
from domain.models import AgentSpec, CommitSha, ContractModel
from knowledge.memory.port import ContextRequest, MemoryPort


class AgentInvocation(ContractModel):
    schema_version: str = "1.0"
    trace_id: str = Field(min_length=1, max_length=500)
    run_id: UUID
    task_id: UUID
    attempt_id: UUID
    project_id: UUID
    repository_id: UUID
    baseline_commit: CommitSha
    goal: str = Field(min_length=1, max_length=20_000)
    input_payload: dict[str, Any]
    memory_budget_tokens: int = Field(default=2_000, ge=0, le=100_000)
    entities: tuple[str, ...] = ()


class AgentAttemptRecord(ContractModel):
    run_id: UUID
    task_id: UUID
    attempt_id: UUID
    agent_spec_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    turn: int = Field(ge=1)
    model: str
    trace_id: str
    provider_request_id: str | None = None
    usage: ModelUsage
    validation_errors: tuple[str, ...] = ()
    failure_class: FailureClass | None = None
    tool_call_ids: tuple[str, ...] = ()


class UsageRecorder(Protocol):
    async def record(self, attempt: AgentAttemptRecord) -> None: ...


class InMemoryUsageRecorder:
    def __init__(self) -> None:
        self.records: list[AgentAttemptRecord] = []

    async def record(self, attempt: AgentAttemptRecord) -> None:
        self.records.append(attempt)


class ToolDispatcher(Protocol):
    async def dispatch(self, call: ToolCall, *, invocation: AgentInvocation) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class AgentRunResult[OutputT: BaseModel]:
    output: OutputT
    trace_id: str
    attempts: tuple[AgentAttemptRecord, ...]
    usage: ModelUsage
    tool_calls: tuple[ToolCall, ...]
    context_memory_ids: tuple[UUID, ...]
    agent_spec_hash: str


class AgentRuntimeError(RuntimeError):
    pass


class StructuredOutputExhausted(AgentRuntimeError):
    def __init__(self, attempts: tuple[AgentAttemptRecord, ...]) -> None:
        super().__init__("structured output validation exhausted its repair budget")
        self.attempts = attempts


class AgentBudgetExceeded(AgentRuntimeError):
    pass


class AgentConfigurationError(AgentRuntimeError):
    pass


class AgentRuntime[OutputT: BaseModel]:
    def __init__(
        self,
        spec: AgentSpec,
        gateway: ModelGateway,
        *,
        input_type: type[BaseModel],
        output_type: type[OutputT],
        memory: MemoryPort | None = None,
        tool_definitions: tuple[ToolDefinition, ...] = (),
        tool_dispatcher: ToolDispatcher | None = None,
        usage_recorder: UsageRecorder | None = None,
        max_schema_repairs: int = 1,
    ) -> None:
        if max_schema_repairs < 0:
            raise ValueError("max_schema_repairs cannot be negative")
        self.spec = spec
        self._gateway = gateway
        self._input_type = input_type
        self._output_type = output_type
        self._memory = memory
        self._tool_definitions = tool_definitions
        self._tool_dispatcher = tool_dispatcher
        self._usage_recorder = usage_recorder or InMemoryUsageRecorder()
        self._max_schema_repairs = max_schema_repairs
        self._validate_configuration()

    async def run(
        self,
        invocation: AgentInvocation,
        *,
        cancel: asyncio.Event | None = None,
    ) -> AgentRunResult[OutputT]:
        validated = self._input_type.model_validate(invocation.model_dump())
        if not isinstance(validated, AgentInvocation):
            raise AgentConfigurationError("input_type must produce AgentInvocation")
        try:
            async with asyncio.timeout(self.spec.wall_time_seconds):
                return await self._run(validated, cancel=cancel)
        except TimeoutError as error:
            raise GatewayError(
                "agent wall-time budget exhausted", failure_class=FailureClass.TIMEOUT
            ) from error

    async def _run(
        self, invocation: AgentInvocation, *, cancel: asyncio.Event | None
    ) -> AgentRunResult[OutputT]:
        context = ""
        memory_ids: tuple[UUID, ...] = ()
        if self.spec.memory_policy.casefold() != "none":
            if self._memory is None:
                raise AgentConfigurationError(
                    "memory policy requires an external MemoryPort; no fallback is allowed"
                )
            recalled = await self._memory.get_context(
                ContextRequest(
                    task=invocation.goal,
                    project_id=invocation.project_id,
                    budget_tokens=max(1, invocation.memory_budget_tokens),
                    entities=invocation.entities,
                    repository_id=invocation.repository_id,
                    baseline_commit=invocation.baseline_commit,
                )
            )
            context = recalled.rendered
            memory_ids = tuple(item.memory_id for item in recalled.memories)

        messages = [
            ModelMessage(role="system", content=self._system_prompt()),
            ModelMessage(role="user", content=self._user_prompt(invocation, context)),
        ]
        attempts: list[AgentAttemptRecord] = []
        all_calls: list[ToolCall] = []
        total_input = total_output = total_cached = 0
        total_cost = 0.0
        schema_failures = 0
        models = (self.spec.primary_model, *self.spec.fallback_models)
        model_index = 0

        for turn in range(1, self.spec.turn_budget + 1):
            model = models[min(model_index, len(models) - 1)]
            request = ModelRequest(
                trace_id=invocation.trace_id,
                model=model,
                messages=tuple(messages),
                output_schema_name=self._output_type.__name__,
                output_schema=self._output_type.model_json_schema(),
                tools=self._tool_definitions,
                timeout_seconds=min(120.0, float(self.spec.wall_time_seconds)),
            )
            try:
                response = await self._gateway.complete(request, cancel=cancel)
            except GatewayError as error:
                attempt = AgentAttemptRecord(
                    run_id=invocation.run_id,
                    task_id=invocation.task_id,
                    attempt_id=invocation.attempt_id,
                    agent_spec_hash=self.spec.spec_hash,
                    turn=turn,
                    model=model,
                    trace_id=invocation.trace_id,
                    usage=ModelUsage(),
                    failure_class=error.failure_class,
                )
                attempts.append(attempt)
                await self._usage_recorder.record(attempt)
                if error.failure_class in {
                    FailureClass.TRANSIENT,
                    FailureClass.TIMEOUT,
                } and model_index + 1 < len(models):
                    model_index += 1
                    continue
                raise

            total_input += response.usage.input_tokens
            total_output += response.usage.output_tokens
            total_cached += response.usage.cached_input_tokens
            total_cost += response.usage.cost_usd

            if response.tool_calls:
                attempt = self._attempt(invocation, turn, response)
                attempts.append(attempt)
                await self._usage_recorder.record(attempt)
                self._check_budget(total_input + total_output, total_cost)
                await self._dispatch_tools(
                    invocation, response, messages=messages, all_calls=all_calls
                )
                continue

            try:
                output = self._validate_response(response)
            except (ValidationError, ValueError, TypeError, json.JSONDecodeError) as error:
                errors = _validation_messages(error)
                attempt = self._attempt(invocation, turn, response, validation_errors=errors)
                attempts.append(attempt)
                await self._usage_recorder.record(attempt)
                self._check_budget(total_input + total_output, total_cost)
                if schema_failures >= self._max_schema_repairs:
                    raise StructuredOutputExhausted(tuple(attempts)) from error
                schema_failures += 1
                invalid_content = response.raw_text or json.dumps(
                    response.structured_output,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                messages.append(ModelMessage(role="assistant", content=invalid_content))
                messages.append(
                    ModelMessage(
                        role="user",
                        content=(
                            "Schema repair required. Return only an object that conforms to "
                            f"{self.spec.output_schema}. Validation errors: " + json.dumps(errors)
                        ),
                    )
                )
                continue

            attempt = self._attempt(invocation, turn, response)
            attempts.append(attempt)
            await self._usage_recorder.record(attempt)
            self._check_budget(total_input + total_output, total_cost)
            return AgentRunResult(
                output=output,
                trace_id=invocation.trace_id,
                attempts=tuple(attempts),
                usage=ModelUsage(
                    input_tokens=total_input,
                    output_tokens=total_output,
                    cached_input_tokens=total_cached,
                    cost_usd=total_cost,
                ),
                tool_calls=tuple(all_calls),
                context_memory_ids=memory_ids,
                agent_spec_hash=self.spec.spec_hash,
            )
        raise AgentBudgetExceeded("agent exhausted its turn budget")

    async def _dispatch_tools(
        self,
        invocation: AgentInvocation,
        response: ModelResponse,
        *,
        messages: list[ModelMessage],
        all_calls: list[ToolCall],
    ) -> None:
        if self._tool_dispatcher is None:
            raise AgentConfigurationError("model selected a tool but no dispatcher is configured")
        messages.append(
            ModelMessage(
                role="assistant",
                content=response.raw_text or "",
                tool_calls=response.tool_calls,
            )
        )
        for call in response.tool_calls:
            if call.name not in self.spec.tool_grants:
                raise AgentConfigurationError(
                    f"tool {call.name} is not granted to {self.spec.role}"
                )
            result = await self._tool_dispatcher.dispatch(call, invocation=invocation)
            all_calls.append(call)
            messages.append(
                ModelMessage(
                    role="tool",
                    content=json.dumps(result, sort_keys=True, separators=(",", ":")),
                    tool_call_id=call.call_id,
                )
            )

    def _validate_response(self, response: ModelResponse) -> OutputT:
        if response.structured_output is not None:
            raw: Any = response.structured_output
        elif response.raw_text is not None:
            raw = json.loads(response.raw_text)
        else:
            raise ValueError("model returned neither structured output nor tool calls")
        return self._output_type.model_validate(raw)

    def _attempt(
        self,
        invocation: AgentInvocation,
        turn: int,
        response: ModelResponse,
        *,
        validation_errors: tuple[str, ...] = (),
    ) -> AgentAttemptRecord:
        return AgentAttemptRecord(
            run_id=invocation.run_id,
            task_id=invocation.task_id,
            attempt_id=invocation.attempt_id,
            agent_spec_hash=self.spec.spec_hash,
            turn=turn,
            model=response.model,
            trace_id=response.trace_id,
            provider_request_id=response.provider_request_id,
            usage=response.usage,
            validation_errors=validation_errors,
            tool_call_ids=tuple(call.call_id for call in response.tool_calls),
        )

    def _check_budget(self, tokens: int, cost: float) -> None:
        if tokens > self.spec.token_budget:
            raise AgentBudgetExceeded("agent token budget exceeded")
        if cost > self.spec.cost_budget_usd:
            raise AgentBudgetExceeded("agent cost budget exceeded")

    def _validate_configuration(self) -> None:
        if self.spec.input_schema != _schema_ref(self._input_type):
            raise AgentConfigurationError("AgentSpec input schema does not match runtime input")
        if self.spec.output_schema != _schema_ref(self._output_type):
            raise AgentConfigurationError("AgentSpec output schema does not match runtime output")
        defined = {tool.name for tool in self._tool_definitions}
        ungranted = defined.difference(self.spec.tool_grants)
        if ungranted:
            raise AgentConfigurationError(
                f"tool definitions are not granted by AgentSpec: {sorted(ungranted)}"
            )
        if self._tool_definitions and self._tool_dispatcher is None:
            raise AgentConfigurationError("tool definitions require a dispatcher")

    def _system_prompt(self) -> str:
        return (
            f"Role: {self.spec.role}\nPurpose: {self.spec.purpose}\n"
            f"Required output: {self.spec.output_schema}\n"
            f"Termination: {self.spec.termination_policy}"
        )

    def _user_prompt(self, invocation: AgentInvocation, context: str) -> str:
        sections = [
            f"Goal: {invocation.goal}",
            "Input:\n" + json.dumps(invocation.input_payload, sort_keys=True),
        ]
        if context:
            sections.append("Verified UAMS context:\n" + context)
        return "\n\n".join(sections)


def _schema_ref(model: type[BaseModel]) -> str:
    field = model.model_fields.get("schema_version")
    version = str(field.default) if field is not None and field.default else "1.0"
    return f"{model.__name__}@{version}"


def _validation_messages(error: Exception) -> tuple[str, ...]:
    if isinstance(error, ValidationError):
        return tuple(
            f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
            for item in error.errors(include_url=False)
        )
    return (str(error),)
