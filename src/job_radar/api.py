"""HTTP API. Week 1: health only."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from redis.asyncio import Redis

from job_radar.config import Settings, get_settings
from job_radar.db import make_engine
from job_radar.health import check_postgres, check_redis


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.engine = make_engine(settings.database_url)
        app.state.redis = Redis.from_url(settings.redis_url)
        try:
            yield
        finally:
            await app.state.redis.aclose()
            await app.state.engine.dispose()

    app = FastAPI(title="job-radar", lifespan=lifespan)

    @app.get("/health")
    async def health(request: Request) -> JSONResponse:
        errors: dict[str, str | None] = {
            "postgres": await check_postgres(request.app.state.engine),
            "redis": await check_redis(request.app.state.redis),
        }
        body: dict[str, Any] = {
            "status": "ok" if not any(errors.values()) else "degraded",
            "checks": {name: err or "ok" for name, err in errors.items()},
        }
        return JSONResponse(body, status_code=200 if body["status"] == "ok" else 503)

    return app


app = create_app()
