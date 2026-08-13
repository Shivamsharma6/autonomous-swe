from __future__ import annotations

import pytest
from pydantic import ValidationError

from infrastructure.config import Settings


def production_values() -> dict[str, object]:
    return {
        "autoswe_env": "production",
        "admin_token": "admin-token-with-at-least-thirty-two-bytes",
        "database_url": "postgresql+asyncpg://autoswe:password@postgres:5432/autoswe",
        "redis_url": "redis://redis:6379/0",
        "uams_url": "http://host.docker.internal:8000",
        "uams_token": "uams-service-token",
        "model_base_url": "http://host.docker.internal:11434/v1",
        "model_api_key": "model-service-token",
        "cors_origins": ["http://localhost:3000"],
        "artifact_root": "/var/lib/autoswe/artifacts",
        "managed_worktree_root": "/var/lib/autoswe/worktrees",
        "python_runner_image": "ghcr.io/example/python-runner@sha256:" + "a" * 64,
        "node_runner_image": "ghcr.io/example/node-runner@sha256:" + "b" * 64,
    }


def test_production_settings_accept_explicit_safe_services() -> None:
    settings = Settings(_env_file=None, **production_values())

    assert settings.is_production is True
    assert settings.max_parallel_tasks == 8
    assert settings.max_parallel_tasks_per_project == 4
    assert settings.max_model_concurrency == 4
    assert settings.max_sandbox_concurrency == 4


def test_production_rejects_missing_admin_token() -> None:
    values = production_values()
    del values["admin_token"]

    with pytest.raises(ValidationError, match="admin_token"):
        Settings(_env_file=None, **values)


def test_production_rejects_sqlite_database() -> None:
    values = production_values() | {"database_url": "sqlite:///autoswe.db"}

    with pytest.raises(ValidationError, match="PostgreSQL"):
        Settings(_env_file=None, **values)


def test_production_rejects_wildcard_cors() -> None:
    values = production_values() | {"cors_origins": ["*"]}

    with pytest.raises(ValidationError, match="wildcard"):
        Settings(_env_file=None, **values)


@pytest.mark.parametrize("field", ["uams_url", "model_base_url"])
def test_production_rejects_unconfigured_external_boundary(field: str) -> None:
    values = production_values() | {field: ""}

    with pytest.raises(ValidationError, match=field):
        Settings(_env_file=None, **values)


def test_memory_and_scripted_adapters_are_test_only() -> None:
    test_values = {
        "autoswe_env": "test",
        "admin_token": "test-admin-token-with-at-least-thirty-two",
        "database_url": "memory://domain",
        "redis_url": "memory://events",
        "uams_url": "memory://uams",
        "model_base_url": "scripted://model",
        "cors_origins": ["http://testserver"],
        "python_runner_image": "test://python-runner",
        "node_runner_image": "test://node-runner",
    }

    assert Settings(_env_file=None, **test_values).is_test is True

    with pytest.raises(ValidationError, match="test adapters"):
        Settings(_env_file=None, **(test_values | {"autoswe_env": "production"}))


def test_secrets_are_redacted_from_repr_and_json() -> None:
    settings = Settings(_env_file=None, **production_values())

    rendered = repr(settings) + settings.model_dump_json()
    assert "admin-token-with-at-least-thirty-two-bytes" not in rendered
    assert "uams-service-token" not in rendered
    assert "model-service-token" not in rendered
    assert "**********" in rendered
