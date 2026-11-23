"""Property test of the pipeline (spec PR 6): random interleavings of fetches (including commits in a
different order from id allocation), reconciles, drains, and crashes at failpoints — then, once quiesced:

- liveness: every open vacancy has a `done` run for the current input of every chain step;
- nothing is left `pending`/`running`, and closed vacancies carry no live work;
- at most one `done` per (vacancy, step, input_hash) — the DB unique key, re-asserted here;
- the CHECK constraints held throughout (any violation would have raised).
"""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from job_radar.db import make_sessionmaker
from job_radar.models import StepRun, Vacancy
from job_radar.store import apply_fetch
from tests.test_steps_engine import FakeStep, SimulatedCrash, make_steps, posting

T0 = datetime(2026, 11, 1, tzinfo=UTC)
BOARDS = [("ashby", "acme"), ("lever", "beta")]
JOB_IDS = ["a", "b", "c"]
LOCATIONS = ["Remote, Serbia", "US only", "EU remote"]

fetch_contents = st.dictionaries(st.sampled_from(JOB_IDS), st.integers(0, 2), max_size=len(JOB_IDS))
operations = st.lists(
    st.one_of(
        st.tuples(st.just("fetch"), st.integers(0, 1), fetch_contents, st.booleans()),
        st.tuples(st.just("pair_fetch"), fetch_contents, fetch_contents),  # commit order != id order
        st.tuples(st.just("reconcile")),
        st.tuples(st.just("drain"), st.sampled_from([None, "after_claim", "before_commit"])),
    ),
    min_size=1,
    max_size=10,
)


async def _apply(
    db: AsyncEngine, board: int, at: datetime, contents: dict[str, int], *, complete: bool = True
) -> None:
    postings = [posting(j, LOCATIONS[v]) for j, v in contents.items()]
    source, org = BOARDS[board]
    async with make_sessionmaker(db)() as session, session.begin():
        await apply_fetch(session, source, org, postings, at, complete=complete)


async def _apply_pair_out_of_order(
    db: AsyncEngine, at: datetime, first: dict[str, int], second: dict[str, int]
) -> None:
    """Board 0's rows get their ids first but commit LAST: visible-commit order differs from id order."""
    maker = make_sessionmaker(db)
    async with maker() as late:
        async with late.begin():
            await apply_fetch(
                late, *BOARDS[0], [posting(j, LOCATIONS[v]) for j, v in first.items()], at, complete=True
            )  # flushed: ids allocated, nothing visible yet
            await _apply(db, 1, at, second)  # commits while the first transaction is still open
        # exiting late.begin() commits the earlier-allocated ids last


async def _quiesce(db: AsyncEngine, steps: Any) -> None:
    for _ in range(40):
        async with db.begin() as conn:  # cut every lease and backoff short: no real waiting in tests
            await conn.execute(
                text(
                    "UPDATE step_runs SET lease_until = now() - interval '1 second'"
                    " WHERE status IN ('pending', 'running') AND lease_until IS NOT NULL"
                )
            )
        await steps.reconcile()
        await steps.drain()
        async with make_sessionmaker(db)() as session:
            live = await session.scalar(
                text("SELECT count(*) FROM step_runs WHERE status IN ('pending', 'running')")
            )
        if not live:
            return
    raise AssertionError("pipeline did not quiesce in 40 rounds")


async def _check_invariants(db: AsyncEngine, steps: Any) -> None:
    async with make_sessionmaker(db)() as session:
        vacancies = (await session.execute(select(Vacancy))).scalars().all()
        all_runs = (await session.execute(select(StepRun))).scalars().all()
        by_key = {(r.vacancy_id, r.step, r.input_hash): r for r in all_runs}
        assert len(by_key) == len(all_runs)  # the unique key held

        open_ids = {v.id for v in vacancies if v.closed_at is None}
        for r in all_runs:
            assert r.status in ("done", "skipped", "failed"), r.status
            if r.vacancy_id not in open_ids:
                assert r.status != "running"

        for v in vacancies:
            if v.closed_at is not None:
                continue
            desired = await steps._desired(session, v)
            assert set(desired) == {s.name for s in steps.chain}, "liveness: full chain derivable"
            for name, inp in desired.items():
                run = by_key.get((v.id, name, inp.hash))
                assert run is not None and run.status == "done", f"{name} not done for current input"


@settings(max_examples=20, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(ops=operations)
def test_any_interleaving_of_fetches_crashes_and_drains_converges(db_url: str, ops: list[Any]) -> None:
    async def scenario() -> None:
        db = create_async_engine(db_url, poolclass=NullPool)
        try:
            async with db.begin() as conn:
                await conn.execute(
                    text("TRUNCATE llm_calls, step_runs, vacancy_events, vacancies, boards RESTART IDENTITY")
                )
            chain = [FakeStep("fa"), FakeStep("fb")]
            steps = make_steps(db, chain=chain, attempt_cap=100, backoff_base_seconds=0)

            def crash_at(point: str | None) -> Any:
                def failpoint(name: str) -> None:
                    if name == point:
                        raise SimulatedCrash(name)

                return failpoint

            at = T0
            for op in ops:
                at += timedelta(hours=1)
                if op[0] == "fetch":
                    await _apply(db, op[1], at, op[2], complete=op[3])
                elif op[0] == "pair_fetch":
                    await _apply_pair_out_of_order(db, at, op[1], op[2])
                elif op[0] == "reconcile":
                    await steps.reconcile()
                elif op[0] == "drain":
                    crashy = make_steps(
                        db, chain=chain, attempt_cap=100, backoff_base_seconds=0, failpoint=crash_at(op[1])
                    )
                    try:
                        await crashy.drain()
                    except SimulatedCrash:
                        pass  # the worker died; its claims recover via lease expiry

            await _quiesce(db, steps)
            await _check_invariants(db, steps)
        finally:
            await db.dispose()

    asyncio.run(scenario())
