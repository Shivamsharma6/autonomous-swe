from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from persistence.tables import Base


class Database:
    def __init__(self, url: str, *, echo: bool = False) -> None:
        if not url.startswith("postgresql+asyncpg://"):
            raise ValueError("the domain database must use postgresql+asyncpg")
        self.engine: AsyncEngine = create_async_engine(
            url, echo=echo, pool_pre_ping=True, hide_parameters=True
        )
        self.sessions = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        self.metadata = Base.metadata

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        async with self.sessions() as session, session.begin():
            yield session

    async def dispose(self) -> None:
        await self.engine.dispose()
