# Week 1 — skeleton, ATS collectors, extraction

Plan: `/Users/k/job-search/ai-engineer-plan.md`, "Неделя 1". Process: `CLAUDE.md`, "How we develop".

**Done when:** a vacancy travels collector → queue → LLM → Postgres, and Langfuse shows cost and latency for each LLM call.

**Out of scope this week:** hh / Telegram / Djinni collectors, eligibility classifier, scoring, the per-step state
machine (week 2), Telegram bot, UI, local models (Ollama, week 4), Inspect AI (week 4).

## Environment facts (checked 2026-11-11)

- Docker 29.1.3, 18 GB RAM (8 GB given to the Docker VM) — enough for self-hosted Langfuse v4 next to our services.
- `uv` is not installed; system `python3` is 3.9. PR 1 installs `uv` and pins Python 3.12 via `uv python`.
- Private data (`/Users/k/job-search/_support/`): 76 vacancy texts, 73 of them have an eligibility label
  (yes 43 · unclear 18 · no 12), 46 of those 73 have a verbatim location quote. Sources of the 76: Ashby 24,
  Greenhouse 13, Lever 8, others 31.

## PR chain

Each PR branches from the previous one. Risky PRs (marked ⚠) get an independent read-only review before merge.

### PR 1 — project skeleton

- `uv` project, Python 3.12, `src/job_radar/` layout; `ruff` + `pytest` + `mypy --strict` on `src/`.
- `pydantic-settings` config from env; `.env.example` lists every variable, `.env` is git-ignored.
- `docker-compose.yml`: Postgres 17 with pgvector, Redis, app (FastAPI), worker (arq), self-hosted Langfuse v4
  with the services it needs (it does run its own ClickHouse — see `decisions.md`).
- FastAPI app with `GET /health` that checks Postgres and Redis.
- Alembic wired to async SQLAlchemy 2.0 (asyncpg); empty first migration.
- GitHub Actions: lint, types, tests on synthetic fixtures only (no private data in CI).

**Status:** done 2026-11-11 on branch `pr1-skeleton` (not committed yet). 3 tests pass; `/health` 200; Langfuse 4.78.0
up; smoke trace found in ClickHouse `events_full`. CI file written but not run: no GitHub remote yet.

**Verify:** `docker compose up` → `/health` returns 200 with both checks green; Langfuse UI opens and a test
trace from a one-off script shows up; CI passes.

### PR 2 — vacancy storage and idempotent upsert ⚠

Tables (names may change in review):

- `vacancies`: `id`, `source` (ashby / greenhouse / lever / workable), `source_org`, `source_job_id`, `url`,
  `title`, `company`, `location_raw`, `raw_text`, `raw_payload` (jsonb), `content_hash`, `first_seen_at`,
  `last_seen_at`, `closed_at`. Unique on (`source`, `source_org`, `source_job_id`) — the canonical id.
- `vacancy_events`: `vacancy_id`, `kind` (opened / changed / closed / reopened), `at`, `content_hash`.

Behaviour:

- `upsert_postings(source, org, postings, seen_at)` in one transaction: insert new (event `opened`), bump
  `last_seen_at` for unchanged, update + event `changed` when `content_hash` differs, reopen if it was closed.
- Postings of that board that were open but are missing from a **complete** board fetch → `closed_at` + event
  `closed`. A partial or failed fetch never closes anything.
- `raw_text` and `raw_payload` are stored at first sight and on every change (lesson from week 0).
- Cross-source duplicates (same job on two ATS) are **not** merged this week; only a `dedup_key`
  (normalized company + title) column is stored and tested, merging comes later if needed.

Tests (`hypothesis`, against a real Postgres in docker):

- applying the same batch N times gives the same rows and exactly one `opened` event per posting;
- any interleaving of complete fetches ends in the same final state as applying the last fetch;
- a failed/partial fetch changes nothing about `closed_at`;
- `dedup_key` is stable under case, whitespace, punctuation, accents, legal suffixes and seniority spellings
  ("Sr." = "Senior"), but "Senior X" and "X" stay different jobs.

Added during implementation: a `boards` table whose row lock serializes concurrent fetches of one board and whose
`last_fetch_at` makes older fetches (late retries) stale, so out-of-order arrival cannot regress state.

**Verify:** tests green; reviewer finds no way to get a duplicate row or a spurious close.

