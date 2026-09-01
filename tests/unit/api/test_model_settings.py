from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from apps.api.dependencies import get_services, require_admin
from apps.api.routes import probe_models, router, update_model_config
from apps.api.routes import test_model as run_model_test
from apps.api.schemas import ModelConfigRequest, ModelProbeRequest, ModelTestRequest
from persistence.model_settings import ModelConfiguration


class MemoryModelSettings:
    def __init__(self, configuration):
        self.configuration = configuration

    async def load(self, session=None):
        return self.configuration

    async def save(self, configuration):
        self.configuration = configuration


def model_services(base_url="http://ollama:11434/v1", api_key=""):
    return SimpleNamespace(model_settings=MemoryModelSettings(ModelConfiguration(
        base_url=base_url, primary_model="local-model", api_key=SecretStr(api_key),
        timeout_seconds=120,
    )))


@pytest.fixture
def services():
    return model_services()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("endpoint", "expected_key"),
    [
        ("https://old-provider.example/v1/", "old-provider-key"),
        ("https://new-provider.example/v1", ""),
    ],
)
async def test_blank_key_is_retained_only_for_the_same_provider(
    endpoint: str, expected_key: str
) -> None:
    services = model_services("https://old-provider.example/v1", "old-provider-key")
    response = await update_model_config(
        ModelConfigRequest(base_url=endpoint, primary_model="selected-model"),
        services,
    )
    assert (await services.model_settings.load()).api_key.get_secret_value() == expected_key
    assert response.has_api_key == bool(expected_key)


@pytest.mark.parametrize("body", [{"models": []}, {"data": []}])
async def test_empty_discovery_does_not_invent_a_default_model(monkeypatch, body, services):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    client = httpx.AsyncClient(transport=transport)
    monkeypatch.setattr("apps.api.routes.httpx.AsyncClient", lambda **kwargs: client)
    response = await probe_models(ModelProbeRequest(base_url="http://ollama:11434/v1"), services)
    assert response.reachable
    assert response.models == []


@pytest.mark.parametrize("body", ["<html>Welcome</html>", '{"status":"ok"}'])
async def test_discovery_falls_back_when_models_endpoint_is_not_a_model_list(
    monkeypatch, body, services
):
    def handler(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "installed:latest"}]})
        return httpx.Response(200, text=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr("apps.api.routes.httpx.AsyncClient", lambda **kwargs: client)
    response = await probe_models(ModelProbeRequest(base_url="http://ollama:11434/v1"), services)
    assert response.reachable
    assert response.models == ["installed:latest"]


async def test_discovery_reports_authentication_failure_instead_of_connection_failed(
    monkeypatch, services
):
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(401)))
    monkeypatch.setattr("apps.api.routes.httpx.AsyncClient", lambda **kwargs: client)
    response = await probe_models(ModelProbeRequest(base_url="http://ollama:11434/v1"), services)
    assert not response.reachable
    assert "401" in response.error


async def test_malformed_model_url_returns_a_discovery_error(services):
    response = await probe_models(ModelProbeRequest(base_url="http://localhost:abc/v1"), services)
    assert not response.reachable
    assert "URL" in response.error


async def test_connection_test_uses_configured_timeout_and_explains_timeouts(monkeypatch, services):
    seen = []

    def handler(request):
        raise httpx.ReadTimeout("", request=request)

    outbound = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    def client_factory(**kwargs):
        seen.append(kwargs)
        return outbound

    monkeypatch.setattr("apps.api.routes.httpx.AsyncClient", client_factory)
    response = await run_model_test(
        ModelTestRequest(base_url="http://ollama:11434/v1", model="slow-local-model"), services
    )
    assert seen[0]["timeout"] == 120
    assert response.success is False
    assert "timed out" in response.error.lower()
    assert "120" in response.error


@pytest.mark.parametrize("body", [{"choices": []}, {"choices": [{"message": {}}]}, ["bad"]])
async def test_connection_test_reports_malformed_responses(monkeypatch, services, body):
    outbound = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=body)
    ))
    monkeypatch.setattr("apps.api.routes.httpx.AsyncClient", lambda **kwargs: outbound)
    response = await run_model_test(
        ModelTestRequest(base_url="http://ollama:11434/v1", model="local-model"), services
    )
    assert response.success is False
    assert "response" in response.error.lower()


@pytest.mark.parametrize("action", ["probe", "test"])
@pytest.mark.parametrize(
    ("endpoint", "key", "expected"),
    [
        ("https://saved-provider.example/v1/", "", "Bearer saved-key"),
        ("https://other-provider.example/v1", "", None),
        ("https://saved-provider.example/v1", "replacement", "Bearer replacement"),
    ],
)
async def test_model_checks_use_saved_credentials_only_for_the_same_endpoint(
    monkeypatch, action, endpoint, key, expected
):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"data": [], "choices": []})

    outbound = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    services = model_services("https://saved-provider.example/v1", "saved-key")
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_services] = lambda: services
    app.dependency_overrides[require_admin] = lambda: None
    payload = {"base_url": endpoint, "api_key": key}
    if action == "test":
        payload["model"] = "selected-model"
    api_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    monkeypatch.setattr("apps.api.routes.httpx.AsyncClient", lambda **kwargs: outbound)
    async with api_client as client:
        assert (await client.post(f"/api/v1/models/{action}", json=payload)).status_code == 200
    assert requests[0].headers.get("Authorization") == expected
