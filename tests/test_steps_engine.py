"""Integration tests of the step engine against real Postgres (spec PR 6, "Verify").

Each test speaks to the DB through the same code paths as production: apply_fetch for content, reconcile for
work creation, drain/run_claim for execution. Leases are shortened to seconds; no sleeps beyond that.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from job_radar.db import make_sessionmaker
from job_radar.models import StepRun, Vacancy
from job_radar.postings import Posting
from job_radar.steps.base import (
    PermanentStepError,
    StepInput,
    StepOutcome,
    TransientStepError,
    input_hash,
)
from job_radar.steps.chain import CHAIN
from job_radar.steps.eligibility_rules_step import EligibilityRulesStep
from job_radar.steps.engine import Engine
from job_radar.steps.ops import backfill, retry_failed, steps_status
from job_radar.store import apply_fetch

T0 = datetime(2026, 11, 1, tzinfo=UTC)
SOURCE, ORG = "ashby", "acme"


class SimulatedCrash(BaseException):
    """A worker dying at a failpoint. BaseException: nothing in the engine may catch it."""


def posting(job_id: str, location: str) -> Posting:
    return Posting(
        source_job_id=job_id,
        url=f"https://jobs.example/{job_id}",
        title=f"Engineer {job_id}",
        company="Acme",
        location_raw=location,
        raw_text=f"text {job_id} {location}",
        raw_payload={},
    )


class FakeStep:
    """A configurable step: raises `fail_with` while `failures` > 0, sleeps, records calls."""

    def __init__(
        self,
        name: str = "fake",
        *,
        failures: int = 0,
        fail_with: type[Exception] = TransientStepError,
        sleep: float = 0.0,
    ) -> None:
        self.name = name
        self.version = "t1"
        self.failures = failures
        self.fail_with = fail_with
        self.sleep = sleep
        self.calls: list[dict[str, Any]] = []

    def build_input(self, vacancy: Vacancy, upstream: StepRun | None) -> StepInput | None:
        payload: dict[str, Any] = {"location_raw": vacancy.location_raw}
        if upstream is not None:
            payload["upstream_hash"] = upstream.input_hash
            payload["upstream_result"] = upstream.result
        return StepInput(payload=payload, hash=input_hash(self.name, self.version, payload))

    async def run(self, payload: dict[str, Any]) -> StepOutcome:
        self.calls.append(payload)
        if self.sleep:
            await asyncio.sleep(self.sleep)
        if self.failures > 0:
            self.failures -= 1
            raise self.fail_with("induced failure")
        return StepOutcome(result={"ok": True, "on": payload.get("location_raw")})


def engine_for(url: str) -> AsyncEngine:
    return create_async_engine(url, poolclass=NullPool)


def make_steps(db: AsyncEngine, chain: list[Any] | None = None, **kwargs: Any) -> Engine:
    defaults: dict[str, Any] = {
        "lease_seconds": 2,
        "batch_size": 10,
        "attempt_cap": 3,
        "hard_timeout_seconds": 10,
        "backoff_base_seconds": 0.01,
    }
    return Engine(make_sessionmaker(db), chain or list(CHAIN), **{**defaults, **kwargs})


async def reset(db: AsyncEngine) -> None:
    async with db.begin() as conn:
        await conn.execute(text("TRUNCATE step_runs, vacancy_events, vacancies, boards RESTART IDENTITY"))


async def fetch(db: AsyncEngine, at: datetime, postings: list[Posting], *, complete: bool = True) -> None:
    async with make_sessionmaker(db)() as session, session.begin():
        await apply_fetch(session, SOURCE, ORG, postings, at, complete=complete)


async def runs(db: AsyncEngine) -> list[StepRun]:
    async with make_sessionmaker(db)() as session:
        return list((await session.execute(select(StepRun).order_by(StepRun.id))).scalars().all())


async def expire_lease(db: AsyncEngine, run_id: int) -> None:
    async with db.begin() as conn:
        await conn.execute(
            text("UPDATE step_runs SET lease_until = now() - interval '1 second' WHERE id = :id"),
            {"id": run_id},
        )


def check(url: str, scenario: Any) -> None:
    async def run() -> None:
        db = engine_for(url)
        try:
            await reset(db)
            await scenario(db)
        finally:
            await db.dispose()

    asyncio.run(run())


# --- creating work ---------------------------------------------------------------------------------------


def test_reconcile_creates_one_pending_row_and_is_idempotent(db_url: str) -> None:
    async def scenario(db: AsyncEngine) -> None:
        await fetch(db, T0, [posting("a", "Remote, Serbia")])
        steps = make_steps(db)

        first = await steps.reconcile()
        assert first.created == 1
        (row,) = await runs(db)
        rules = EligibilityRulesStep()
        assert (row.step, row.status) == ("eligibility_rules", "pending")
        assert row.input_hash == input_hash(rules.name, rules.version, {"location_raw": "Remote, Serbia"})

        second = await steps.reconcile()
        assert (second.created, second.revived, second.superseded) == (0, 0, 0)
        assert len(await runs(db)) == 1

    check(db_url, scenario)


def test_content_change_supersedes_pending_and_only_last_hash_runs(db_url: str) -> None:
    async def scenario(db: AsyncEngine) -> None:
        await fetch(db, T0, [posting("a", "Remote, Serbia")])
        steps = make_steps(db)
        await steps.reconcile()
        await fetch(db, T0 + timedelta(hours=1), [posting("a", "US only")])
        await fetch(db, T0 + timedelta(hours=2), [posting("a", "EU remote")])

        result = await steps.reconcile()
        assert result.created == 1 and result.superseded == 1
        await steps.drain()

        by_status = {r.status: r for r in await runs(db)}
        assert by_status["skipped"].skip_reason == "superseded"
        assert by_status["done"].result is not None  # only the EU-remote hash ever ran
        assert len(await runs(db)) == 2  # the US-only hash never got a row

    check(db_url, scenario)


def test_closed_vacancy_skips_work_and_reopen_with_same_content_revives_it(db_url: str) -> None:
    async def scenario(db: AsyncEngine) -> None:
        await fetch(db, T0, [posting("a", "Remote, Serbia")])
        steps = make_steps(db)
        await steps.reconcile()

        await fetch(db, T0 + timedelta(hours=1), [])  # first empty complete: closes nothing yet
        await fetch(db, T0 + timedelta(hours=2), [])  # second: closes `a`
        result = await steps.reconcile()
        assert result.closed == 1
        (row,) = await runs(db)
        assert (row.status, row.skip_reason) == ("skipped", "closed")

        await fetch(db, T0 + timedelta(hours=3), [posting("a", "Remote, Serbia")])  # reopened, same content
        result = await steps.reconcile()
        assert result.revived == 1 and result.created == 0
        (row,) = await runs(db)
        assert (row.status, row.skip_reason) == ("pending", None)

    check(db_url, scenario)


def test_flapping_content_reuses_the_done_row(db_url: str) -> None:
    async def scenario(db: AsyncEngine) -> None:
        await fetch(db, T0, [posting("a", "Remote, Serbia")])
        steps = make_steps(db)
        await steps.reconcile()
        await steps.drain()  # H1 done

        await fetch(db, T0 + timedelta(hours=1), [posting("a", "US only")])  # H2
        await steps.reconcile()
        await fetch(db, T0 + timedelta(hours=2), [posting("a", "Remote, Serbia")])  # back to H1
        result = await steps.reconcile()

        assert result.superseded == 1  # the H2 pending row
        by_status = {r.status: r for r in await runs(db)}
        assert by_status["done"].attempts == 1  # H1 was not re-run
        assert by_status["skipped"].skip_reason == "superseded"
        assert await steps.drain() == {}

    check(db_url, scenario)


# --- executing work --------------------------------------------------------------------------------------


def test_drain_runs_the_rules_step_to_done(db_url: str) -> None:
    async def scenario(db: AsyncEngine) -> None:
        await fetch(db, T0, [posting("a", "Remote, Serbia")])
        steps = make_steps(db)
        await steps.reconcile()

        assert await steps.drain() == {"done": 1}
        (row,) = await runs(db)
        assert row.status == "done" and row.result is not None
        assert row.result["label"] == "yes" and row.claim_token is None and row.attempts == 1
        assert row.prompt_version == "rules-v2" and row.latency_ms is not None

        assert await steps.drain() == {}  # nothing left to claim

    check(db_url, scenario)


def test_next_step_row_is_created_in_the_commit_transaction(db_url: str) -> None:
    async def scenario(db: AsyncEngine) -> None:
        a, b = FakeStep("fa"), FakeStep("fb")
        await fetch(db, T0, [posting("a", "Anywhere")])
        steps = make_steps(db, chain=[a, b])
        await steps.reconcile()

        assert await steps.drain() == {"done": 1}  # runs fa; fb's row must exist without a reconcile
        by_step = {r.step: r for r in await runs(db)}
        assert by_step["fa"].status == "done" and by_step["fb"].status == "pending"

        assert await steps.drain() == {"done": 1}
        assert b.calls[0]["upstream_hash"] == by_step["fa"].input_hash
        assert b.calls[0]["upstream_result"] == {"ok": True, "on": "Anywhere"}

    check(db_url, scenario)


def test_transient_errors_back_off_then_hit_the_attempt_cap(db_url: str) -> None:
    async def scenario(db: AsyncEngine) -> None:
        step = FakeStep(failures=99, fail_with=TransientStepError)
        await fetch(db, T0, [posting("a", "Anywhere")])
        steps = make_steps(db, chain=[step], attempt_cap=3)
        await steps.reconcile()

        for expected_attempts in (1, 2, 3):
            assert await steps.drain() == {"requeued": 1}
            (row,) = await runs(db)
            assert (row.status, row.error_class, row.attempts) == ("pending", "transient", expected_attempts)
            await asyncio.sleep(0.05)  # let the tiny backoff pass

        assert await steps.drain() == {}  # the cap turns the row failed/exhausted instead of claiming it
        (row,) = await runs(db)
        assert (row.status, row.error_class) == ("failed", "exhausted")
        assert len(step.calls) == 3

    check(db_url, scenario)


def test_backoff_delays_the_next_claim(db_url: str) -> None:
    """A transiently failed row sits out its backoff: an immediate drain must not re-claim it."""

    async def scenario(db: AsyncEngine) -> None:
        step = FakeStep(failures=1, fail_with=TransientStepError)
        await fetch(db, T0, [posting("a", "Anywhere")])
        steps = make_steps(db, chain=[step], backoff_base_seconds=3600)
        await steps.reconcile()

        assert await steps.drain() == {"requeued": 1}
        assert await steps.drain() == {}  # the hour-long backoff is in lease_until: not claimable
        assert len(step.calls) == 1

        (row,) = await runs(db)
        await expire_lease(db, row.id)  # backoff over
        assert await steps.drain() == {"done": 1}

    check(db_url, scenario)


def test_permanent_error_fails_at_once_and_retry_failed_requeues(db_url: str) -> None:
    async def scenario(db: AsyncEngine) -> None:
        step = FakeStep(failures=1, fail_with=PermanentStepError)
        await fetch(db, T0, [posting("a", "Anywhere")])
        steps = make_steps(db, chain=[step])
        await steps.reconcile()

        assert await steps.drain() == {"failed": 1}
        (row,) = await runs(db)
        assert (row.status, row.error_class, row.attempts) == ("failed", "permanent", 1)
        assert row.result is None and "induced failure" in (row.error_message or "")

        async with make_sessionmaker(db)() as session, session.begin():
            assert await retry_failed(session, step.name) == 1
        assert await steps.drain() == {"done": 1}

    check(db_url, scenario)


def test_unexpected_exception_classes_as_permanent(db_url: str) -> None:
    async def scenario(db: AsyncEngine) -> None:
        step = FakeStep(failures=1, fail_with=ValueError)  # a bug, not a declared step error
        await fetch(db, T0, [posting("a", "Anywhere")])
        steps = make_steps(db, chain=[step])
        await steps.reconcile()

        assert await steps.drain() == {"failed": 1}
        (row,) = await runs(db)
        assert row.error_class == "permanent" and "ValueError" in (row.error_message or "")

    check(db_url, scenario)


def test_lost_lease_discards_the_late_result(db_url: str) -> None:
    """Two workers claim the same row in turn; the first one's commit must write nothing."""

    async def scenario(db: AsyncEngine) -> None:
        await fetch(db, T0, [posting("a", "Remote, Serbia")])
        steps = make_steps(db)
        await steps.reconcile()

        (stale_claim,) = await steps.claim_batch()
        await expire_lease(db, stale_claim.id)
        (fresh_claim,) = await steps.claim_batch()  # reclaims the expired row
        assert fresh_claim.id == stale_claim.id and fresh_claim.claim_token != stale_claim.claim_token

        assert await steps.run_claim(fresh_claim) == "done"
        assert await steps.run_claim(stale_claim) == "lost"  # fenced: late worker writes nothing

        (row,) = await runs(db)
        assert (row.status, row.attempts) == ("done", 2)

    check(db_url, scenario)


