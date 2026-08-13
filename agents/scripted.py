from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta

from agents.gateway import (
    GatewayCancelled,
    GatewayError,
    ModelAdmission,
    ModelRequest,
    ModelResponse,
    ModelStreamChunk,
    ProviderCapabilities,
)


@dataclass(frozen=True, slots=True)
class ScriptedResponse:
    response: ModelResponse | None = None
    error: GatewayError | None = None
    delay: timedelta = timedelta(0)


@dataclass(frozen=True, slots=True)
class ScriptedStream:
    chunks: tuple[str, ...]
    delay: timedelta = timedelta(0)


class ScriptedGateway:
    """Deterministic CI model that exercises the real runtime contract."""

    def __init__(
        self,
        *,
        responses: tuple[ScriptedResponse, ...] = (),
        streams: tuple[ScriptedStream, ...] = (),
        max_concurrency: int = 100,
    ) -> None:
        self._responses = list(responses)
        self._streams = list(streams)
        self._lock = asyncio.Lock()
        self.requests: list[ModelRequest] = []
        self.admission = ModelAdmission(max_concurrency)

    async def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities.all_supported()

    async def complete(
        self, request: ModelRequest, *, cancel: asyncio.Event | None = None
    ) -> ModelResponse:
        async with self.admission.slot():
            async with self._lock:
                if not self._responses:
                    raise AssertionError("scripted model response queue is exhausted")
                step = self._responses.pop(0)
                self.requests.append(request)
            if step.delay:
                await _cancel_aware_delay(step.delay, cancel)
            if cancel is not None and cancel.is_set():
                raise GatewayCancelled()
            if step.error is not None:
                raise step.error
            if step.response is None:
                raise AssertionError("scripted response must contain response or error")
            return step.response.model_copy(
                update={"trace_id": request.trace_id, "model": request.model}
            )

    async def stream(
        self, request: ModelRequest, *, cancel: asyncio.Event | None = None
    ) -> AsyncIterator[ModelStreamChunk]:
        async with self.admission.slot():
            async with self._lock:
                if not self._streams:
                    raise AssertionError("scripted model stream queue is exhausted")
                step = self._streams.pop(0)
                self.requests.append(request)
            for index, text in enumerate(step.chunks):
                if cancel is not None and cancel.is_set():
                    raise GatewayCancelled()
                if step.delay:
                    await _cancel_aware_delay(step.delay, cancel)
                yield ModelStreamChunk(
                    trace_id=request.trace_id,
                    text=text,
                    finish_reason="stop" if index == len(step.chunks) - 1 else None,
                )


async def _cancel_aware_delay(delay: timedelta, cancel: asyncio.Event | None) -> None:
    sleep = asyncio.create_task(asyncio.sleep(delay.total_seconds()))
    if cancel is None:
        await sleep
        return
    cancellation = asyncio.create_task(cancel.wait())
    done, _ = await asyncio.wait({sleep, cancellation}, return_when=asyncio.FIRST_COMPLETED)
    if cancellation in done and cancel.is_set():
        sleep.cancel()
        await asyncio.gather(sleep, return_exceptions=True)
        raise GatewayCancelled()
    cancellation.cancel()
    await asyncio.gather(cancellation, return_exceptions=True)
