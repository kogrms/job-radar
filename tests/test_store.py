"""Properties of apply_fetch, checked against a plain-Python model of the expected state."""

import asyncio
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from job_radar.db import make_sessionmaker
from job_radar.models import Vacancy, VacancyEvent
from job_radar.postings import Posting, content_hash, dedup_key
from job_radar.store import FetchResult, _lock_board, apply_fetch

T0 = datetime(2026, 11, 1, tzinfo=UTC)
SOURCE, ORG = "ashby", "acme"
JOB_IDS = ["a", "b", "c", "d", "e"]


def posting(job_id: str, variant: int) -> Posting:
    return Posting(
        source_job_id=job_id,
        url=f"https://jobs.example/{job_id}",
        title=f"Senior Engineer {job_id}"
        if variant == 2
        else f"Engineer {job_id}",  # variant 2 moves dedup_key
        company="Acme",
        location_raw="Remote",
        raw_text=f"text {job_id} v{variant}",
        raw_payload={"volatile": variant * 1000 + 7},
    )


@dataclass(frozen=True)
class Fetch:
    at: datetime
    postings: dict[str, int]  # job_id -> content variant
    complete: bool


fetch_contents = st.dictionaries(st.sampled_from(JOB_IDS), st.integers(0, 2), max_size=len(JOB_IDS))


@st.composite
def fetch_sequences(draw: st.DrawFn, max_size: int = 8) -> list[Fetch]:
    n = draw(st.integers(1, max_size))
    return [Fetch(T0 + timedelta(hours=i), draw(fetch_contents), draw(st.booleans())) for i in range(n)]


# --- reference model -------------------------------------------------------------------------------------


@dataclass
class Row:
    variant: int
    first_seen: datetime
    last_seen: datetime
    closed_at: datetime | None = None


@dataclass
class Model:
    rows: dict[str, Row]
    events: Counter[str]
    last_at: datetime | None = None
    last_complete_empty: bool = False

    @classmethod
    def empty(cls) -> "Model":
        return cls({}, Counter())

    @property
    def open(self) -> set[str]:
        return {j for j, r in self.rows.items() if r.closed_at is None}

    def apply(self, f: Fetch) -> None:
        if self.last_at is not None and f.at <= self.last_at:
            return  # stale
        self.last_at = f.at
        for jid, variant in f.postings.items():
            r = self.rows.get(jid)
            if r is None:
                self.rows[jid] = Row(variant, f.at, f.at)
                self.events["opened"] += 1
                continue
            if r.closed_at is not None:
                r.closed_at = None
                self.events["reopened"] += 1
            if content_hash(posting(jid, r.variant)) != content_hash(posting(jid, variant)):
                self.events["changed"] += 1  # also when reopened in the same fetch
            r.variant, r.last_seen = variant, f.at
        if f.complete:
            # An empty complete listing closes the board only when the previous complete fetch was empty
            # too (one-off empty listings are ATS incidents; decisions.md 2026-11-16).
            empty = not f.postings
            if not empty or self.last_complete_empty:
                for jid in self.open - f.postings.keys():
                    self.rows[jid].closed_at = f.at
                    self.events["closed"] += 1
            self.last_complete_empty = empty

    def expected(self) -> tuple[dict[str, tuple[Any, ...]], Counter[str]]:
        rows = {}
        for jid, r in self.rows.items():
            p = posting(jid, r.variant)
            rows[jid] = (
                p.title,
                p.raw_text,
                p.raw_payload,
                dedup_key(p.company, p.title),
                content_hash(p),
                r.first_seen,
                r.last_seen,
                r.closed_at,
            )
        return rows, +self.events


# --- database helpers ------------------------------------------------------------------------------------


def engine_for(url: str) -> AsyncEngine:
    return create_async_engine(url, poolclass=NullPool)