def test_fence_blocks_a_stale_claim_while_another_worker_holds_the_row(db_url: str) -> None:
    """The late worker finishes while the row is claimed by someone else: it must write nothing at all."""

    async def scenario(db: AsyncEngine) -> None:
        await fetch(db, T0, [posting("a", "Remote, Serbia")])
        steps = make_steps(db)
        await steps.reconcile()

        (stale_claim,) = await steps.claim_batch()
        await expire_lease(db, stale_claim.id)
        (fresh_claim,) = await steps.claim_batch()  # now the row belongs to this claim

        assert await steps.run_claim(stale_claim) == "lost"
        (row,) = await runs(db)
        assert (row.status, row.claim_token) == ("running", fresh_claim.claim_token)
        assert row.result is None

        assert await steps.run_claim(fresh_claim) == "done"

    check(db_url, scenario)


def test_heartbeat_keeps_a_slow_step_claimed(db_url: str) -> None:
    async def scenario(db: AsyncEngine) -> None:
        step = FakeStep(sleep=3.5)  # longer than the 2 s lease: without heartbeats it would be reclaimed
        await fetch(db, T0, [posting("a", "Anywhere")])
        steps = make_steps(db, chain=[step])
        await steps.reconcile()

        async def try_steal() -> list[Any]:
            await asyncio.sleep(2.5)  # past the original lease; the heartbeat must have renewed it
            return await steps.claim_batch()

        outcome, stolen = await asyncio.gather(steps.drain(), try_steal())
        assert outcome == {"done": 1} and stolen == []
        (row,) = await runs(db)
        assert row.attempts == 1

    check(db_url, scenario)


