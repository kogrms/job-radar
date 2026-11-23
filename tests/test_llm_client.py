"""LLM.structured against the fake provider and the real ledger (spec PR 7: validation, one retry with the
error attached, spend recorded for every attempt including failed and cancelled ones)."""

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from job_radar.db import make_sessionmaker
from job_radar.extract.schema import VacancyFields
from job_radar.llm.client import LLM, InvalidOutputError
from job_radar.llm.ledger import CallTags, Ledger, LedgerError
from job_radar.llm.pricing import cost_usd
from job_radar.llm.provider import (
    LLMPermanentError,
    LLMTimeoutError,
    LLMTransientError,
    OutOfCreditsError,
)
from job_radar.models import LLMCall
from job_radar.steps.base import TransientStepError
from tests.llm_fakes import USAGE, FakeProvider, answer, fields, refusal, truncated
from tests.test_steps_engine import check

MODEL = "gpt-6-luna"
TAGS = CallTags(purpose="test")


def make_llm(db: AsyncEngine, provider: FakeProvider) -> LLM:
    return LLM(provider, Ledger(make_sessionmaker(db)))


async def call(llm: LLM, **kwargs: Any) -> Any:
    return await llm.structured(
        tags=TAGS,
        model=MODEL,
        prompt_version="p1",
        instructions="extract",
        user_input="BEGIN POSTING x\n...\nEND POSTING x",
        schema=VacancyFields,
        **kwargs,
    )


async def ledger_rows(db: AsyncEngine) -> list[LLMCall]:
    async with make_sessionmaker(db)() as session:
        return list((await session.execute(select(LLMCall).order_by(LLMCall.id))).scalars().all())


def test_valid_answer_is_returned_and_its_real_cost_recorded(db_url: str) -> None:
    async def scenario(db: AsyncEngine) -> None:
        result = await call(make_llm(db, FakeProvider([answer(fields())])))
        assert result.value.title == "Backend Engineer"
        assert (result.calls, result.invalid_outputs) == (1, 0)
        assert result.cost_usd == pytest.approx(cost_usd(MODEL, USAGE))
        (row,) = await ledger_rows(db)
        assert (row.outcome, row.attempt, row.model, row.response_model) == ("ok", 1, MODEL, "fake-snap")
        assert row.cost_usd == pytest.approx(cost_usd(MODEL, USAGE))
        assert (row.input_tokens, row.output_tokens, row.reasoning_tokens) == (1000, 200, 50)
        assert row.finished_at is not None

    check(db_url, scenario)


def test_invalid_answer_gets_one_retry_with_the_problem_attached(db_url: str) -> None:
    async def scenario(db: AsyncEngine) -> None:
        provider = FakeProvider([answer({"title": "x"}), answer(fields())])
        result = await call(make_llm(db, provider))
        assert (result.calls, result.invalid_outputs) == (2, 1)
        retry = provider.requests[1].messages
        assert [m.role for m in retry] == ["user", "assistant", "user"]
        assert "schema validation failed" in retry[2].content and "seniority" in retry[2].content
        assert [r.outcome for r in await ledger_rows(db)] == ["invalid", "ok"]
        assert result.cost_usd == pytest.approx(2 * cost_usd(MODEL, USAGE))

    check(db_url, scenario)


def test_two_invalid_answers_are_permanent_and_carry_their_cost(db_url: str) -> None:
    async def scenario(db: AsyncEngine) -> None:
        provider = FakeProvider([answer("not json"), truncated()])
        with pytest.raises(InvalidOutputError) as err:
            await call(make_llm(db, provider))
        assert isinstance(err.value, LLMPermanentError)
        assert err.value.cost_usd == pytest.approx(2 * cost_usd(MODEL, USAGE))
        rows = await ledger_rows(db)
        assert [r.outcome for r in rows] == ["invalid", "invalid"]
        assert "cut off" in (rows[1].error_message or "")

    check(db_url, scenario)


def test_refusal_is_recorded_and_retried(db_url: str) -> None:
    async def scenario(db: AsyncEngine) -> None:
        await call(make_llm(db, FakeProvider([refusal(), answer(fields())])))
        assert [r.outcome for r in await ledger_rows(db)] == ["refusal", "ok"]

    check(db_url, scenario)


def test_failed_custom_check_triggers_the_retry(db_url: str) -> None:
    async def scenario(db: AsyncEngine) -> None:
        provider = FakeProvider([answer(fields(location_quote="nowhere")), answer(fields())])
        result = await call(
            make_llm(db, provider), check=lambda f: "bad quote" if f.location_quote == "nowhere" else None
        )
        assert result.invalid_outputs == 1
        assert "bad quote" in provider.requests[1].messages[2].content

    check(db_url, scenario)


