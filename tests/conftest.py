"""Tests run against real Postgres and Redis (`docker compose up -d db redis` locally, services in CI)."""

import pytest

from job_radar.config import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)  # env vars only: CI and local runs see the same config
