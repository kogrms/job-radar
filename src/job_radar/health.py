"""Dependency checks shared by the API health endpoint and the worker.

Checks return only the exception class name: details (hosts, users, SQL) go to the log, not to HTTP clients.
"""

import asyncio
import logging

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

CHECK_TIMEOUT_S = 2.0
log = logging.getLogger(__name__)


async def check_postgres(engine: AsyncEngine) -> str | None:
    """Return None when healthy, otherwise a short error description."""
    try:
        async with asyncio.timeout(CHECK_TIMEOUT_S), engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as e:  # any failure means unhealthy
        log.warning("postgres health check failed", exc_info=True)
        return type(e).__name__
    return None


async def check_redis(redis: Redis) -> str | None:
    try:
        async with asyncio.timeout(CHECK_TIMEOUT_S):
            await redis.ping()
    except Exception as e:
        log.warning("redis health check failed", exc_info=True)
        return type(e).__name__
    return None