def test_transient_error_after_a_billed_attempt_carries_that_cost(db_url: str) -> None:
    async def scenario(db: AsyncEngine) -> None:
        provider = FakeProvider([answer("{}"), LLMTransientError("503: overloaded")])
        with pytest.raises(LLMTransientError) as err:
            await call(make_llm(db, provider))
        assert err.value.cost_usd == pytest.approx(cost_usd(MODEL, USAGE))
        rows = await ledger_rows(db)
        assert [(r.outcome, r.cost_usd) for r in rows] == [
            ("invalid", pytest.approx(rows[0].cost_usd)),
            ("transient_error", 0.0),
        ]

    check(db_url, scenario)


def test_out_of_credits_is_marked_in_the_ledger(db_url: str) -> None:
    async def scenario(db: AsyncEngine) -> None:
        with pytest.raises(OutOfCreditsError):
            await call(make_llm(db, FakeProvider([OutOfCreditsError("429 insufficient_quota")])))
        (row,) = await ledger_rows(db)
        assert row.outcome == "out_of_credits"

    check(db_url, scenario)


def test_a_cancelled_call_keeps_its_estimate_as_spend(db_url: str) -> None:
    """A hard timeout (or kill -9) mid-request: the provider may have billed it, so the estimate stays."""

    async def scenario(db: AsyncEngine) -> None:
        provider = FakeProvider(hang=asyncio.Event())
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.2):
                await call(make_llm(db, provider))
        (row,) = await ledger_rows(db)
        assert row.outcome == "started" and row.cost_usd > 0
        assert await Ledger(make_sessionmaker(db)).spent_today() == pytest.approx(row.cost_usd)

    check(db_url, scenario)


class FlakyLedger(Ledger):
    """A ledger whose `finish` fails (a DB blip) on the given call numbers."""

    def __init__(self, db: AsyncEngine, fail_finish_on: set[int]) -> None:
        super().__init__(make_sessionmaker(db))
        self.fail_finish_on = fail_finish_on
        self.finishes = 0

    async def finish(self, call_id: int, **kwargs: Any) -> None:
        self.finishes += 1
        if self.finishes in self.fail_finish_on:
            raise LedgerError("llm_calls update failed: OperationalError")
        await super().finish(call_id, **kwargs)


def test_a_ledger_failure_after_a_billed_call_is_transient_and_keeps_the_estimate(db_url: str) -> None:
    async def scenario(db: AsyncEngine) -> None:
        llm = LLM(FakeProvider([answer(fields())]), FlakyLedger(db, {1}))
        with pytest.raises(LedgerError) as err:
            await call(llm)
        assert isinstance(err.value, TransientStepError)
        assert err.value.cost_usd == pytest.approx(cost_usd(MODEL, USAGE))
        (row,) = await ledger_rows(db)
        assert row.outcome == "started" and row.cost_usd > 0  # over-counted, never lost

    check(db_url, scenario)


def test_a_timeout_keeps_the_estimate_as_spend(db_url: str) -> None:
    async def scenario(db: AsyncEngine) -> None:
        with pytest.raises(LLMTimeoutError) as err:
            await call(make_llm(db, FakeProvider([LLMTimeoutError("APITimeoutError")])))
        (row,) = await ledger_rows(db)
        assert row.outcome == "transient_error" and row.cost_usd > 0
        assert err.value.cost_usd == pytest.approx(row.cost_usd, abs=1e-6)  # DB keeps 6 decimals

    check(db_url, scenario)


def test_spent_today_is_a_utc_day_whatever_the_session_time_zone(db_url: str) -> None:
    async def scenario(db: AsyncEngine) -> None:
        tz_db = create_async_engine(
            db_url, poolclass=NullPool, connect_args={"server_settings": {"TimeZone": "Etc/GMT+12"}}
        )
        try:
            async with tz_db.begin() as conn:
                utc_start, local_start = (
                    await conn.execute(
                        text(
                            "SELECT date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC',"
                            " date_trunc('day', now())"
                        )
                    )
                ).one()
                in_utc_today = utc_start < local_start
                await conn.execute(
                    text(
                        "INSERT INTO llm_calls (created_at, purpose, role, attempt, model, outcome, cost_usd)"
                        " VALUES (:at, 't', 'primary', 1, 'm', 'ok', 5)"
                    ),
                    {"at": max(utc_start, local_start) - timedelta(seconds=1)},
                )
            spent = await Ledger(make_sessionmaker(tz_db)).spent_today()
            assert spent == (5.0 if in_utc_today else 0.0)
        finally:
            await tz_db.dispose()

    check(db_url, scenario)


async def test_the_real_ledger_turns_db_errors_into_transient_errors() -> None:
    down = create_async_engine("postgresql+asyncpg://127.0.0.1:1/none", poolclass=NullPool)
    ledger = Ledger(make_sessionmaker(down))
    try:
        with pytest.raises(LedgerError):
            await ledger.start(TAGS, attempt=1, model=MODEL, prompt_version="p1", estimate_usd=0.01)
        with pytest.raises(LedgerError):
            await ledger.finish(1, outcome="ok", cost_usd=0.01)
    finally:
        await down.dispose()
