"""arq worker: polls job boards on a schedule and stores what it finds."""

import logging
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

from arq import cron
from arq.connections import RedisSettings
from sqlalchemy import select

from job_radar.boards import load_boards
from job_radar.collectors import fetch_board
from job_radar.collectors.base import make_client
from job_radar.config import get_settings
from job_radar.db import make_engine, make_sessionmaker
from job_radar.models import Board
from job_radar.steps.chain import CHAIN
from job_radar.steps.engine import Engine
from job_radar.store import apply_fetch

log = logging.getLogger(__name__)

# Sources whose data changes slower than the 6-hour cron: poll at most this often.
MIN_INTERVAL = {"himalayas": timedelta(hours=20)}


async def ping(ctx: dict[str, Any]) -> str:
    return "pong"


async def collect_board(
    ctx: dict[str, Any],
    source: str,
    org: str,
    company: str,
    region: str | None = None,
    queries: list[str] | None = None,
) -> dict[str, Any]:
    """Fetch one board and apply it. A failed fetch never reaches storage, so it cannot close anything."""
    seen_at = datetime.now(UTC)  # fetch START time: the stale guard in apply_fetch relies on it
    fetched = await fetch_board(ctx["http"], source, org, company, region, queries)
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
    now = datetime.now(UTC)
    slot = now.strftime("%Y%m%d%H")
    async with ctx["sessionmaker"]() as session:
        rows = (await session.execute(select(Board.source, Board.org, Board.last_fetch_at))).all()
    last = {(s, o): at for s, o, at in rows}
    queued = 0
    for b in load_boards(path):
        at = last.get((b.source, b.org))
        if b.source in MIN_INTERVAL and at is not None and now - at < MIN_INTERVAL[b.source]:
            continue  # e.g. Himalayas refreshes daily: polling it every 6 hours only adds load
        job = await ctx["redis"].enqueue_job(
            "collect_board",
            b.source,
            b.org,
            b.company,
            b.region,
            # Keyword and only when set: ATS jobs keep the old argument shape, so a worker that has not been
            # rebuilt yet can still run them.
            **({"queries": b.queries} if b.queries else {}),
            _job_id=f"collect:{b.source}:{b.org}:{slot}",
        )
        queued += job is not None  # arq returns None when a job with this id already exists
    return queued


async def reconcile_steps(ctx: dict[str, Any]) -> dict[str, int]:
    """Create/revive/supersede step runs from committed state. Runs every minute; idempotent."""
    engine: Engine = ctx["steps"]
    return asdict(await engine.reconcile())


async def drain_steps(ctx: dict[str, Any]) -> dict[str, int]:
    """Claim and execute a batch of step runs. Retries belong to the engine, never to arq (max_tries=1)."""
    engine: Engine = ctx["steps"]
    return await engine.drain()


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    ctx["engine"] = make_engine(settings.database_url)
    ctx["sessionmaker"] = make_sessionmaker(ctx["engine"])
    ctx["http"] = make_client()
    ctx["steps"] = Engine(
        ctx["sessionmaker"],
        CHAIN,
        lease_seconds=settings.step_lease_seconds,
        batch_size=settings.drain_batch_size,
        attempt_cap=settings.step_attempt_cap,
        hard_timeout_seconds=settings.step_hard_timeout_seconds,
        backoff_base_seconds=settings.step_backoff_base_seconds,
    )


async def shutdown(ctx: dict[str, Any]) -> None:
    await ctx["http"].aclose()
    await ctx["engine"].dispose()


class WorkerSettings:
    functions: ClassVar[list[Any]] = [ping, collect_board, collect_all]
    cron_jobs: ClassVar[list[Any]] = [
        cron(collect_all, hour={0, 6, 12, 18}, minute=5),
        # The pipeline's own retry layer (leases, backoff, attempt cap) decides re-runs: arq must never
        # retry these jobs (max_tries=1). Offsets put the usual reconcile before the next drain.
        cron(reconcile_steps, second=0, max_tries=1, timeout=120),
        # arq cron jobs are unique by default: while one drain (or reconcile) runs, the next minute's
        # enqueue is skipped, so slow steps cannot pile drains up and starve the collect jobs.
        cron(
            drain_steps,
            second=30,
            max_tries=1,
            # Longer than a full batch of hard-timeout steps, so arq never kills a drain mid-step.
            timeout=int(get_settings().step_hard_timeout_seconds * get_settings().drain_batch_size) + 60,
        ),
    ]
    # Worst case per board: 4 attempts x 20 s timeout + Retry-After sleeps capped at 60 s ~ 260 s.
    job_timeout = 600
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
