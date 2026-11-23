"""The daily budget gate at claim time (spec PR 7): spend from the ledger plus reservations for running costly
rows; an out-of-credits answer pauses costly claims; free steps are never gated."""

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from job_radar.llm.provider import OutOfCreditsError
from job_radar.steps.base import PermanentStepError, RunContext, StepOutcome
from job_radar.steps.engine import Engine, LLMBudget
from tests.test_steps_engine import T0, FakeStep, check, fetch, make_steps, posting, runs


class CostlyStep(FakeStep):
    def __init__(self, name: str = "costly", estimate: float = 1.0, **kwargs: Any) -> None:
        super().__init__(name, **kwargs)
        self.cost_estimate_usd = estimate


class PricedFailure(CostlyStep):
    async def run(self, payload: dict[str, Any], ctx: RunContext) -> StepOutcome:
        raise PermanentStepError("bad output twice", cost_usd=0.25)


async def spend(db: AsyncEngine, usd: float, *, outcome: str = "ok", ago: timedelta = timedelta(0)) -> None:
    async with db.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO llm_calls (created_at, purpose, role, attempt, model, outcome, cost_usd)"
                " VALUES (now() - make_interval(secs => :ago), 'test', 'primary', 1, 'm', :o, :c)"
            ),
            {"ago": ago.total_seconds(), "o": outcome, "c": usd},
        )


async def ten_vacancies(db: AsyncEngine, steps: Engine) -> None:
    await fetch(db, T0, [posting(str(i), "Remote") for i in range(10)])
    await steps.reconcile()


def gated(db: AsyncEngine, chain: list[Any], cap: float, **kwargs: Any) -> Engine:
    return make_steps(
        db, chain=chain, budget=LLMBudget(daily_cap_usd=cap, out_of_credits_pause_seconds=600), **kwargs
    )


def test_a_costly_chain_needs_a_budget(db_url: str) -> None:
    async def scenario(db: AsyncEngine) -> None:
        with pytest.raises(ValueError, match="need an LLMBudget"):
            make_steps(db, chain=[CostlyStep()])

    check(db_url, scenario)


def test_the_cap_limits_how_many_costly_rows_are_claimed(db_url: str) -> None:
    async def scenario(db: AsyncEngine) -> None:
        steps = gated(db, [CostlyStep(estimate=1.0)], cap=2.5)
        await ten_vacancies(db, steps)
        assert len(await steps.claim_batch()) == 2  # floor(2.5 / 1.0)

    check(db_url, scenario)


def test_running_rows_reserve_budget_so_a_second_claimer_gets_nothing(db_url: str) -> None:
    async def scenario(db: AsyncEngine) -> None:
        steps = gated(db, [CostlyStep(estimate=1.0)], cap=2.5)
        other = gated(db, [CostlyStep(estimate=1.0)], cap=2.5)
        await ten_vacancies(db, steps)
        first = await steps.claim_batch()
        second = await other.claim_batch()
        assert (len(first), len(second)) == (2, 0)

    check(db_url, scenario)


def test_todays_spend_counts_and_yesterdays_does_not(db_url: str) -> None:
    async def scenario(db: AsyncEngine) -> None:
        steps = gated(db, [CostlyStep(estimate=1.0)], cap=3.0)
        await ten_vacancies(db, steps)
        await spend(db, 100.0, ago=timedelta(days=2))
        await spend(db, 2.0)
        assert len(await steps.claim_batch()) == 1

    check(db_url, scenario)


def test_out_of_credits_pauses_costly_claims_until_the_pause_ends(db_url: str) -> None:
    async def scenario(db: AsyncEngine) -> None:
        steps = gated(db, [CostlyStep(estimate=0.01)], cap=100.0)
        await ten_vacancies(db, steps)
        await spend(db, 0.0, outcome="out_of_credits", ago=timedelta(seconds=30))
        assert await steps.claim_batch() == []
        async with db.begin() as conn:
            await conn.execute(text("UPDATE llm_calls SET created_at = now() - interval '1 hour'"))
        assert len(await steps.claim_batch()) == 10

    check(db_url, scenario)


