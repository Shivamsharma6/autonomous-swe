from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy import text
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer

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


@pytest.fixture(scope="session")
def redis_url() -> Iterator[str]:
    with RedisContainer(image="redis:7.4-bookworm") as redis_container:
        host = redis_container.get_container_host_ip()
        port = redis_container.get_exposed_port(redis_container.port)
        yield f"redis://{host}:{port}/0"


@pytest_asyncio.fixture
async def redis_client(redis_url: str) -> AsyncIterator[Redis]:
    client = Redis.from_url(redis_url, decode_responses=True)
    await client.flushdb()
    yield client
    await client.aclose()
