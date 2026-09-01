"""Resolve a run's private model snapshot without mutating process-wide settings."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

from agents.gateway import (
    ModelAdmission,
    ModelGateway,
    ModelRequest,
    ModelResponse,
    ModelStreamChunk,
    OpenAICompatibleGateway,
    ProviderCapabilities,
)
from domain.models import AgentSpec
from infrastructure.config import Settings
from persistence.model_settings import ModelConfiguration


class ConfiguredModelGateway:
    def __init__(self, gateway: ModelGateway, configuration: ModelConfiguration) -> None:
        self._gateway = gateway
        self._configuration = configuration

    async def capabilities(self, model: str) -> ProviderCapabilities:
        return await self._gateway.capabilities(model)

    def _request(self, request: ModelRequest) -> ModelRequest:
        return request.model_copy(update={
            "timeout_seconds": self._configuration.timeout_seconds,
            "temperature": self._configuration.temperature,
        })

    async def complete(
        self, request: ModelRequest, *, cancel: asyncio.Event | None = None
    ) -> ModelResponse:
        return await self._gateway.complete(self._request(request), cancel=cancel)

    def stream(
        self, request: ModelRequest, *, cancel: asyncio.Event | None = None
    ) -> AsyncIterator[ModelStreamChunk]:
        return self._gateway.stream(self._request(request), cancel=cancel)


@dataclass(frozen=True, slots=True)
class RuntimeModel:
    gateway: ModelGateway
    primary_model: str
    fallback_models: tuple[str, ...]

    def apply_spec(self, spec: AgentSpec) -> AgentSpec:
        return spec.model_copy(update={
            "primary_model": self.primary_model,
            "fallback_models": self.fallback_models,
        })


class ModelRuntimeFactory:
    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._defaults = ModelConfiguration.from_settings(settings)
        self._client = client
        self._admission = ModelAdmission(settings.max_model_concurrency)
        self._gateways: dict[str, OpenAICompatibleGateway] = {}
        self._runtimes: dict[str, RuntimeModel] = {}

    def resolve(self, snapshot: dict[str, Any] | None) -> RuntimeModel:
        configuration = ModelConfiguration.model_validate(snapshot) if snapshot else self._defaults
        # Private in-process cache key; never logged or used as public metadata.
        key = json.dumps(configuration.private_storage(), sort_keys=True)
        if key not in self._runtimes:
            gateway = OpenAICompatibleGateway(
                base_url=configuration.base_url,
                api_key=configuration.api_key.get_secret_value(),
                max_concurrency=self._settings.max_model_concurrency,
                input_cost_per_million=self._settings.model_input_cost_per_million,
                cached_input_cost_per_million=self._settings.model_cached_input_cost_per_million,
                output_cost_per_million=self._settings.model_output_cost_per_million,
                default_capabilities=(
                    ProviderCapabilities.all_supported()
                    if self._settings.model_capability_mode == "declared" else None
                ),
                client=self._client,
            )
            # A settings change must not multiply the process concurrency limit.
            gateway.admission = self._admission
            self._gateways[key] = gateway
            self._runtimes[key] = RuntimeModel(
                gateway=ConfiguredModelGateway(gateway, configuration),
                primary_model=configuration.primary_model,
                fallback_models=configuration.fallback_models,
            )
        return self._runtimes[key]

    async def close(self) -> None:
        for gateway in self._gateways.values():
            await gateway.close()
        self._gateways.clear()
        self._runtimes.clear()
