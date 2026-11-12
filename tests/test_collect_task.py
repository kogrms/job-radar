"""collect_board / collect_all end to end: mocked HTTP, real Postgres (test DB) and Redis."""

import asyncio
from pathlib import Path
from typing import Any

import httpx
import respx
from arq import create_pool
from arq.connections import RedisSettings
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from job_radar import worker
from job_radar.collectors.base import BoardFetch, make_client
from job_radar.config import Settings, get_settings
from job_radar.db import make_sessionmaker
from job_radar.models import Vacancy, VacancyEvent
from job_radar.worker import collect_all, collect_board

from .test_collectors import ASHBY, GREENHOUSE, URLS


async def _counts(maker: Any) -> tuple[int, int, int]:
    async with maker() as s:
        n = await s.scalar(select(func.count()).select_from(Vacancy))
        open_ = await s.scalar(select(func.count()).select_from(Vacancy).where(Vacancy.closed_at.is_(None)))
        ev = await s.scalar(select(func.count()).select_from(VacancyEvent))
    return n or 0, open_ or 0, ev or 0


def test_collect_board_stores_then_is_idempotent_and_failures_close_nothing(db_url: str) -> None:
    async def run() -> None:
        engine = create_async_engine(db_url, poolclass=NullPool)
        async with engine.begin() as conn:
            await conn.execute(text("TRUNCATE vacancy_events, vacancies, boards RESTART IDENTITY CASCADE"))
        ctx: dict[str, Any] = {"sessionmaker": make_sessionmaker(engine), "http": make_client()}
        try:
            with respx.mock:
                route = respx.get(URLS["ashby"])
                route.respond(json=ASHBY)
                first = await collect_board(ctx, "ashby", "examplecorp", "Example Corp")
                assert first["opened"] == 1 and first["outcome"] == "applied"
                await asyncio.sleep(0.01)  # a later seen_at, as a real next poll would have
                second = await collect_board(ctx, "ashby", "examplecorp", "Example Corp")
                assert second["outcome"] == "applied" and second["unchanged"] == 1 and second["opened"] == 0
                assert await _counts(ctx["sessionmaker"]) == (1, 1, 1)

                route.mock(side_effect=[httpx.Response(404)])
                failed = await collect_board(ctx, "ashby", "examplecorp", "Example Corp")
                assert failed == {"outcome": "failed", "error": "http 404"}
                assert await _counts(ctx["sessionmaker"]) == (1, 1, 1)  # nothing closed, no events
        finally:
            await ctx["http"].aclose()
            await engine.dispose()

    asyncio.run(run())


def test_collect_all_enqueues_once_per_hour_and_respects_min_interval(
    tmp_path: Path, db_url: str, settings: Settings, monkeypatch: Any
) -> None:
    boards = tmp_path / "boards.yml"
    boards.write_text(
        "boards:\n"
        "  - {source: ashby, org: one, company: One}\n"
        "  - {source: lever, org: two, company: Two}\n"
        "  - {source: himalayas, org: rs, queries: [python]}\n"
    )
    monkeypatch.setenv("BOARDS_FILE", str(boards))
    get_settings.cache_clear()

    async def run() -> None:
        engine, ctx = _fresh_ctx(db_url)
        await _truncate(engine)
        async with engine.begin() as conn:  # all three fetched 2 hours ago: too soon only for daily Himalayas
            await conn.execute(
                text(
                    "INSERT INTO boards (source, org, last_fetch_at) VALUES"
                    " ('himalayas', 'RS', now() - interval '2 hours'),"
                    " ('ashby', 'one', now() - interval '2 hours'),"
                    " ('lever', 'two', now() - interval '2 hours')"
                )
            )
        pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        ctx["redis"] = pool
        try:
            await pool.flushdb()
            assert await collect_all(ctx) == 2  # two ATS boards; Himalayas skipped by MIN_INTERVAL
            assert await collect_all(ctx) == 0  # same hour: job ids collide, nothing new queued
            async with engine.begin() as conn:
                await conn.execute(text("UPDATE boards SET last_fetch_at = now() - interval '21 hours'"))
            await pool.flushdb()
            assert await collect_all(ctx) == 3  # a day later Himalayas is due again
            jobs = {(j.args[0], j.args[1]): j for j in await pool.queued_jobs()}
            assert jobs[("himalayas", "RS")].kwargs == {"queries": ["python"]}
            assert jobs[("ashby", "one")].args == ("ashby", "one", "One", None)
            assert jobs[("ashby", "one")].kwargs == {}  # ATS jobs keep the old argument shape
        finally:
            await pool.flushdb()
            await pool.aclose()
            await ctx["http"].aclose()
            await engine.dispose()
            get_settings.cache_clear()

    asyncio.run(run())


