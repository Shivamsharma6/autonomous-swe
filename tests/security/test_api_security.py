from __future__ import annotations

from typing import Any, cast

from fastapi.testclient import TestClient

from apps.api.dependencies import ControlPlaneServices, ReadinessChecks
from apps.api.main import create_app
from infrastructure.config import Settings
from knowledge.memory.fake import FakeMemoryPort

ADMIN_TOKEN = "a" * 40


class ReadyRedis:
    async def ping(self) -> bool:
        return True


async def ready() -> bool:
    return True


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "autoswe_env": "test",
        "admin_token": ADMIN_TOKEN,
        "database_url": "test://postgres",
        "redis_url": "test://redis",
        "uams_url": "test://uams",
        "model_base_url": "test://model",
        "model_primary": "scripted-model",
        "cors_origins": ["https://console.example"],
        "python_runner_image": "test://python",
        "node_runner_image": "test://node",
        "request_max_bytes": 1_024,
        "api_rate_limit_per_minute": 2,
    }
    values.update(overrides)
    return Settings.model_validate(values)


def services(config: Settings | None = None) -> ControlPlaneServices:
    return ControlPlaneServices(
        settings=config or settings(),
        database=cast(Any, None),
        redis=ReadyRedis(),
        memory=FakeMemoryPort(),
        approvals=cast(Any, None),
        artifacts=cast(Any, None),
        scheduler=cast(Any, None),
        cancel_notify=cast(Any, None),
        readiness=ReadinessChecks(
            postgres=ready,
            redis=ready,
            checkpoints=ready,
            sandbox=ready,
            model=ready,
            uams=ready,
        ),
    )


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {ADMIN_TOKEN}"}


def test_liveness_is_public_but_every_control_route_requires_bearer_auth() -> None:
    client = TestClient(create_app(services()))

    assert client.get("/health/live").status_code == 200
    unauthorized = client.get("/api/v1/status")
    wrong = client.get("/api/v1/status", headers={"Authorization": "Bearer wrong"})

    assert unauthorized.status_code == wrong.status_code == 401
    assert unauthorized.json() == wrong.json() == {"detail": "invalid administrator credentials"}
    assert client.get("/api/v1/status", headers=auth()).status_code == 200


def test_request_size_rate_limit_security_headers_and_explicit_cors() -> None:
    client = TestClient(create_app(services()))

    too_large = client.post(
        "/api/v1/projects",
        content=b"x" * 1_025,
        headers={**auth(), "content-type": "application/json"},
    )
    first = client.get("/api/v1/status", headers=auth())
    second = client.get("/api/v1/status", headers=auth())
    limited = client.get("/api/v1/status", headers=auth())
    allowed_preflight = client.options(
        "/api/v1/status",
        headers={
            "Origin": "https://console.example",
            "Access-Control-Request-Method": "GET",
        },
    )
    rejected_preflight = client.options(
        "/api/v1/status",
        headers={
            "Origin": "https://attacker.invalid",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert too_large.status_code == 413
    assert first.status_code == second.status_code == 200
    assert limited.status_code == 429
    assert first.headers["x-content-type-options"] == "nosniff"
    assert first.headers["cache-control"] == "no-store"
    assert allowed_preflight.headers["access-control-allow-origin"] == "https://console.example"
    assert "access-control-allow-origin" not in rejected_preflight.headers


def test_openapi_and_responses_never_expose_configured_secrets() -> None:
    config = settings(
        model_api_key="model-secret-value",
        uams_token="uams-secret-value",  # noqa: S106 - verifies response redaction
    )
    client = TestClient(create_app(services(config)))

    responses = (
        client.get("/openapi.json").text,
        client.get("/health/live").text,
        client.get("/health/ready").text,
        client.get("/api/v1/status", headers=auth()).text,
    )

    for response in responses:
        assert ADMIN_TOKEN not in response
        assert "model-secret-value" not in response
        assert "uams-secret-value" not in response
