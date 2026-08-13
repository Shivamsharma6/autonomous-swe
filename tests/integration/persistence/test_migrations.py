from __future__ import annotations

from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError


def alembic_config(sync_url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", sync_url)
    return config


def test_migrations_upgrade_and_downgrade_without_losing_compatible_rows(
    postgres_urls: tuple[str, str],
) -> None:
    sync_url = postgres_urls[1]
    engine = create_engine(sync_url)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))

    config = alembic_config(sync_url)
    command.upgrade(config, "head")
    assert "tasks" in inspect(engine).get_table_names()

    audit_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO audit_events "
                "(id, event_type, aggregate_type, aggregate_id, payload, correlation_id, "
                "causation_id, content_hash, created_at) VALUES "
                "(:id, 'created', 'task', :aggregate_id, '{}'::jsonb, :correlation_id, "
                ":causation_id, :content_hash, now())"
            ),
            {
                "id": audit_id,
                "aggregate_id": uuid4(),
                "correlation_id": uuid4(),
                "causation_id": uuid4(),
                "content_hash": "f" * 64,
            },
        )

    with pytest.raises(DBAPIError, match="audit_events are immutable"):
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE audit_events SET event_type = 'tampered' WHERE id = :id"),
                {"id": audit_id},
            )

    project_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO projects (id, name, created_at, updated_at) "
                "VALUES (:id, 'migration project', now(), now())"
            ),
            {"id": project_id},
        )

    command.downgrade(config, "-1")
    with engine.begin() as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM projects WHERE id = :id"), {"id": project_id}
            )
            == 1
        )

    command.upgrade(config, "head")
    command.check(config)
    with engine.begin() as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM projects WHERE id = :id"), {"id": project_id}
            )
            == 1
        )
    engine.dispose()
