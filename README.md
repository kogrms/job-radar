# job-radar

Personal job-vacancy pipeline: collectors, LLM extraction, a work-eligibility classifier, evals.
Architecture and roadmap: [`docs/architecture.md`](docs/architecture.md).

## Run locally

Requires Docker and [uv](https://docs.astral.sh/uv/).

```sh
./scripts/init_env.sh            # .env with random secrets
docker compose up -d --build     # app, worker, Postgres+pgvector, Redis, self-hosted Langfuse
curl localhost:8000/health       # {"status":"ok",...}
uv run python scripts/langfuse_smoke.py   # one test trace -> http://localhost:3000
```

Tests: `docker compose up -d db redis && uv run alembic upgrade head && uv run pytest`.
