"""Child process for the kill -9 test: claims a step run and works on it until killed.

Run as `python tests/steps_kill_child.py <db_url>`; SLEEPY_SECONDS makes the step hang.
"""

import asyncio
import sys

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from steps_kill_helper import SleepyStep

from job_radar.db import make_sessionmaker
from job_radar.steps.engine import Engine


async def main(url: str) -> None:
    db = create_async_engine(url, poolclass=NullPool)
    engine = Engine(
        make_sessionmaker(db),
        [SleepyStep()],
        lease_seconds=2,
        batch_size=1,
        attempt_cap=5,
        hard_timeout_seconds=120,
        backoff_base_seconds=1,
    )
    await engine.drain()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