async def reset(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE vacancy_events, vacancies, boards RESTART IDENTITY CASCADE"))


async def apply(engine: AsyncEngine, f: Fetch) -> FetchResult:
    async with make_sessionmaker(engine)() as session, session.begin():
        return await apply_fetch(
            session, SOURCE, ORG, [posting(j, v) for j, v in f.postings.items()], f.at, complete=f.complete
        )


async def db_state(engine: AsyncEngine) -> tuple[dict[str, tuple[Any, ...]], Counter[str]]:
    """Every stored column the model predicts, per posting, plus event counts."""
    async with make_sessionmaker(engine)() as session:
        vs = (await session.execute(select(Vacancy))).scalars().all()
        kinds = (await session.execute(select(VacancyEvent.kind))).scalars().all()
    rows = {
        v.source_job_id: (
            v.title,
            v.raw_text,
            v.raw_payload,
            v.dedup_key,
            v.content_hash,
            v.first_seen_at,
            v.last_seen_at,
            v.closed_at,
        )
        for v in vs
    }
    return rows, +Counter(kinds)


def check(url: str, scenario: Any) -> None:
    async def run() -> None:
        engine = engine_for(url)
        try:
            await reset(engine)
            await scenario(engine)
        finally:
            await engine.dispose()

    asyncio.run(run())


PROPERTY = settings(
    max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)


# --- properties ------------------------------------------------------------------------------------------


@PROPERTY
@given(fetch_sequences())
def test_state_matches_model_for_any_sequence(db_url: str, fetches: list[Fetch]) -> None:
    """Covers: partial fetches never close, complete fetches close the missing, reopen, change detection."""

    async def scenario(engine: AsyncEngine) -> None:
        model = Model.empty()
        for f in fetches:
            await apply(engine, f)
            model.apply(f)
        assert await db_state(engine) == model.expected()

    check(db_url, scenario)


@PROPERTY
@given(fetch_sequences(), st.randoms(use_true_random=False))
def test_out_of_order_fetches_never_regress(db_url: str, fetches: list[Fetch], rnd: Any) -> None:
    """Fetches arriving in any order: older-than-newest ones are ignored, so the result equals the model
    applied to exactly the accepted subsequence."""
    shuffled = fetches[:]
    rnd.shuffle(shuffled)

    async def scenario(engine: AsyncEngine) -> None:
        model = Model.empty()
        accepted: list[Fetch] = []
        for f in shuffled:
            result = await apply(engine, f)
            expected_stale = model.last_at is not None and f.at <= model.last_at
            assert (result.outcome == "stale") == expected_stale
            if not expected_stale:
                accepted.append(f)  # stale fetches never touch state (review of PR 6, finding 1)
            model.apply(f)
        assert await db_state(engine) == model.expected()
        if accepted and all(f.complete for f in accepted):
            # Spec wording: with complete fetches only, the open set is exactly the newest NON-EMPTY applied
            # fetch's postings — an empty listing closes the board only after two in a row (the two-empty
            # rule), so a single trailing empty fetch leaves the previous open set intact. Only the APPLIED
            # subsequence counts: a fetch that arrived late is stale and invisible.
            empties = 0
            for f in reversed(accepted):  # accepted is in increasing `at` order by construction
                if f.postings:
                    break
                empties += 1
            rows, _ = await db_state(engine)
            open_ids = {j for j, r in rows.items() if r[-1] is None}
            if empties == 0:
                assert open_ids == set(accepted[-1].postings)
            elif empties >= 2:
                assert open_ids == set()
            else:  # exactly one trailing empty listing: nothing closed by it
                before = next((f for f in reversed(accepted) if f.postings), None)
                assert open_ids == (set(before.postings) if before else set())

    check(db_url, scenario)


@PROPERTY
@given(fetch_contents, st.booleans(), st.integers(1, 4))
def test_reapplying_a_fetch_is_a_no_op(
    db_url: str, contents: dict[str, int], complete: bool, times: int
) -> None:
    """The same fetch retried (same timestamp) or re-fetched later with the same content adds no events."""

    async def scenario(engine: AsyncEngine) -> None:
        first = Fetch(T0, contents, complete)
        model = Model.empty()
        await apply(engine, first)
        model.apply(first)
        events_before = (await db_state(engine))[1]
        for i in range(times):
            assert (await apply(engine, first)).outcome == "stale"
            again = Fetch(T0 + timedelta(minutes=i + 1), contents, complete)
            later = await apply(engine, again)
            model.apply(again)  # only last_seen_at moves
            assert later.opened == later.changed == later.reopened == later.closed == 0
        rows, events = await db_state(engine)
        assert (rows, events) == model.expected()
        assert events == events_before and events["opened"] == len(contents)

    check(db_url, scenario)


# --- examples --------------------------------------------------------------------------------------------


def test_older_fetch_waits_for_the_lock_and_becomes_stale(db_url: str) -> None:
    """Worker A takes the board lock and has not written anything yet; worker B arrives with an older fetch.
    B must block until A commits, then see A's timestamp and be stale.

    A holds the lock *before* touching the board row on purpose: once A has updated the row, Postgres makes
    B's INSERT ... ON CONFLICT wait anyway, which would hide a missing FOR UPDATE (see agent-log)."""

    async def scenario(engine: AsyncEngine) -> None:
        await apply(engine, Fetch(T0, {"a": 0}, True))  # board row exists and is committed
        newer = Fetch(T0 + timedelta(hours=2), {"a": 1, "b": 0}, True)
        older = Fetch(T0 + timedelta(hours=1), {"c": 0}, True)

        async with make_sessionmaker(engine)() as a_session:
            await a_session.begin()
            await _lock_board(a_session, SOURCE, ORG)
            b_task = asyncio.create_task(apply(engine, older))
            await asyncio.sleep(0.5)
            assert not b_task.done(), "B did not wait for A's board lock"
            await apply_fetch(
                a_session,
                SOURCE,
                ORG,
                [posting(j, v) for j, v in newer.postings.items()],
                newer.at,
                complete=True,
            )
            await a_session.commit()
        b_result = await asyncio.wait_for(b_task, timeout=5)

        assert b_result.outcome == "stale"
        model = Model.empty()
        model.apply(Fetch(T0, {"a": 0}, True))
        model.apply(newer)
        assert await db_state(engine) == model.expected()

    check(db_url, scenario)


def test_failed_transaction_leaves_no_trace_and_retry_applies(db_url: str) -> None:
    """A rolled-back fetch must not advance last_fetch_at: the retry is applied, not stale."""

    async def scenario(engine: AsyncEngine) -> None:
        await apply(engine, Fetch(T0, {"a": 0}, True))
        before = await db_state(engine)
        retry = Fetch(T0 + timedelta(hours=1), {"a": 1, "b": 0}, True)

        class Boom(Exception):
            pass

        with pytest.raises(Boom):
            async with make_sessionmaker(engine)() as session, session.begin():
                await apply_fetch(
                    session,
                    SOURCE,
                    ORG,
                    [posting(j, v) for j, v in retry.postings.items()],
                    retry.at,
                    complete=True,
                )
                raise Boom
        assert await db_state(engine) == before
        assert (await apply(engine, retry)).outcome == "applied"

    check(db_url, scenario)


def test_rejects_naive_timestamp_and_duplicate_ids(db_url: str) -> None:
    async def scenario(engine: AsyncEngine) -> None:
        async with make_sessionmaker(engine)() as session, session.begin():
            with pytest.raises(ValueError, match="timezone-aware"):
                await apply_fetch(session, SOURCE, ORG, [], datetime(2026, 1, 1), complete=True)
            with pytest.raises(ValueError, match="duplicate"):
                await apply_fetch(session, SOURCE, ORG, [posting("a", 0), posting("a", 1)], T0, complete=True)

    check(db_url, scenario)


def test_volatile_payload_does_not_count_as_a_change(db_url: str) -> None:
    async def scenario(engine: AsyncEngine) -> None:
        await apply(engine, Fetch(T0, {"a": 0}, True))
        async with make_sessionmaker(engine)() as session, session.begin():
            p = posting("a", 0)
            moved = Posting(**{**p.__dict__, "raw_payload": {"volatile": 999}})
            result = await apply_fetch(session, SOURCE, ORG, [moved], T0 + timedelta(hours=1), complete=True)
        assert result.unchanged == 1 and result.changed == 0

    check(db_url, scenario)


def test_one_empty_listing_closes_nothing_two_in_a_row_close_the_board(db_url: str) -> None:
    """An ATS incident looks like one empty complete listing: the first one must not close anything."""

    async def scenario(engine: AsyncEngine) -> None:
        await apply(engine, Fetch(T0, {"a": 0, "b": 0}, True))

        first_empty = await apply(engine, Fetch(T0 + timedelta(hours=1), {}, True))
        assert first_empty.closed == 0

        # A non-empty complete fetch in between resets the streak: the next empty one is "first" again.
        await apply(engine, Fetch(T0 + timedelta(hours=2), {"a": 0}, True))  # closes b legitimately
        reset_empty = await apply(engine, Fetch(T0 + timedelta(hours=3), {}, True))
        assert reset_empty.closed == 0

        second_empty = await apply(engine, Fetch(T0 + timedelta(hours=4), {}, True))
        assert second_empty.closed == 1  # now a closes: two empty complete listings in a row

        async with make_sessionmaker(engine)() as session:
            open_ids = (
                (await session.execute(select(Vacancy.source_job_id).where(Vacancy.closed_at.is_(None))))
                .scalars()
                .all()
            )
        assert open_ids == []

    check(db_url, scenario)
