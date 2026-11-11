# Architecture — what job-radar is at the end of week 7

A personal pipeline that finds vacancies, decides which ones are worth the owner's time, and prepares the
application. The owner makes every decision and submits every application by hand: there is no auto-apply and
no browser automation.

Source plan: `/Users/k/job-search/ai-engineer-plan.md`. Week numbers below (W1–W7) refer to it.

## Big picture

```
 SOURCES                      PIPELINE (arq workers on Redis, state in Postgres)                    HUMAN
 ───────                      ─────────────────────────────────────────────────                     ─────
 Ashby  ─┐                  ┌──────────┐   ┌────────────┐   ┌─────────────┐   ┌──────────┐
 Greenh. ─┤  collectors     │ 1 extract│──▶│2 eligibility│──▶│ 3 score vs  │──▶│ 4 digest │──▶ Telegram bot
 Lever  ─┼─▶ (W1) ──upsert─▶│  fields  │   │ yes/no/     │   │   CV        │   │  + HITL  │    Next.js UI (W6)
 Workable┤   canonical id   │  (W1)    │   │ unknown(W2) │   │ cascade:    │   │  (W2)    │      │
 hh.ru  ─┤   open/close     └──────────┘   └─────┬───────┘   │ local→strong│   └────┬─────┘      │ "apply"
 TG chan.┘   events                              │unknown    │ (W2, W4)    │        │            ▼
                                                 └──────────▶└─────────────┘        │   ┌────────────────────┐
                                                   to human                         └──▶│ 5 letter agent     │
                                                                                        │ RAG over cv.md,    │
                                                                                        │ log.md, past apps  │
                                                                                        │ claims → citations │
                                                                                        │ SSE streaming (W3) │
                                                                                        └─────────┬──────────┘
                                                                                                  ▼
                                                                                   owner edits, submits by hand

 CROSS-CUTTING
 Langfuse (W1)   every LLM call: prompt version, tokens, cost, latency; eval datasets
 Evals (W1, W4)  extraction, eligibility benchmark, scoring set, letter judge; Inspect AI; CI regression gate
 Safety (W4)     vacancy text is untrusted: no tools in extraction, prompt-injection set in CI, PII masking
 MCP server (W5) Claude Code talks to the vacancy DB: search, get, explain score, propose decision (write via HITL)
```

## Components

| Component | What it does | Tech | Week |
|---|---|---|---|
| Collectors | Poll public ATS board APIs (later hh.ru, Telegram channels). Store raw text at first sight. Emit opened / changed / closed events | httpx, arq cron | W1 (ATS), later others |
| Vacancy store | One row per canonical id (`source`, `org`, `job_id`), idempotent upsert, event log, `dedup_key` across sources | Postgres, SQLAlchemy 2 async, Alembic | W1 |
| Extraction | Text → typed fields (location quote, workplace type, countries, salary, stack). Validated, one retry, invalid rate tracked | Pydantic, LLM structured output | W1 |
| Eligibility classifier | "Can the owner take this from Serbia with a Russian passport?" → yes / no / unknown. Unknown goes to the human | LLM + rules; benchmark of 191 hand-labeled wordings | W2 |
| Scoring | Fit against the CV. Cheap local model filters first, strong model scores the rest | Ollama (Qwen 7–8B) + API model | W2, cascade W4 |
| Workflow state | Each step's status, result and error stored separately; steps idempotent; resumes after `kill -9` | Postgres + arq | W2 |
| Telegram bot | Daily digest, buttons apply / skip / why; every decision becomes a label; 10–20% shown unfiltered | python-telegram-bot or aiogram | W2 |
| RAG | Chunks of `cv.md`, `log.md`, past application folders; hybrid search + reranking; recall@k | pgvector + Postgres FTS | W3 |
| Letter agent | Writes a cover letter and screening prep; every claim cites a chunk; unsupported-claim rate measured | LLM with tools, SSE | W3 |
| Evals | Frozen scoring set, eligibility benchmark, model table (accuracy × p50/p95 × cost × invalid rate), LLM judge, CI gate | Inspect AI, Langfuse datasets, GitHub Actions | W1 baseline, W4 |
| MCP server | Tools: `search_vacancies`, `get_vacancy`, `explain_score`, `propose_decision` | MCP Python SDK | W5 |
| LangGraph variant | The letter step re-implemented on LangGraph to compare with our own orchestrator | LangGraph, PostgresSaver, `interrupt()` | W5 |
| Web UI | Inbox with score explanations, same HITL buttons as the bot, streaming letter with abort that reaches the LLM call | Next.js App Router, TypeScript | W6 |
| Operations | Provider rate limits with backoff, fallback model, timeouts; `docker compose up` in 10 minutes | — | W6 |

## Data

- **Public repo:** code, synthetic fixtures, docs. **Private dir** (`/Users/k/job-search/`): past decisions,
  vacancy texts, labels, CV. The repo reads it by path from env; CI never sees it.
- **Postgres tables (grow by week):** `vacancies`, `vacancy_events` (W1) → `extractions` (W1) → `step_runs`,
  `decisions` / labels (W2) → `chunks` with embeddings (W3) → `eval_runs` (W4).

## What the finished repo shows

1. `docker compose up` brings up everything, Langfuse included.
2. README with this diagram, the eval table, "what broke and how it was fixed", own orchestrator vs. LangGraph.
3. Published eligibility benchmark.
4. 2–3 minute demo video including `kill -9` mid-workflow.
