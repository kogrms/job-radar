"""Queue round-trip: enqueue a job through Redis and run it with a real arq worker in burst mode."""

from arq import create_pool
from arq.connections import RedisSettings
from arq.worker import Worker

from job_radar.config import Settings
from job_radar.worker import ping


async def test_ping_round_trip(settings: Settings) -> None:
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    pool = await create_pool(redis_settings)
    try:
        job = await pool.enqueue_job("ping")
        assert job is not None
        worker = Worker(functions=[ping], redis_settings=redis_settings, burst=True, poll_delay=0.05)
        await worker.async_run()
        await worker.close()
        assert await job.result(timeout=5) == "pong"
    finally:
        await pool.aclose()
