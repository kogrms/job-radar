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

## 2026-11-11 — one flush per new posting in apply_fetch

- **Context:** each new vacancy needs its `id` before its `opened` event can reference it.
- **Options:** `flush()` per new posting; one batched `INSERT ... RETURNING id` for all new postings.
- **Decision:** flush per posting. Boards have tens to hundreds of postings and almost all of them are new only on
  the first fetch; the code stays a plain unit-of-work loop.
- **Cost:** N round trips on a board's first fetch. Revisit if a board brings thousands of new postings at once.

## 2026-11-11 — a complete empty listing closes the whole board (accepted for now)

- **Context:** PR 3 review: if an ATS returns a valid empty list during an incident, every open posting of that board
  gets a `closed` event, then `reopened` on the next poll.
- **Options:** accept; require two consecutive empty listings before closing (a flag on `boards`); treat "empty while
  postings are open" as incomplete always.
- **Decision:** accept for now. Missing boards answer 404 (checked on all four ATS), so the remaining case is an ATS
  incident; the damage is churn of closed/reopened events, which nothing consumes yet.
- **Revisit:** in week 2, when closures start to matter (digest, per-step state). The two-empty-listings rule is the
  likely fix; it belongs in `apply_fetch` under the board lock, with the property-test model updated.

## 2026-11-11 — Lever region is not part of the board key

- **Context:** a board is `(source, org)`; Lever US and EU are different hosts.
- **Decision:** keep. One Lever site name lives in one region; the config says which. If the same name ever exists in
  both, add `region` to the key and to `collect_board`'s job id.

## 2026-11-11 — Himalayas completeness: tolerate a small totalCount gap

- **Context:** live, every query was read to an empty last page with no duplicates, yet `totalCount` was 0-2 above
  the unique jobs returned (python 638/636, ai engineer 475/474, typescript 229/227, llm 190/190, machine learning
  175/175). With an exact-match rule the fetch was never complete, so Himalayas postings would never close.
- **Options:** exact match (never closes); ignore `totalCount`; tolerance; close by checking each absent job via the
  single-job endpoint.
- **Decision:** complete = every query read to its empty page and unique ≥ total − max(3, 2%). A larger gap still
  means truncated or shifted results → incomplete.
- **Cost:** a job that moves between pages during the read can be closed falsely and reopened next day — the same
  churn already accepted for empty ATS listings. If closures start to matter (week 2), verify absent jobs via
  `/jobs/api/companies/:company/jobs/:slug` before closing.

## 2026-11-11 — Himalayas: what makes a fetch incomplete, and two consequences (PR 3b review)

- A query whose first page is empty makes the fetch incomplete: a broad query with zero results is likelier a
  transient or edge-cached empty answer than a real empty market, and trusting it would close every job only that
  query finds. Consequence: a query that legitimately returns nothing blocks closures — remove such queries.
- Each query is checked against its own `totalCount` with its own unique count, not the union so far.
- **Removing or editing a query** in `boards.yml` makes the next complete fetch close the jobs only that query found.
  Correct by definition (the board is "union of current queries"), but expect a burst of `closed` events.
- `last_fetch_at` moves on incomplete fetches too, so after an incomplete Himalayas fetch the next try is ~a day
  later (MIN_INTERVAL 20 h + 6 h cron). Accepted: nothing is lost, closures are just delayed. A separate
  "last complete fetch" column would fix it if it ever matters.

## 2026-11-12 — rule-based eligibility baseline: the number an LLM has to beat

- **Context:** no LLM key yet; the 191 hand-labeled location wordings exist. A transparent baseline makes the
  later LLM claim measurable ("rules give X, the model gives Y at cost Z").
- **Rules:** `src/job_radar/eligibility/rules.py`, written from the labeling rules above (2026-11-11), first match
  wins. Evaluation: `scripts/eval_eligibility.py` (reads the private CSV).
- **Results (all 191 / verbatim quotes only, 119):**
  - first run, before looking at errors: accuracy 90.6% / —; fitting jobs dropped (gold yes → no) 5; wasted
    applications (gold no → yes) 1; sent to a human 42.
  - after three general fixes (Russian case forms of "Serbia"; relocation is "no" only when remote is not
    offered; dropped two over-broad patterns of my own: "international", "time zones close to Europe"):
    accuracy 91.1% / 93.3%; dropped 4 / 2; wasted 1 / 1; sent to a human 44 / 35.
- **Caveat — these numbers are optimistic.** The rules were written by someone who had seen all 191 rows, and the
  first-run errors were looked at before the fixes. This is in-sample. A fair number needs wordings nobody has
  seen: a fresh held-out set from the live database (ATS + Himalayas `location_raw`), labeled before any rule or
  prompt is tuned on it.
- **Known limitation, kept as an xfail test:** "100% remote, or relocation to London or Dubai" → "no", because the
  cities read as the allowed locations. Context like this is where an LLM should win.
- **Paraphrased rows** carry the owner's notes ("not stated whether Serbia is ok"), which trigger the Serbia rule;
  that is why the verbatim-only number is reported separately.

## 2026-11-16 — held-out eligibility set from the live database

- **Why:** the 2026-11-12 baseline numbers are in-sample. A fair number needs wordings neither the owner nor the
  rules author had seen, labeled before anything is tuned on them.
- **Sampling** (`scripts/sample_heldout.py`, seed 20261112): distinct `location_raw` strings of open vacancies,
  minus wordings already in the 191-row set; 15 from Himalayas, 45 from ATS boards split by each source's count of
  distinct wordings (ashby 22, greenhouse 14, lever 8, workable 1). Each row keeps `n_postings`, so results can be
  reported per wording and per posting.
- **Freeze:** the sha256 of `rules.py` at sampling time is stored next to the private CSV. The rules are not edited
  until the held-out number is computed; any later fix gets its own run and is reported as such.
- **Known gap:** see `docs/tasks/0004` — two Himalayas groups did not make it into the random sample.
- **Different input shape:** live ATS strings are structured fields ("Berlin; Hybrid; Remote"), not quotes from
  the vacancy text like most of the 191. A drop in accuracy here partly measures that shift, not only overfitting.

## 2026-11-16 — task cards as files, no task board

- **Context:** another team's agentic workflow adds an orchestration layer over all tasks (a board; agents file
  new cards for bugs they find). The useful part for us is "a found bug becomes a card, not a chat message".
- **Options:** a board UI over the task list; an issue tracker (GitHub Issues); markdown files in the repo.
- **Decision:** `docs/tasks/NNNN-slug.md` with a status header. One PR chain runs at a time, so a board would show
  one column of cards. Files are read by the agent with no API, are versioned with the code, and work offline.
- **Gave up:** a visual overview and cross-task orchestration. Revisit in week 7 if time is left.
- Same day: spec review before code was added to the workflow (step 1 in `CLAUDE.md`, spec-review mode of
  `pr-reviewer`).