def test_claiming_skips_locked_rows_instead_of_blocking(db_url: str) -> None:
    async def scenario(db: AsyncEngine) -> None:
        await fetch(db, T0, [posting("a", "Anywhere")])
        steps = make_steps(db)
        await steps.reconcile()
        (row,) = await runs(db)

        async with db.connect() as conn:  # a concurrent claimer holds the row lock
            await conn.execute(text("SELECT 1 FROM step_runs WHERE id = :id FOR UPDATE"), {"id": row.id})
            claims = await asyncio.wait_for(steps.claim_batch(), timeout=5)  # must not block on the lock
            assert claims == []
            await conn.rollback()

    check(db_url, scenario)


def test_content_change_during_the_run_is_caught_at_commit(db_url: str) -> None:
    async def scenario(db: AsyncEngine) -> None:
        step = FakeStep(sleep=0.5)
        await fetch(db, T0, [posting("a", "Remote, Serbia")])
        steps = make_steps(db, chain=[step])
        await steps.reconcile()

        async def change_mid_run() -> None:
            await asyncio.sleep(0.2)
            await fetch(db, T0 + timedelta(hours=1), [posting("a", "US only")])

        outcome, _ = await asyncio.gather(steps.drain(), change_mid_run())
        assert outcome == {"superseded": 1}  # the result would be stored under the wrong hash
        (row,) = await runs(db)
        assert (row.status, row.skip_reason, row.result) == ("skipped", "superseded", None)
        assert row.latency_ms is not None  # the spent work is still accounted on the row

    check(db_url, scenario)


