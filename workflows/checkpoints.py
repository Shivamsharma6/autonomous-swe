from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver


def checkpoint_connection_url(database_url: str) -> str:
    for driver in ("+asyncpg", "+psycopg"):
        database_url = database_url.replace(driver, "")
    if not database_url.startswith(("postgresql://", "postgres://")):
        raise ValueError("LangGraph production checkpoints require PostgreSQL")
    return database_url


@asynccontextmanager
async def postgres_checkpointer(
    database_url: str, *, setup: bool = False
) -> AsyncIterator[AsyncPostgresSaver]:
    """Own one async psycopg connection for a compiled graph's lifetime."""
    connection_url = checkpoint_connection_url(database_url)
    async with AsyncPostgresSaver.from_conn_string(connection_url) as saver:
        if setup:
            await saver.setup()
        yield saver
