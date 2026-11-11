"""Tests run against real Postgres and Redis (`docker compose up -d db redis` locally, services in CI).

Storage tests use a separate database, `<name>_test`, created and migrated once per session, so they can
truncate tables without touching development data.
"""

import asyncio

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import create_async_engine

from job_radar.config import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)  # env vars only: CI and local runs see the same config


async def _create_database(admin_url: str, name: str) -> None:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            exists = await conn.scalar(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name})
            if not exists:
                await conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def db_url() -> str:
    """URL of a migrated test database."""
    base = make_url(Settings(_env_file=None).database_url)
    test_url = base.set(database=f"{base.database}_test")
    asyncio.run(_create_database(base.render_as_string(hide_password=False), str(test_url.database)))
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", test_url.render_as_string(hide_password=False))
    command.upgrade(cfg, "head")
    return test_url.render_as_string(hide_password=False)