def test_crash_before_commit_recovers_within_a_lease(db_url: str) -> None:
    async def scenario(db: AsyncEngine) -> None:
        def crash(point: str) -> None:
            if point == "before_commit":
                raise SimulatedCrash(point)

        await fetch(db, T0, [posting("a", "Remote, Serbia")])
        crashing = make_steps(db, failpoint=crash)
        await crashing.reconcile()
        with pytest.raises(SimulatedCrash):
            await crashing.drain()
        (row,) = await runs(db)
        assert row.status == "running"  # the dead worker's claim is still visible

        healthy = make_steps(db)
        assert await healthy.drain() == {}  # the lease has not expired yet: nothing to reclaim
        await expire_lease(db, row.id)
        assert await healthy.drain() == {"done": 1}
        (row,) = await runs(db)
        assert (row.status, row.attempts) == ("done", 2)

    check(db_url, scenario)


def test_checks_reject_an_error_stored_as_a_result(db_url: str) -> None:
    async def scenario(db: AsyncEngine) -> None:
        await fetch(db, T0, [posting("a", "Anywhere")])
        steps = make_steps(db)
        await steps.reconcile()
        (row,) = await runs(db)

        for sets in (
            "status='failed', error_class='permanent', result='{}'::jsonb",  # failed with a result
            "status='done'",  # done without a result
            "status='done', result='{}'::jsonb, error_class='transient'",  # done with an error
            "status='skipped'",  # skipped without a reason
        ):
            with pytest.raises(IntegrityError):
                async with db.begin() as conn:
                    await conn.execute(text(f"UPDATE step_runs SET {sets} WHERE id = :id"), {"id": row.id})

    check(db_url, scenario)


