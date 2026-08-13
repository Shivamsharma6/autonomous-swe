from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from typing import Any
from uuid import uuid4

import httpx
import pytest

from agents.gateway import (
    FailureClass,
    GatewayCancelled,
    GatewayError,
    ModelMessage,
    ModelRequest,
    OpenAICompatibleGateway,
    ProviderCapabilities,
    ToolDefinition,
    classify_status,
)
from agents.scripted import ScriptedGateway, ScriptedStream


def request(*, timeout: float = 1.0) -> ModelRequest:
    return ModelRequest(
        trace_id=f"trace-{uuid4()}",
        model="coding-model",
        messages=(ModelMessage(role="user", content="Return the verified result."),),
        output_schema_name="verified_result",
        output_schema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
        tools=(
            ToolDefinition(
                name="read_file",
                description="Read a repository file.",
                input_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
            ),
        ),
        timeout_seconds=timeout,
    )


@pytest.mark.asyncio
async def test_openai_wire_uses_strict_schema_native_tools_usage_and_trace() -> None:
    seen: list[dict[str, Any]] = []

    async def handler(http_request: httpx.Request) -> httpx.Response:
        if http_request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "coding-model",
                            "capabilities": [
                                "structured_outputs",
                                "tool_calling",
                                "streaming",
                            ],
                        }
                    ]
                },
            )
        body = json.loads(http_request.content)
        seen.append(body)
        assert http_request.headers["authorization"] == "Bearer model-test-token"
        assert http_request.headers["x-trace-id"].startswith("trace-")
        return httpx.Response(
            200,
            headers={"x-request-id": "provider-request-1"},
            json={
                "id": "completion-1",
                "model": "coding-model",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": '{"answer":"verified"}',
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": '{"path":"src/app.py"}',
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
            },
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://model.test/v1"
    )
    gateway = OpenAICompatibleGateway(
        base_url="http://model.test/v1",
        api_key="model-test-token",  # noqa: S106 - disposable contract credential
        max_concurrency=2,
        input_cost_per_million=2.0,
        output_cost_per_million=8.0,
        client=client,
    )
    try:
        capabilities = await gateway.capabilities("coding-model")
        response = await gateway.complete(request())
    finally:
        await client.aclose()

    assert capabilities == ProviderCapabilities(
        structured_outputs=True,
        native_tool_calls=True,
        streaming=True,
        cancellation=True,
        usage_accounting=True,
    )
    assert seen[0]["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "verified_result",
            "strict": True,
            "schema": request().output_schema,
        },
    }
    assert seen[0]["tools"][0]["function"]["strict"] is True
    assert response.structured_output == {"answer": "verified"}
    assert response.tool_calls[0].call_id == "call-1"
    assert response.tool_calls[0].arguments == {"path": "src/app.py"}
    assert response.usage.total_tokens == 18
    assert response.usage.cost_usd == pytest.approx(0.000078)
    assert response.trace_id.startswith("trace-")
    assert response.provider_request_id == "provider-request-1"


@pytest.mark.asyncio
async def test_cached_input_tokens_use_their_declared_price() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "coding-model",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '{"answer":"ok"}'},
                    }
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 10,
                    "prompt_tokens_details": {"cached_tokens": 80},
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gateway = OpenAICompatibleGateway(
        base_url="http://model.test/v1",
        max_concurrency=1,
        input_cost_per_million=10.0,
        cached_input_cost_per_million=1.0,
        output_cost_per_million=20.0,
        default_capabilities=ProviderCapabilities.all_supported(),
        client=client,
    )
    try:
        result = await gateway.complete(request())
    finally:
        await client.aclose()

    assert result.usage.cached_input_tokens == 80
    assert result.usage.cost_usd == pytest.approx(0.00048)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (408, FailureClass.TRANSIENT),
        (429, FailureClass.TRANSIENT),
        (500, FailureClass.TRANSIENT),
        (400, FailureClass.PERMANENT),
        (401, FailureClass.PERMANENT),
    ],
)
def test_http_retry_classification(status: int, expected: FailureClass) -> None:
    assert classify_status(status) is expected


@pytest.mark.asyncio
async def test_timeout_is_visible_and_classified() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://model.test/v1"
    )
    gateway = OpenAICompatibleGateway(
        base_url="http://model.test/v1",
        max_concurrency=1,
        default_capabilities=ProviderCapabilities.all_supported(),
        client=client,
    )
    try:
        with pytest.raises(GatewayError) as raised:
            await gateway.complete(request(timeout=0.001))
    finally:
        await client.aclose()
    assert raised.value.failure_class is FailureClass.TIMEOUT


@pytest.mark.asyncio
async def test_gateway_concurrency_admission_enforces_model_ceiling() -> None:
    running = 0
    observed = 0
    lock = asyncio.Lock()

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal running, observed
        async with lock:
            running += 1
            observed = max(observed, running)
        await asyncio.sleep(0.02)
        async with lock:
            running -= 1
        return httpx.Response(
            200,
            json={
                "model": "coding-model",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '{"answer":"ok"}', "tool_calls": []},
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://model.test/v1"
    )
    gateway = OpenAICompatibleGateway(
        base_url="http://model.test/v1",
        max_concurrency=2,
        default_capabilities=ProviderCapabilities.all_supported(),
        client=client,
    )
    try:
        await asyncio.gather(*(gateway.complete(request()) for _ in range(5)))
    finally:
        await client.aclose()

    assert observed == 2
    assert gateway.admission.max_observed == 2


@pytest.mark.asyncio
async def test_streaming_cancellation_stops_scripted_provider() -> None:
    gateway = ScriptedGateway(
        streams=(
            ScriptedStream(
                chunks=("one", "two", "three"),
                delay=timedelta(milliseconds=1),
            ),
        )
    )
    cancelled = asyncio.Event()
    received: list[str] = []

    with pytest.raises(GatewayCancelled):
        async for chunk in gateway.stream(request(), cancel=cancelled):
            received.append(chunk.text)
            cancelled.set()

    assert received == ["one"]
