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
    sandbox_manager_url: str = "http://sandbox-manager:8090"
    model_base_url: str
    model_api_key: SecretStr = SecretStr("")
    model_primary: str
    model_fallbacks: list[str] = Field(default_factory=list, max_length=10)
    model_timeout_seconds: float = Field(default=120.0, gt=0, le=3_600)
    model_input_cost_per_million: float = Field(default=0.0, ge=0)
    model_cached_input_cost_per_million: float = Field(default=0.0, ge=0)
    model_output_cost_per_million: float = Field(default=0.0, ge=0)
    model_capability_mode: Literal["detect", "declared"] = "detect"
    model_declared_capabilities: set[
        Literal[
            "structured_outputs",
            "native_tool_calls",
            "streaming",
            "usage_accounting",
        ]
    ] = Field(default_factory=set)

    cors_origins: list[str]
    artifact_root: Path = Path("/var/lib/autoswe/artifacts")
    repository_import_root: Path = Path("/var/lib/autoswe/imports")
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
    api_rate_limit_per_minute: int = Field(default=120, ge=1, le=100_000)
    host_uid: int = Field(
        default=65532,
        ge=1,
        validation_alias=AliasChoices("host_uid", "AUTOSWE_UID"),
    )
    host_gid: int = Field(
        default=65532,
        ge=1,
        validation_alias=AliasChoices("host_gid", "AUTOSWE_GID"),
    )
    otel_exporter_otlp_endpoint: str = Field(
        default="",
        validation_alias=AliasChoices(
            "otel_exporter_otlp_endpoint", "OTEL_EXPORTER_OTLP_ENDPOINT"
        ),
    )

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
            "sandbox_manager_url": self.sandbox_manager_url,
            "model_base_url": self.model_base_url,
            "model_primary": self.model_primary,
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
        required_model_capabilities = {
            "structured_outputs",
            "native_tool_calls",
            "streaming",
            "usage_accounting",
        }
        if self.model_capability_mode == "declared" and not required_model_capabilities.issubset(
            self.model_declared_capabilities
        ):
            missing = required_model_capabilities.difference(self.model_declared_capabilities)
            raise ValueError(
                "model_declared_capabilities is missing production requirements: "
                + ", ".join(sorted(missing))
            )
        return self
