"""Durable workspace settings and private, immutable run configuration snapshots."""

from __future__ import annotations

from typing import Any

from pydantic import Field, SecretStr
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from domain.models import ContractModel
from infrastructure.config import Settings
from persistence.database import Database
from persistence.tables import WorkspaceModelConfigRow, utc_now


class ModelConfiguration(ContractModel):
    base_url: str = Field(min_length=1, max_length=2_000)
    primary_model: str = Field(min_length=1, max_length=200)
    api_key: SecretStr = Field(default_factory=lambda: SecretStr(""))
    fallback_models: tuple[str, ...] = Field(default_factory=tuple, max_length=10)
    timeout_seconds: float = Field(default=120, gt=0, le=3_600)
    temperature: float = Field(default=0, ge=0, le=2)

    @classmethod
    def from_settings(cls, settings: Settings) -> ModelConfiguration:
        return cls(
            base_url=settings.model_base_url.rstrip("/"),
            primary_model=settings.model_primary,
            api_key=settings.model_api_key,
            fallback_models=tuple(settings.model_fallbacks),
            timeout_seconds=settings.model_timeout_seconds,
        )

    def private_storage(self) -> dict[str, Any]:
        # Only for protected database columns. Never return this in an API,
        # audit event, model prompt or log; ordinary model_dump masks the key.
        return self.model_dump(mode="json") | {"api_key": self.api_key.get_secret_value()}


class ModelSettingsStore:
    def __init__(self, database: Database, defaults: ModelConfiguration) -> None:
        self._database = database
        self._defaults = defaults

    async def load(self, session: AsyncSession | None = None) -> ModelConfiguration:
        if session is None:
            async with self._database.sessions() as own_session:
                return await self.load(own_session)
        row = await session.get(WorkspaceModelConfigRow, 1)
        return ModelConfiguration.model_validate(row.configuration) if row else self._defaults

    async def save(self, configuration: ModelConfiguration) -> None:
        async with self._database.transaction() as session:
            await session.execute(
                insert(WorkspaceModelConfigRow)
                .values(id=1, configuration=configuration.private_storage())
                .on_conflict_do_update(
                    index_elements=[WorkspaceModelConfigRow.id],
                    set_={
                        "configuration": configuration.private_storage(), "updated_at": utc_now(),
                    },
                )
            )
