# Decisions

Non-obvious choices with the alternatives we rejected. Format: context — options — decision — cost.

## 2026-11-05 — build for own use, not as a product

- **Context:** market check of the job-search pipeline idea.
- **Options:** sell it; open-source it as a career-ops competitor; build it for own job search.
- **Decision:** own use + portfolio. career-ops (80k stars) already owns "signal over volume", Hirify and
  job boards sell geo-filters cheaply.
- **Cost:** no users or revenue; differentiation has to come from engineering and measurement (evals,
  eligibility benchmark, durable workflow).

## 2026-11-05 — arq on Redis for the task queue

- **Context:** need a job queue for collectors and LLM steps.
- **Options:** `procrastinate` (Postgres-only, one less service); `arq` on Redis; Celery.
- **Decision:** `arq`. Async-native, small, and Redis is explicitly required in the fullstack vacancies we target.
- **Cost:** one more service in compose; queue state lives outside Postgres, so per-step state must be stored in
  Postgres separately.

## 2026-11-05 — eligibility classifier has an explicit "unknown" outcome

- **Context:** wrong "eligible" wastes an application; wrong "not eligible" silently loses a fitting role.
- **Options:** binary yes/no with a threshold; yes / no / unknown with unknown routed to a human.
- **Decision:** three outcomes. The benchmark (`eligibility_labels.csv`) has an `unclear` class for this.
- **Cost:** more items reach the human; the metric is precision/recall on yes/no plus the unknown rate.

## 2026-11-05 — personal data stays out of this repo

- **Context:** the repo may become public as a portfolio piece.
- **Decision:** applications, vacancy texts and labels live in `/Users/k/job-search/`; the repo reads them by path
  (later: by env var) and ships only synthetic fixtures.
- **Cost:** evals need the private data dir to run locally; CI runs on synthetic fixtures only.

## 2026-11-11 — labeling rules for the eligibility benchmark

- **Context:** 191 location wordings in `/Users/k/job-search/_support/eligibility_labels.csv` were hand-labeled by the
  owner; 16 inconsistent labels were then reconciled into the rules below. The question per row:
  "Can I take this job while living in Serbia with a Russian passport, without moving?" — judged from the text only.
- **Rules:**
  - Serbia named (alone, in a country list, Belgrade office) → `yes`. Novi Sad / Kraljevo office → `no` (owner's commute constraint).
  - Worldwide / anywhere / global → `yes`. Explicit "candidates from Russia considered" → `yes`.
  - Time zone band that covers Serbia → `yes`.
  - Region only (Europe, EMEA, "Countries of Europe or Ukraine") → `unclear`: for a Russian passport a region often hides
    an entity or citizenship restriction.
  - Bare "Remote" with no geography → `unclear`.
  - EU / named country list without Serbia / US-only / office elsewhere → `no`. Relocation offers → `no` with note `reloc`.
- **Result:** yes 102 · no 45 · unclear 44.
- **Cost:** the `unclear` class is large (23%), so the classifier will send many items to the human; that is intended.
  Region-level wordings may turn out to be fine in practice — the bot labels from week 2 will show it.

## 2026-11-11 — Langfuse v4 self-hosted, separate from our own Postgres and Redis

- **Context:** the plan said "self-hosted Langfuse"; the current release is v4 (4.78.0). It needs Postgres,
  ClickHouse, Redis and S3-compatible storage (MinIO).
- **Options:** share our Postgres/Redis with Langfuse; run Langfuse's own; use Langfuse Cloud.
- **Decision:** Langfuse's own services in `compose.langfuse.yml` (trimmed official file, `langfuse-` prefix),
  included from `compose.yml`. Headless init creates the org, project and API keys from `.env`.
- **Cost:** ~6 extra containers; the Docker VM has 8 GB and copes. Upside: ClickHouse comes for free —
  Langfuse v4 stores every span in `events_full`, so cost/latency analytics can be plain ClickHouse SQL.
- **Gotcha:** v4 runs in events-only mode: `GET /api/public/traces/{id}` is gone; read traces in the UI,
  via ClickHouse, or the newer API.

## 2026-11-11 — agent authorship is visible in git history

- **Context:** the project is evidence for roles that ask how you work with coding agents and what your personal
  contribution is. A history where every commit looks hand-written contradicts that story.
- **Options:** owner-only authorship, no marks; owner as author + `Co-Authored-By` for agent-written commits;
  a separate bot account authoring commits and PRs.
- **Decision:** owner as author + `Co-Authored-By` trailer naming the model that wrote the code. Hand-written
  commits carry no trailer, so `git log` shows the split. Reviewer models are not co-authors.
- **Cost:** some conservative teams may read AI co-authorship negatively; a bot account would show the
  agent-proposes / human-approves flow more literally, but costs a day of setup.