**Status:** implemented 2026-11-11 on `pr2-storage`, independently reviewed (11 findings, see `agent-log.md`),
fixes applied. 18 tests pass. Property tests compare every stored column with a reference model. Mutation check:
partial fetch closes / no stale guard / never clears `closed_at` / no row lock / reopen hides change — each fails
at least one test. A reopened posting with new content emits both `reopened` and `changed`.

**Carried into PR 3:** sanitize NUL characters; never report an error page or empty body as a complete fetch;
decide whether `org` is case-insensitive per ATS and normalize it. **Into PR 4:** index `vacancy_events(kind, at)`
for polling.

### PR 3 — ATS collectors

- One interface: `fetch_board(org) -> BoardFetch(postings, complete: bool)`.
- Ashby, Greenhouse (US + EU API), Lever (US + EU API), Workable — public board APIs; logic ported from
  `/Users/k/job-search/_support/jd_tools/fetch_jd.py`, which already handles the EU endpoints.
- `httpx.AsyncClient` with timeouts, retries with backoff on 429/5xx, a polite User-Agent with no personal data.
- arq task `collect_board(source, org)` → `upsert_postings`; arq cron runs all boards every N hours.
- Board list from a YAML file outside the repo (path in env); the repo ships `examples/boards.yml`.
- Tests on recorded HTTP responses (`respx`), one fixture per ATS, including an empty board and a 429.

**Status:** implemented 2026-11-11 on `pr3-collectors`. Probed the live APIs first: Ashby/Greenhouse ignore org
case, Lever/Workable do not; missing boards are 404 everywhere; Greenhouse EU boards use the regular API host;
Greenhouse `meta.total` gives a completeness check. Failure rule: 404, network errors after retries, non-JSON,
empty body, unexpected shape, a job without an id → failed fetch, storage not called. Live run on the 34 boards from
past applications through the queue: 2026 vacancies; second run 34/34 applied with 0 events. Worker container gets
only `boards.yml`, read-only. Fixtures are synthetic (no real company data); the board list stays private.
Independent review: 10 findings (1 config bug, 3 test gaps from surviving mutants, fail-open Greenhouse total,
redirect/decoding errors, timeout) — all handled or recorded in `decisions.md`. 72 tests; six mutants, each caught.

**Verify:** run against 3 real boards → rows appear; second run → 0 new rows, 0 events; a board whose fetch
fails → nothing closed.

### PR 4 — structured extraction ⚠

- Pydantic schema `VacancyFields`: title, seniority, location quote (verbatim substring of the text), workplace
  type (remote / hybrid / onsite / unknown), countries and regions mentioned, salary (min / max / currency /
  period, all optional), stack keywords, language of the posting.
- One LLM call with structured output; the vacancy text is untrusted input: no tools, text passed as data, not
  instructions.
- On invalid output: one retry with the validation error attached; after that store the failure. Counters for
  invalid outputs and retries, exported to Langfuse as scores.
- The location quote is checked to really be a substring of `raw_text`; a non-substring counts as invalid.
- Table `extractions`: `vacancy_id`, `content_hash`, `model`, `prompt_version`, `fields` (jsonb) or `error`,
  `cost`, `latency_ms`, `created_at`. Re-extraction happens only when `content_hash` or `prompt_version` changes.
- arq task `extract(vacancy_id)` is enqueued on `opened` and `changed` events.
- Every call traced in Langfuse with model, tokens, cost, latency, prompt version.

**Verify:** collector → queue → extraction → row in `extractions`, trace visible in Langfuse with cost.
Reviewer focus: idempotency of `extract`, what happens on provider errors, prompt-injection surface.

### PR 5 — first extraction eval

- Loader reads the 76 private texts from `JOB_RADAR_PRIVATE_DIR` (never copied into the repo).
- Runs extraction on all 76, writes a report to the private dir and a summary table to stdout:
  - valid-output rate, retry rate;
  - location quote: on the 46 rows with a verbatim label, does the extracted quote cover the labeled one
    (exact / overlapping / missed);
  - workplace type vs. what the labeled wording says (manual spot check of disagreements, not a metric yet);
  - cost total and per vacancy, latency p50 / p95.
- The same 76 items are pushed as a Langfuse dataset so later runs are comparable.

**Verify:** one command reproduces the numbers; the numbers go into `docs/decisions.md` as the week-1 baseline.

## Open questions

1. **LLM provider and model for PR 4–5.** Needs an API key and a budget. Decided before PR 4 starts.
2. Langfuse self-hosted vs. cloud: self-hosted is the plan; fall back to cloud only if the v3 stack is too heavy
   for the laptop.
