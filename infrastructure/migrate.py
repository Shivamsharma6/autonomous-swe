from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from alembic import command
from alembic.config import Config

from infrastructure.config import Settings
from workflows.checkpoints import checkpoint_connection_url, postgres_checkpointer

_MIGRATION_LOCK_ID = 7_292_038_507_734_981_122


def sync_database_url(database_url: str) -> str:
    return checkpoint_connection_url(database_url).replace(
        "postgresql://",
        "postgresql+psycopg://",
        1,
    )


@contextmanager
def migration_lock(database_url: str) -> Iterator[None]:
    connection_url = checkpoint_connection_url(database_url)
    with psycopg.connect(connection_url, autocommit=True) as connection:
        connection.execute("SELECT pg_advisory_lock(%s)", (_MIGRATION_LOCK_ID,))
        try:
            yield
        finally:
            connection.execute("SELECT pg_advisory_unlock(%s)", (_MIGRATION_LOCK_ID,))


async def setup_checkpoints(database_url: str) -> None:
    async with postgres_checkpointer(database_url, setup=True):
        return


def migrate() -> None:
    settings = Settings()
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", sync_database_url(settings.database_url))
    with migration_lock(settings.database_url):
        command.upgrade(config, "head")
        command.check(config)
        asyncio.run(setup_checkpoints(settings.database_url))


if __name__ == "__main__":
    migrate()