def _fresh_ctx(db_url: str) -> tuple[Any, dict[str, Any]]:
    engine = create_async_engine(db_url, poolclass=NullPool)
    return engine, {"sessionmaker": make_sessionmaker(engine), "http": make_client()}


async def _truncate(engine: Any) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE vacancy_events, vacancies, boards RESTART IDENTITY CASCADE"))


def test_truncated_listing_reaches_storage_as_incomplete(db_url: str) -> None:
    """collect_board must pass complete=False through: a truncated Greenhouse listing closes nothing."""
    two = {"jobs": [GREENHOUSE["jobs"][0], {**GREENHOUSE["jobs"][0], "id": 102}], "meta": {"total": 2}}
    truncated = {"jobs": [GREENHOUSE["jobs"][0]], "meta": {"total": 2}}

    async def run() -> None:
        engine, ctx = _fresh_ctx(db_url)
        await _truncate(engine)
        try:
            with respx.mock:
                route = respx.get(URLS["greenhouse"])
                route.respond(json=two)
                assert (await collect_board(ctx, "greenhouse", "examplecorp", "Example Corp"))["opened"] == 2
                await asyncio.sleep(0.01)
                route.respond(json=truncated)
                r = await collect_board(ctx, "greenhouse", "examplecorp", "Example Corp")
                assert r["complete"] is False and r["closed"] == 0
                assert await _counts(ctx["sessionmaker"]) == (2, 2, 2)  # both still open, no closed events
        finally:
            await ctx["http"].aclose()
            await engine.dispose()

    asyncio.run(run())


def test_seen_at_is_taken_before_the_http_call(db_url: str, monkeypatch: Any) -> None:
    """A slow fetch that started first must lose to a faster one that started later and already applied."""
    release = asyncio.Event()
    calls = 0

    async def fake_fetch(
        client: Any, source: str, org: str, company: str, region: str | None = None, queries: Any = None
    ) -> BoardFetch:
        nonlocal calls
        calls += 1
        if calls == 1:
            await release.wait()  # the slow one
            return BoardFetch(postings=[], complete=True)  # would close everything if it were applied
        from job_radar.collectors import ashby

        return ashby.parse(ASHBY, org, company)

    monkeypatch.setattr(worker, "fetch_board", fake_fetch)

    async def run() -> None:
        engine, ctx = _fresh_ctx(db_url)
        await _truncate(engine)
        try:
            slow = asyncio.create_task(collect_board(ctx, "ashby", "examplecorp", "Example Corp"))
            await asyncio.sleep(0.05)  # slow has taken its seen_at and is waiting inside the fetch
            fast = await collect_board(ctx, "ashby", "examplecorp", "Example Corp")
            assert fast["outcome"] == "applied" and fast["opened"] == 1
            release.set()
            assert (await slow)["outcome"] == "stale"
            assert await _counts(ctx["sessionmaker"]) == (1, 1, 1)  # nothing closed by the slow, older fetch
        finally:
            await ctx["http"].aclose()
            await engine.dispose()

    asyncio.run(run())
