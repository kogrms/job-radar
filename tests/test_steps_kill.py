"""The done-when check with a real SIGKILL (spec PR 6): a worker subprocess dies mid-step; a fresh drain
finishes the vacancy from its last completed step within two minutes (here: one 2 s lease + a drain)."""

import asyncio
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from job_radar.db import make_sessionmaker
from job_radar.store import apply_fetch
from tests.steps_kill_helper import SleepyStep
from tests.test_steps_engine import make_steps, posting, runs

RECOVERY_BUDGET_S = 120  # the spec's "recovery takes <= 2 minutes, not arq's 10"


def test_kill_9_mid_step_recovers_within_the_budget(db_url: str) -> None:
    async def prepare() -> None:
        db = create_async_engine(db_url, poolclass=NullPool)
        try:
            async with db.begin() as conn:
                await conn.execute(
                    text("TRUNCATE step_runs, vacancy_events, vacancies, boards RESTART IDENTITY")
                )
            async with make_sessionmaker(db)() as session, session.begin():
                await apply_fetch(
                    session,
                    "ashby",
                    "acme",
                    [posting("a", "Remote, Serbia")],
                    datetime(2026, 11, 1, tzinfo=UTC),
                    complete=True,
                )
            await make_steps(db, chain=[SleepyStep()]).reconcile()
        finally:
            await db.dispose()

    async def poll(query: str, want: int, budget: float) -> None:
        db = create_async_engine(db_url, poolclass=NullPool)
        try:
            deadline = time.monotonic() + budget
            while time.monotonic() < deadline:
                async with make_sessionmaker(db)() as session:
                    if await session.scalar(text(query)) == want:
                        return
                await asyncio.sleep(0.2)
            raise AssertionError(f"timed out waiting for: {query}")
        finally:
            await db.dispose()

    async def recover() -> None:
        db = create_async_engine(db_url, poolclass=NullPool)
        try:
            steps = make_steps(db, chain=[SleepyStep()], lease_seconds=2)
            deadline = time.monotonic() + RECOVERY_BUDGET_S
            while time.monotonic() < deadline:
                if await steps.drain() == {"done": 1}:
                    break
                await asyncio.sleep(0.5)  # the dead worker's lease has not expired yet
            else:
                raise AssertionError("did not recover within the budget")
            (row,) = await runs(db)
            assert (row.status, row.attempts) == ("done", 2)  # the killed claim plus the recovery claim
            assert row.result == {"ok": True}
        finally:
            await db.dispose()

    asyncio.run(prepare())

    child = subprocess.Popen(
        [sys.executable, str(Path(__file__).with_name("steps_kill_child.py")), db_url],
        env={**os.environ, "SLEEPY_SECONDS": "300"},
        cwd=Path(__file__).parent.parent,
    )
    try:
        asyncio.run(poll("SELECT count(*) FROM step_runs WHERE status = 'running'", want=1, budget=20))
        os.kill(child.pid, signal.SIGKILL)  # mid-step, lease held, nothing committed
        child.wait(timeout=10)
    finally:
        if child.poll() is None:
            child.kill()

    os.environ["SLEEPY_SECONDS"] = "0"
    try:
        asyncio.run(recover())
    finally:
        os.environ.pop("SLEEPY_SECONDS", None)
