from __future__ import annotations

import re
from pathlib import Path
from typing import Literal, Self

from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DIGEST_PINNED_IMAGE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
_TEST_SCHEMES = ("memory://", "scripted://", "test://")


class Settings(BaseSettings):
    """Validated process configuration shared by every AutoSWE service."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="AUTOSWE_",
        extra="ignore",
        case_sensitive=False,
        validate_default=True,
    )

    autoswe_env: Literal["production", "development", "test"] = Field(
        default="production",
        validation_alias=AliasChoices("autoswe_env", "AUTOSWE_ENV"),
    )
    admin_token: SecretStr
    database_url: str
    redis_url: str
    uams_url: str
    uams_token: SecretStr = SecretStr("")
    uams_timeout_seconds: float = Field(default=15.0, gt=0, le=300)
    model_base_url: str
    model_api_key: SecretStr = SecretStr("")

    cors_origins: list[str]
    artifact_root: Path = Path("/var/lib/autoswe/artifacts")
    managed_worktree_root: Path = Path("/var/lib/autoswe/worktrees")
    python_runner_image: str
    node_runner_image: str

    max_parallel_tasks: int = Field(default=8, ge=1, le=256)
    max_parallel_tasks_per_project: int = Field(default=4, ge=1, le=256)
    max_model_concurrency: int = Field(default=4, ge=1, le=256)
    max_sandbox_concurrency: int = Field(default=4, ge=1, le=256)
    max_dynamic_tasks: int = Field(default=24, ge=0, le=1_000)
    max_plan_depth: int = Field(default=12, ge=1, le=100)
    max_total_budget_usd: float = Field(default=25.0, gt=0)
    max_total_execution_seconds: int = Field(default=7_200, ge=1, le=604_800)
    request_max_bytes: int = Field(default=1_048_576, ge=1_024, le=104_857_600)

    @property
    def is_production(self) -> bool:
        return self.autoswe_env == "production"

    @property
    def is_test(self) -> bool:
        return self.autoswe_env == "test"

    @model_validator(mode="after")
    def validate_service_boundaries(self) -> Self:
        if len(self.admin_token.get_secret_value()) < 32:
            raise ValueError("admin_token must contain at least 32 characters")
        if not self.cors_origins:
            raise ValueError("cors_origins must contain at least one explicit origin")
        if "*" in self.cors_origins:
            raise ValueError("wildcard CORS origins are forbidden")

        service_values = {
            "database_url": self.database_url,
            "redis_url": self.redis_url,
            "uams_url": self.uams_url,
            "model_base_url": self.model_base_url,
        }
        for field_name, value in service_values.items():
            if not value.strip():
                raise ValueError(f"{field_name} must be configured")

        test_adapter_used = (
            any(value.startswith(_TEST_SCHEMES) for value in service_values.values())
            or self.python_runner_image.startswith("test://")
            or self.node_runner_image.startswith("test://")
        )
        if test_adapter_used and not self.is_test:
            raise ValueError("test adapters may only be used when AUTOSWE_ENV=test")

        if not self.is_test:
            if not self.database_url.startswith(("postgresql+asyncpg://", "postgresql+psycopg://")):
                raise ValueError("database_url must use PostgreSQL in non-test environments")
            if not self.redis_url.startswith(("redis://", "rediss://")):
                raise ValueError("redis_url must use Redis in non-test environments")
            for image_field, image in (
                ("python_runner_image", self.python_runner_image),
                ("node_runner_image", self.node_runner_image),
            ):
                if not _DIGEST_PINNED_IMAGE.fullmatch(image):
                    raise ValueError(f"{image_field} must be pinned by sha256 digest")

        if self.max_parallel_tasks_per_project > self.max_parallel_tasks:
            raise ValueError("max_parallel_tasks_per_project cannot exceed max_parallel_tasks")
        if self.max_model_concurrency > self.max_parallel_tasks:
            raise ValueError("max_model_concurrency cannot exceed max_parallel_tasks")
        if self.max_sandbox_concurrency > self.max_parallel_tasks:
            raise ValueError("max_sandbox_concurrency cannot exceed max_parallel_tasks")
        return self
