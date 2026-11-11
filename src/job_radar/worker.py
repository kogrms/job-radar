"""arq worker. Week 1 PR 1: a ping task proving the queue round-trip; real tasks arrive in PR 3-4."""

from typing import Any, ClassVar

from arq.connections import RedisSettings

from job_radar.config import get_settings
from job_radar.db import make_engine


async def ping(ctx: dict[str, Any]) -> str:
    return "pong"


async def startup(ctx: dict[str, Any]) -> None:
    ctx["engine"] = make_engine(get_settings().database_url)


async def shutdown(ctx: dict[str, Any]) -> None:
    await ctx["engine"].dispose()


class WorkerSettings:
    functions: ClassVar[list[Any]] = [ping]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