def test_free_steps_ignore_the_cap(db_url: str) -> None:
    async def scenario(db: AsyncEngine) -> None:
        steps = gated(db, [FakeStep("free"), CostlyStep(estimate=1.0)], cap=0.5)
        await ten_vacancies(db, steps)
        await spend(db, 5.0)
        counts = await steps.drain()
        assert counts == {"done": 10}
        assert {r.step for r in await runs(db) if r.status == "pending"} == {"costly"}
        assert await steps.claim_batch() == []

    check(db_url, scenario)


def test_a_failed_run_keeps_what_it_spent(db_url: str) -> None:
    async def scenario(db: AsyncEngine) -> None:
        steps = gated(db, [PricedFailure()], cap=100.0)
        await fetch(db, T0, [posting("a", "Remote")])
        await steps.reconcile()
        assert await steps.drain() == {"failed": 1}
        (row,) = await runs(db)
        assert (row.status, row.cost_usd) == ("failed", 0.25)

    check(db_url, scenario)


def test_day_boundary_is_utc_whatever_the_session_time_zone(db_url: str) -> None:
    """The engine's own connections run in UTC-12 here. A row placed between the local and the UTC start of
    "today" must count exactly as the UTC rule says, whichever side of it the local day starts on."""

    async def scenario(db: AsyncEngine) -> None:
        tz_db = create_async_engine(
            db_url, poolclass=NullPool, connect_args={"server_settings": {"TimeZone": "Etc/GMT+12"}}
        )
        try:
            steps = gated(tz_db, [CostlyStep(estimate=1.0)], cap=1.5)
            await ten_vacancies(db, steps)
            async with tz_db.begin() as conn:
                utc_start, local_start = (
                    await conn.execute(
                        text(
                            "SELECT date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC',"
                            " date_trunc('day', now())"
                        )
                    )
                ).one()
                assert utc_start != local_start
                # Just before the later of the two starts: inside one "today" only, so the rules disagree.
                in_utc_today = utc_start < local_start
                at = max(utc_start, local_start) - timedelta(seconds=1)
                await conn.execute(
                    text(
                        "INSERT INTO llm_calls (created_at, purpose, role, attempt, model, outcome, cost_usd)"
                        " VALUES (:at, 'test', 'primary', 1, 'm', 'ok', 50)"
                    ),
                    {"at": at},
                )
            assert len(await steps.claim_batch()) == (0 if in_utc_today else 1)
        finally:
            await tz_db.dispose()

    check(db_url, scenario)


def test_concurrent_claimers_cannot_overshoot_the_cap(db_url: str) -> None:
    """Four drains claim at the same moment: the advisory lock serializes the budget check, so together they
    take no more than the cap allows (without the lock each sees the full remaining budget)."""

    async def scenario(db: AsyncEngine) -> None:
        engines = [gated(db, [CostlyStep(estimate=1.0)], cap=2.5) for _ in range(4)]
        await ten_vacancies(db, engines[0])
        for _ in range(3):
            claimed = await asyncio.gather(*(e.claim_batch() for e in engines))
            assert sum(len(c) for c in claimed) <= 2
            async with db.begin() as conn:  # release everything for the next round
                await conn.execute(
                    text(
                        "UPDATE step_runs SET status='pending', claim_token=NULL, lease_until=NULL,"
                        " attempts=0"
                    )
                )

    check(db_url, scenario)


def test_out_of_credits_hands_the_attempt_back(db_url: str) -> None:
    """An empty balance says nothing about the row: probing it must not push it toward the attempt cap."""

    async def scenario(db: AsyncEngine) -> None:
        steps = gated(db, [CostlyStep(estimate=0.01, failures=10, fail_with=OutOfCreditsError)], cap=100.0)
        await fetch(db, T0, [posting("a", "Remote")])
        await steps.reconcile()
        for _ in range(5):  # more probes than the attempt cap (3)
            assert await steps.drain() == {"requeued": 1}
            async with db.begin() as conn:
                await conn.execute(text("UPDATE step_runs SET lease_until = now() - interval '1 second'"))
        (row,) = await runs(db)
        assert (row.status, row.attempts, row.error_class) == ("pending", 0, "transient")

    check(db_url, scenario)
