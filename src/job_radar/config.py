"""Settings from environment variables (and `.env` for local runs)."""

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", env_ignore_empty=True
    )  # "X=" means unset

    database_url: str = "postgresql+asyncpg://job_radar:job_radar@localhost:5433/job_radar"
    redis_url: str = "redis://localhost:6380/0"

    langfuse_base_url: str = "http://localhost:3000"
    langfuse_public_key: str | None = None
    langfuse_secret_key: SecretStr | None = None

    # Private data (past decisions, vacancy texts, labels). Never copied into the repo; unset in CI.
    private_dir: Path | None = None
    # Boards to poll (YAML, see examples/boards.yml). Personal, so it lives in the private dir.
    boards_file: Path | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