# --- operator commands -----------------------------------------------------------------------------------


def test_backfill_releases_baseline_rows_explicitly(db_url: str) -> None:
    async def scenario(db: AsyncEngine) -> None:
        await fetch(db, T0, [posting("a", "Remote, Serbia"), posting("b", "US only")])
        rules = EligibilityRulesStep()
        async with make_sessionmaker(db)() as session, session.begin():
            for v in (await session.execute(select(Vacancy))).scalars():
                inp = rules.build_input(v, None)
                assert inp is not None
                session.add(
                    StepRun(
                        vacancy_id=v.id,
                        step=rules.name,
                        input_hash=inp.hash,
                        status="skipped",
                        skip_reason="baseline",
                    )
                )

        steps = make_steps(db)
        assert (await steps.reconcile()).created == 0  # baseline suppresses new work

        async with make_sessionmaker(db)() as session, session.begin():
            assert await backfill(session, rules.name, limit=10, dry_run=True) == 2
        assert all(r.status == "skipped" for r in await runs(db))  # dry run changes nothing

        async with make_sessionmaker(db)() as session, session.begin():
            assert await backfill(session, rules.name, limit=1) == 1
        assert await steps.drain() == {"done": 1}

    check(db_url, scenario)


def test_steps_status_counts(db_url: str) -> None:
    async def scenario(db: AsyncEngine) -> None:
        await fetch(db, T0, [posting("a", "Remote, Serbia"), posting("b", "US only")])
        steps = make_steps(db)
        await steps.reconcile()
        await steps.drain()

        async with make_sessionmaker(db)() as session:
            status = await steps_status(session, lease_seconds=2)
        assert ("eligibility_rules", "done", 2) in status.counts
        assert status.stuck_running == 0 and status.oldest_waiting_human_s is None

    check(db_url, scenario)
