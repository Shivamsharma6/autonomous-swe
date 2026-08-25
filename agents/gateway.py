from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from enum import StrEnum
from typing import Any, Protocol, cast

import httpx
from pydantic import Field, computed_field, model_validator

from domain.models import ContractModel
from observability.metrics import track_actual_resource
from observability.tracing import current_correlation


class FailureClass(StrEnum):
    TRANSIENT = "TRANSIENT"
    PERMANENT = "PERMANENT"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    CAPABILITY = "CAPABILITY"
    BUDGET = "BUDGET"


class GatewayError(RuntimeError):
    def __init__(self, message: str, *, failure_class: FailureClass) -> None:
        super().__init__(message)
        self.failure_class = failure_class


class GatewayCancelled(GatewayError):
    def __init__(self, message: str = "model request cancelled") -> None:
        super().__init__(message, failure_class=FailureClass.CANCELLED)


class ProviderCapabilities(ContractModel):
    structured_outputs: bool = False
    native_tool_calls: bool = False
    streaming: bool = False
    cancellation: bool = True
    usage_accounting: bool = True

    @classmethod
    def all_supported(cls) -> ProviderCapabilities:
        return cls(
            structured_outputs=True,
            native_tool_calls=True,
            streaming=True,
            cancellation=True,
            usage_accounting=True,
        )


