"""arq worker: polls job boards on a schedule and stores what it finds."""

import logging
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any, ClassVar

from arq import cron
from arq.connections import RedisSettings

from job_radar.boards import load_boards
from job_radar.collectors import fetch_board
from job_radar.collectors.base import make_client
from job_radar.config import get_settings
from job_radar.db import make_engine, make_sessionmaker
from job_radar.store import apply_fetch

log = logging.getLogger(__name__)


async def ping(ctx: dict[str, Any]) -> str:
    return "pong"


async def collect_board(
    ctx: dict[str, Any], source: str, org: str, company: str, region: str | None = None
) -> dict[str, Any]:
    """Fetch one board and apply it. A failed fetch never reaches storage, so it cannot close anything."""
    seen_at = datetime.now(UTC)  # fetch START time: the stale guard in apply_fetch relies on it
    fetched = await fetch_board(ctx["http"], source, org, company, region)
    if fetched.error:
        log.warning("fetch %s/%s failed: %s", source, org, fetched.error)
        return {"outcome": "failed", "error": fetched.error}
    async with ctx["sessionmaker"]() as session, session.begin():
        result = await apply_fetch(session, source, org, fetched.postings, seen_at, complete=fetched.complete)
    summary = {**asdict(result), "postings": len(fetched.postings), "complete": fetched.complete}
    log.info("fetch %s/%s: %s", source, org, summary)
    return summary


async def collect_all(ctx: dict[str, Any]) -> int:
    """Enqueue one collect_board job per configured board; return how many were newly queued.

    The job id includes the hour, so a cron firing twice in the same hour (restart, two workers) enqueues each
    board once.
    """
    path = get_settings().boards_file
    if path is None:
        log.warning("BOARDS_FILE is not set, nothing to collect")
        return 0
    slot = datetime.now(UTC).strftime("%Y%m%d%H")
    queued = 0
    for b in load_boards(path):
        job = await ctx["redis"].enqueue_job(
            "collect_board",
            b.source,
            b.org,
            b.company,
            b.region,
            _job_id=f"collect:{b.source}:{b.org}:{slot}",
        )
        queued += job is not None  # arq returns None when a job with this id already exists
    return queued


async def startup(ctx: dict[str, Any]) -> None:
    ctx["engine"] = make_engine(get_settings().database_url)
    ctx["sessionmaker"] = make_sessionmaker(ctx["engine"])
    ctx["http"] = make_client()


async def shutdown(ctx: dict[str, Any]) -> None:
    await ctx["http"].aclose()
    await ctx["engine"].dispose()


class WorkerSettings:
    functions: ClassVar[list[Any]] = [ping, collect_board, collect_all]
    cron_jobs: ClassVar[list[Any]] = [cron(collect_all, hour={0, 6, 12, 18}, minute=5)]
    # Worst case per board: 4 attempts x 20 s timeout + Retry-After sleeps capped at 60 s ~ 260 s.
    job_timeout = 600
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
