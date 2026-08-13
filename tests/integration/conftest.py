from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from testcontainers.community.postgres import PostgresContainer

from persistence.database import Database


@pytest.fixture(scope="session")
def postgres_urls() -> Iterator[tuple[str, str]]:
    with PostgresContainer(
        image="postgres:16-bookworm",
        username="autoswe",
        password="autoswe-test-password",  # noqa: S106 - isolated disposable database
        dbname="autoswe_test",
        driver=None,
    ) as postgres:
        raw_url = postgres.get_connection_url(driver=None)
        sync_url = raw_url.replace("postgresql://", "postgresql+psycopg://", 1)
        async_url = raw_url.replace("postgresql://", "postgresql+asyncpg://", 1)
        yield async_url, sync_url


@pytest_asyncio.fixture
async def database(postgres_urls: tuple[str, str]) -> AsyncIterator[Database]:
    database = Database(postgres_urls[0])
    async with database.engine.begin() as connection:
        await connection.execute(text("DROP SCHEMA public CASCADE"))
        await connection.execute(text("CREATE SCHEMA public"))
        await connection.run_sync(database.metadata.create_all)
    yield database
    await database.dispose()