class ToolDefinition(ContractModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    description: str = Field(min_length=1, max_length=2_000)
    input_schema: dict[str, Any]


class ToolCall(ContractModel):
    call_id: str = Field(min_length=1, max_length=500)
    name: str = Field(min_length=1, max_length=64)
    arguments: dict[str, Any]


class ModelMessage(ContractModel):
    role: str = Field(pattern=r"^(system|user|assistant|tool)$")
    content: str
    tool_call_id: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()

    @model_validator(mode="after")
    def tool_message_has_call_id(self) -> ModelMessage:
        if self.role == "tool" and not self.tool_call_id:
            raise ValueError("tool messages require tool_call_id")
        return self


class ModelRequest(ContractModel):
    trace_id: str = Field(min_length=1, max_length=500)
    model: str = Field(min_length=1, max_length=200)
    messages: tuple[ModelMessage, ...] = Field(min_length=1, max_length=1_000)
    output_schema_name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    output_schema: dict[str, Any]
    tools: tuple[ToolDefinition, ...] = Field(default_factory=tuple, max_length=128)
    timeout_seconds: float = Field(default=60.0, gt=0, le=3_600)
    temperature: float = Field(default=0.0, ge=0, le=2)


class ModelUsage(ContractModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class ModelResponse(ContractModel):
    trace_id: str = Field(min_length=1, max_length=500)
    provider_request_id: str | None = None
    model: str = Field(min_length=1, max_length=200)
    structured_output: dict[str, Any] | None = None
    raw_text: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str
    usage: ModelUsage


class ModelStreamChunk(ContractModel):
    trace_id: str
    text: str
    finish_reason: str | None = None


class ModelGateway(Protocol):
    async def capabilities(self, model: str) -> ProviderCapabilities: ...

    async def complete(
        self, request: ModelRequest, *, cancel: asyncio.Event | None = None
    ) -> ModelResponse: ...

    def stream(
        self, request: ModelRequest, *, cancel: asyncio.Event | None = None
    ) -> AsyncIterator[ModelStreamChunk]: ...


class ModelAdmission:
    def __init__(self, max_concurrency: int) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._lock = asyncio.Lock()
        self.in_flight = 0
        self.max_observed = 0

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        async with self._semaphore:
            async with self._lock:
                self.in_flight += 1
                self.max_observed = max(self.max_observed, self.in_flight)
            try:
                yield
            finally:
                async with self._lock:
                    self.in_flight -= 1


class OpenAICompatibleGateway:
    """Typed Chat Completions adapter for OpenAI-compatible providers."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str = "",
        max_concurrency: int,
        input_cost_per_million: float = 0.0,
        cached_input_cost_per_million: float | None = None,
        output_cost_per_million: float = 0.0,
        default_capabilities: ProviderCapabilities | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"authorization": f"Bearer {api_key}"} if api_key else {}
        self._input_cost = input_cost_per_million
        self._cached_input_cost = (
            input_cost_per_million
            if cached_input_cost_per_million is None
            else cached_input_cost_per_million
        )
        self._output_cost = output_cost_per_million
        self._default_capabilities = default_capabilities
        self._capability_cache: dict[str, ProviderCapabilities] = {}
        self._client = client or httpx.AsyncClient(base_url=self._base_url)
        self._owns_client = client is None
        self.admission = ModelAdmission(max_concurrency)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def capabilities(self, model: str) -> ProviderCapabilities:
        cached = self._capability_cache.get(model)
        if cached is not None:
            return cached
        if self._default_capabilities is not None:
            self._capability_cache[model] = self._default_capabilities
            return self._default_capabilities
        try:
            response = await self._client.get(
                f"{self._base_url}/models",
                headers=self._headers | current_correlation().to_headers(),
                timeout=5.0,
            )
        except httpx.RequestError as error:
            raise GatewayError(
                f"model capability probe failed: {error}",
                failure_class=FailureClass.TRANSIENT,
            ) from error
        self._raise_for_status(response)
        body = _object_json(response)
        data = body.get("data")
        selected: dict[str, Any] = {}
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("id") == model:
                    selected = cast(dict[str, Any], item)
                    break
        raw = selected.get("capabilities", body.get("capabilities", ()))
        names = {str(item).casefold() for item in raw} if isinstance(raw, list) else set()
        capabilities = ProviderCapabilities(
            structured_outputs=bool(
                {"structured_outputs", "structured-output", "json_schema"} & names
            ),
            native_tool_calls=bool({"tool_calling", "function_calling", "tools"} & names),
            streaming="streaming" in names,
            cancellation=True,
            usage_accounting=True,
        )
        self._capability_cache[model] = capabilities
        return capabilities

    async def complete(
        self, request: ModelRequest, *, cancel: asyncio.Event | None = None
    ) -> ModelResponse:
        capabilities = await self.capabilities(request.model)
        self._require_capabilities(request, capabilities)
        if cancel is not None and cancel.is_set():
            raise GatewayCancelled()
        async with self.admission.slot():
            with track_actual_resource("model"):
                try:
                    async with asyncio.timeout(request.timeout_seconds):
                        return await self._complete_with_cancellation(request, cancel=cancel)
                except TimeoutError as error:
                    raise GatewayError(
                        "model request timed out", failure_class=FailureClass.TIMEOUT
                    ) from error

    async def _complete_with_cancellation(
        self, request: ModelRequest, *, cancel: asyncio.Event | None
    ) -> ModelResponse:
        call = asyncio.create_task(self._post_completion(request))
        if cancel is None:
            return await call
        cancellation = asyncio.create_task(cancel.wait())
        done, _ = await asyncio.wait({call, cancellation}, return_when=asyncio.FIRST_COMPLETED)
        if cancellation in done and cancel.is_set():
            call.cancel()
            await asyncio.gather(call, return_exceptions=True)
            raise GatewayCancelled()
        cancellation.cancel()
        await asyncio.gather(cancellation, return_exceptions=True)
        return await call

    async def _post_completion(self, request: ModelRequest) -> ModelResponse:
        try:
            response = await self._client.post(
                f"{self._base_url}/chat/completions",
                headers=self._headers
                | current_correlation().to_headers()
                | {"x-trace-id": request.trace_id},
                json=self._payload(request, stream=False),
                timeout=request.timeout_seconds,
            )
        except httpx.TimeoutException as error:
            raise GatewayError(
                "model request timed out", failure_class=FailureClass.TIMEOUT
            ) from error
        except httpx.RequestError as error:
            raise GatewayError(
                f"model request failed: {error}",
                failure_class=FailureClass.TRANSIENT,
            ) from error
        self._raise_for_status(response)
        return self._parse_response(request, response)

    async def stream(
        self, request: ModelRequest, *, cancel: asyncio.Event | None = None
    ) -> AsyncIterator[ModelStreamChunk]:
        capabilities = await self.capabilities(request.model)
        self._require_capabilities(request, capabilities, streaming=True)
        async with self.admission.slot():
            with track_actual_resource("model"):
                try:
                    async with asyncio.timeout(request.timeout_seconds):
                        async with self._client.stream(
                        "POST",
                        f"{self._base_url}/chat/completions",
                        headers=self._headers
                        | current_correlation().to_headers()
                        | {"x-trace-id": request.trace_id},
                            json=self._payload(request, stream=True),
                            timeout=request.timeout_seconds,
                        ) as response:
                            self._raise_for_status(response)
                            async for line in response.aiter_lines():
                                if cancel is not None and cancel.is_set():
                                    raise GatewayCancelled()
                                if not line.startswith("data:"):
                                    continue
                                data = line[5:].strip()
                                if data == "[DONE]":
                                    return
                                body = json.loads(data)
                                choice = body.get("choices", [{}])[0]
                                delta = choice.get("delta", {})
                                yield ModelStreamChunk(
                                    trace_id=request.trace_id,
                                    text=str(delta.get("content") or ""),
                                    finish_reason=choice.get("finish_reason"),
                                )
                except TimeoutError as error:
                    raise GatewayError(
                        "model stream timed out", failure_class=FailureClass.TIMEOUT
                    ) from error
                except httpx.RequestError as error:
                    raise GatewayError(
                        f"model stream failed: {error}",
                        failure_class=FailureClass.TRANSIENT,
                    ) from error

    def _payload(self, request: ModelRequest, *, stream: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": [_message_payload(message) for message in request.messages],
            "temperature": request.temperature,
            "stream": stream,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": request.output_schema_name,
                    "strict": True,
                    "schema": strict_json_schema(request.output_schema),
                },
            },
        }
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": strict_json_schema(tool.input_schema),
                        "strict": True,
                    },
                }
                for tool in request.tools
            ]
            payload["tool_choice"] = "auto"
        return payload

    def _parse_response(self, request: ModelRequest, response: httpx.Response) -> ModelResponse:
        body = _object_json(response)
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise GatewayError(
                "model response is missing choices",
                failure_class=FailureClass.PERMANENT,
            )
        choice = cast(dict[str, Any], choices[0])
        message = choice.get("message")
        if not isinstance(message, dict):
            raise GatewayError(
                "model response is missing assistant message",
                failure_class=FailureClass.PERMANENT,
            )
        raw_text = message.get("content")
        structured: dict[str, Any] | None = None
        if isinstance(raw_text, str) and raw_text.strip():
            structured = _extract_json(raw_text)
        calls = tuple(_tool_call(value) for value in message.get("tool_calls") or ())
        raw_usage: dict[str, Any] = (
            cast(dict[str, Any], body["usage"]) if isinstance(body.get("usage"), dict) else {}
        )
        input_tokens = int(raw_usage.get("prompt_tokens", 0))
        output_tokens = int(raw_usage.get("completion_tokens", 0))
        cached_details = raw_usage.get("prompt_tokens_details")
        cached = (
            int(cached_details.get("cached_tokens", 0)) if isinstance(cached_details, dict) else 0
        )
        uncached_input = max(0, input_tokens - cached)
        cost = (
            uncached_input * self._input_cost
            + cached * self._cached_input_cost
            + output_tokens * self._output_cost
        ) / 1_000_000
        return ModelResponse(
            trace_id=request.trace_id,
            provider_request_id=response.headers.get("x-request-id")
            or (str(body["id"]) if body.get("id") else None),
            model=str(body.get("model") or request.model),
            structured_output=structured,
            raw_text=raw_text if isinstance(raw_text, str) else None,
            tool_calls=calls,
            finish_reason=str(choice.get("finish_reason") or "unknown"),
            usage=ModelUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_input_tokens=cached,
                cost_usd=cost,
            ),
        )

    def _require_capabilities(
        self,
        request: ModelRequest,
        capabilities: ProviderCapabilities,
        *,
        streaming: bool = False,
    ) -> None:
        missing: list[str] = []
        if not capabilities.structured_outputs:
            missing.append("structured_outputs")
        if request.tools and not capabilities.native_tool_calls:
            missing.append("native_tool_calls")
        if streaming and not capabilities.streaming:
            missing.append("streaming")
        if missing:
            raise GatewayError(
                f"provider lacks required capabilities: {', '.join(missing)}",
                failure_class=FailureClass.CAPABILITY,
            )

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        try:
            detail = response.text[:1_000]
        except httpx.ResponseNotRead:
            detail = "streaming response body not buffered"
        raise GatewayError(
            f"model provider returned {response.status_code}: {detail}",
            failure_class=classify_status(response.status_code),
        )


def classify_status(status_code: int) -> FailureClass:
    if status_code in {408, 409, 425, 429} or status_code >= 500:
        return FailureClass.TRANSIENT
    return FailureClass.PERMANENT


def strict_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Normalize object schemas for strict structured outputs and strict tools."""
    result: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "default":
            continue
        if isinstance(value, dict):
            result[key] = strict_json_schema(cast(dict[str, Any], value))
        elif isinstance(value, list):
            result[key] = [
                strict_json_schema(cast(dict[str, Any], item)) if isinstance(item, dict) else item
                for item in value
            ]
        else:
            result[key] = value
    if result.get("type") == "object" or isinstance(result.get("properties"), dict):
        properties = result.get("properties")
        if isinstance(properties, dict):
            result["required"] = list(properties)
        result["additionalProperties"] = False
    return result


def _extract_json(raw_text: str) -> dict[str, Any] | None:
    text = raw_text.strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE)
    if match:
        try:
            parsed = json.loads(match.group(1).strip())
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        try:
            parsed = json.loads(text[first_brace : last_brace + 1])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    return None


def _object_json(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError as error:
        raise GatewayError(
            "model provider returned invalid JSON",
            failure_class=FailureClass.PERMANENT,
        ) from error
    if not isinstance(body, dict):
        raise GatewayError(
            "model provider returned a non-object response",
            failure_class=FailureClass.PERMANENT,
        )
    return cast(dict[str, Any], body)


def _message_payload(message: ModelMessage) -> dict[str, Any]:
    payload: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.tool_call_id:
        payload["tool_call_id"] = message.tool_call_id
    if message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": call.call_id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, sort_keys=True),
                },
            }
            for call in message.tool_calls
        ]
    return payload


def _tool_call(raw: Any) -> ToolCall:
    if not isinstance(raw, dict) or not isinstance(raw.get("function"), dict):
        raise GatewayError(
            "model returned an invalid tool call",
            failure_class=FailureClass.PERMANENT,
        )
    function = cast(dict[str, Any], raw["function"])
    try:
        arguments = json.loads(str(function.get("arguments") or "{}"))
    except json.JSONDecodeError as error:
        raise GatewayError(
            "model returned invalid tool arguments",
            failure_class=FailureClass.PERMANENT,
        ) from error
    if not isinstance(arguments, dict):
        raise GatewayError(
            "tool arguments must be an object",
            failure_class=FailureClass.PERMANENT,
        )
    return ToolCall(
        call_id=str(raw.get("id") or ""),
        name=str(function.get("name") or ""),
        arguments=cast(dict[str, Any], arguments),
    )
