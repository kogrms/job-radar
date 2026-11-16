# Agent log

Mistakes made by the coding agent (Claude Code), how they were caught, and what changed afterwards.
Raw material for the "how I work with coding agents" interview story.

Format: date — what happened — how it was caught — fix — rule going forward.

## 2026-11-05 — import re-ran a destructive script

- **What:** to reuse a helper (`jsonld_job`), the agent ran `from fix_jd import jsonld_job`. `fix_jd.py` had no
  `if __name__ == '__main__'` guard, so the import re-ran the whole cleanup pass. It deleted 4 valid vacancy
  files because its validation rules were stricter than those of the first run.
- **Caught:** by the agent itself, from the output of a command that was only supposed to import; it stopped
  the process.
- **Fix:** `main()` guard in every script; the dataset was rebuilt from scratch with an idempotent `rebuild_jd.py`.
  All files recovered.
- **Rule:** any script that deletes or rewrites data gets a `main()` guard and must be idempotent, so a re-run is
  harmless.

## 2026-11-05 — over-strict validation dropped valid texts

- **What:** a "does the page mention the role words" check plus a 1200-character minimum dropped 5 valid pages
  (Infostud, hh) whose structured JobPosting summary was short.
- **Caught:** while checking the rebuilt dataset by source: known-good Infostud and hh pages were missing.
- **Fix:** separate thresholds — 400 characters for structured (API / JSON-LD) text, 1200 for plain page text;
  a page with a JobPosting block counts as a job page even with a short description.
- **Rule:** after any filtering step, compare counts per source before and after; a drop in a trusted source is a bug.

## 2026-11-11 — PR 2 independent review (reviewer: Claude Sonnet 5.5, read-only session; implementer: Claude Opus 5.5)

Reviewer got the spec and the diff, not the implementer's reasoning. 11 findings; each re-checked by the
implementer before acting.

| # | Finding | Verdict | Action |
|---|---|---|---|
| 1 | Reopened posting with new content emitted `reopened` only, no `changed` → extraction (runs on opened/changed) would never see the new text. The test model had the **same** bug, so property tests passed | **Real bug** (reproduced) | Emit both events; model fixed; mutant "reopen hides change" now fails 2 tests |
| 2 | Concurrency test passes with the `FOR UPDATE` removed | **Real test gap** (reproduced) | Rewritten — see next entry: the first rewrite still missed it |
| 3 | `D.O.O.`, `d.o.o`, `B.V.`, `S.A.` not folded → same company, different keys | **Real bug** (reproduced) | Dotted abbreviations folded; tests added |
| 4 | Cyrillic names normalized to empty string: "Яндекс" and "Озон" got the same key; C++/C#/C collided | **Real bug** (reproduced). Would have hit the hh.ru collector | Unicode tokens kept; C++/C#/F#/.NET mapped to tokens; tests added |
| 5 | "Front-end" ≠ "Frontend", "Dev Ops" ≠ "DevOps" | Real, minor | Compound joins; tests added |
| 6 | Properties compared only hash, open set and event counts | Real test gap | `db_state` now compares every stored column the model predicts (text, payload, dedup_key, first/last seen, closed_at); generator varies the title |
| 7 | No test for the rollback path | Real test gap | Test: rolled-back fetch leaves no trace and its retry is applied, not stale |
| 8 | Out-of-order property never states "open set = newest complete fetch" | Minor test gap | Assertion added for complete-only sequences |
| 9 | `seen_at` semantics and "empty complete fetch closes everything" undocumented | Real, design | Caller contract in `store.py` docstring; guard goes into PR 3 |
| 10 | NUL characters break TEXT/JSONB inserts | Real, low | Moved to PR 3 (collector sanitizes) |
| 11 | Unused partial index; heavy columns loaded; flush per insert; `org` case-sensitive; no `events(kind, at)` index | Mixed | Index dropped, heavy columns deferred; flush-per-insert kept (boards are small); `org` casing → PR 3; events index → PR 4 |

False alarms: none. Counted for the eval table: 11 findings, 5 real bugs, 4 real test gaps, 2 design notes.

## 2026-11-11 — the first lock-test rewrite still could not see a missing FOR UPDATE

- **What:** after finding #2, the implementer rewrote the test so worker A holds its transaction while B waits.
  Mutation check: removing `FOR UPDATE` still passed. Cause: A held the transaction *after* updating the board
  row, and Postgres makes a concurrent `INSERT ... ON CONFLICT` wait on that row anyway. The explicit lock only
  matters in the window between A's `SELECT ... FOR UPDATE` and its first write.
- **Caught:** by re-running the mutation check after the fix, not by the green test run.
- **Fix:** A now takes the lock and holds it before writing anything. Without `FOR UPDATE` the test fails 3/3,
  with it passes 3/3.
- **Rule:** after fixing a test gap, re-run the mutation that exposed it. A green test after a fix proves nothing.

## 2026-11-11 — mutation script mutated the real file and failed to restore it

- **What:** a shell one-liner chained `ruff check && S=<backup dir>; cp ... "$S/..."`. Ruff failed, so `S` stayed
  empty; the backup `cp` failed, the mutation was applied to the real `store.py`, and the restore `cp` failed too.
  Two "real code" test runs were in fact runs against the mutant.
- **Caught:** "cp: /store.orig.py: Read-only file system" in the output and real-code runs failing.
- **Fix:** restored from the previous verified backup after diffing it against the mutant (only the lock line
  differed). Mutation runs now set the backup path first and restore via `trap ... EXIT`.
- **Rule:** never chain setup of a safety net (variables, backups) after a command that can fail; restore in a
  trap; compare the file with the backup at the end.

## 2026-11-11 — false alarm raised by the implementer itself during the code walkthrough

- **What:** while walking the owner through `store.py`, the agent claimed the bulk `UPDATE ... closed_at` left
  stale `closed_at = None` on the in-memory ORM objects, and the owner agreed to a fix.
- **Caught:** the agent checked the claim before editing: SQLAlchemy 2 runs ORM-enabled bulk updates with
  `synchronize_session="auto"`, and a script showed the in-memory object did get the new `closed_at`.
  **Verdict: false alarm.**
- **Change made anyway, labeled as simplification, not a fix:** close postings via `v.closed_at = seen_at`, so every
  write in `apply_fetch` goes through the unit of work and nothing relies on implicit synchronization.
- **Rule:** verify a claimed defect before presenting it as one, even in an explanation. "I think it's stale" goes
  to the owner as a hypothesis, with the check that would settle it.

## 2026-11-11 — an unverified API claim written into upstream-bugs.md in week 0

- **What:** the week-0 note said Greenhouse EU boards need `boards-api.eu.greenhouse.io`. The host does not resolve;
  EU boards are served by the regular API. The week-0 script tried the EU host first and silently fell back, so the
  claim was inferred from code, never observed.
- **Caught:** probing the live APIs before writing the PR 3 collectors (`dig`, `curl`).
- **Fix:** note corrected; collector uses one host.
- **Rule:** a behaviour claim about an external API goes into the docs only with the request that showed it.

## 2026-11-11 — Greenhouse HTML-escaped content (caught by a test, not shipped)

- **What:** the first Greenhouse parser stripped tags and then unescaped entities, so `&lt;p&gt;` became `<p>` in
  the stored text. Same order as the generic HTML helper; Greenhouse is the only API that double-encodes.
- **Caught:** the synthetic fixture built with `html.escape(...)`, mirroring the live response, failed the test.
- **Fix:** unescape first for Greenhouse. Live check after the fix: 0 Greenhouse texts with leftover tags.

## 2026-11-11 — PR 3 independent review (reviewer: Claude Sonnet 5.5 with `.claude/agents/pr-reviewer.md` instructions)

The reviewer found no path from a failed or truncated fetch to `apply_fetch(complete=True)`; it live-checked that
missing boards are 404 on all four ATS and that Lever/Greenhouse return full listings (302 and 783 postings).
New agent definitions are not picked up mid-session, so it ran as a general-purpose agent told to follow the file.

| # | Finding | Verdict | Action |
|---|---|---|---|
| 1 | `BOARDS_FILE=` (empty, as in `.env.example`) parsed as `Path('.')` → `IsADirectoryError` every cron tick | **Real bug** (reproduced) | `env_ignore_empty=True`; test |
| 2A | Mutant `complete=True` in `collect_board` survived: nothing checked that incompleteness reaches storage | **Real test gap** | Test: truncated Greenhouse listing through `collect_board` closes nothing |
| 2B | Shape guards of Greenhouse/Workable/Lever untested; failure table ran on Ashby only | **Real test gap** | Failure table × all four sources (+ bad shape and missing id per ATS) |
| 2C | Mutant "seen_at after the fetch" survived | **Real test gap** | Test: slow older fetch loses to a faster newer one → `stale`, closes nothing |
| 3 | Greenhouse without `meta.total` counted as complete (fail-open) | Real, low likelihood | Missing total → not complete; test |
| 4 | Complete empty listing on a board with open postings closes all of them | Design residual | Decision record: accepted for now, revisit in week 2 |
| 5 | `TooManyRedirects` / `DecodingError` escape as exceptions | Real, minor (fail-safe) | Redirect loop → failed fetch; decoding error retried; tests |
| 6 | Lever `region` not in the board key | NIT, no live case | Decision record |
| 7 | Retry budget close to arq's default 300 s job timeout | Real, minor | `job_timeout = 600` |
| 8 | Spec named `boards.example.yml`; manual `collect_all` in a cron hour queues 0 | NIT | Spec fixed; noted in CLAUDE.md commands |

False alarms: none. Mutation re-check after fixes (in a temp copy): six mutants, each fails at least one test.

## 2026-11-11 — second mutation-script accident: zsh does not word-split `$FILES`

- **What:** `FILES="a b c"; for f in $FILES` is one iteration in zsh. The backup `cp` failed, the restore failed,
  and five mutants accumulated in the real `worker.py`, `greenhouse.py`, `workable.py` (the latter two untracked,
  so git could not restore them). The reported mutation results were invalid.
- **Caught:** `cp: ... No such file or directory` in the output and failure counts growing 1→5 across "independent"
  mutants.
- **Fix:** each mutation reversed by an exact one-occurrence replacement in reverse order; then `ruff format
  --check` (no diff), 72 tests green. The mutation check was redone with a Python script that works on a temporary
  copy of the repo and asserts the working tree is unchanged; a baseline run of the copy passes first.
- **Rule (replaces the 'trap' rule):** mutation checks never touch the working tree — copy the repo to a temp dir,
  mutate there, run a baseline first. Second shell accident of the day: stop using ad-hoc shell loops for anything
  that writes files.

## 2026-11-11 — PR 3b: two problems found only by the live run

- **Old worker, new job signature.** A `collect_board` job with the new `queries` argument was picked up by the
  worker container still running PR 3 code → `TypeError: takes from 4 to 5 positional arguments but 6 were given`.
  Nothing was written. Fix: rebuild the worker before enqueueing jobs with a new signature. Rule added to CLAUDE.md.
  For real deployments: job arguments are an API between versions; add new ones as keyword args with defaults and
  roll workers first.
- **Exact `totalCount` match made Himalayas never complete** (the API counts 0-2 jobs it does not return). All unit
  tests passed: the synthetic fixtures had consistent counters. Found by checking `complete` in the live result, not
  just the counts. Fix and its cost: `docs/decisions.md`.

## 2026-11-11 — PR 3b independent review (reviewer: Claude Sonnet 5.5 with pr-reviewer instructions)

No data loss beyond accepted risks; migration checked up/down on a scratch DB; terms compliance confirmed in code.
The reviewer's own first mutation run was invalid (its copy imported the real repo's code, so every mutant
"survived") — it noticed, discarded it and redid the run. Our mutation script now asserts the copy imports itself.

| # | Finding | Verdict | Action |
|---|---|---|---|
| 1 | Nothing checked the arguments `collect_all` enqueues (dropping `queries` survived) | **Real test gap** | Test inspects queued jobs' args/kwargs |
| 2 | "ATS boards are not throttled by MIN_INTERVAL" untested | **Real test gap** | ATS rows with recent fetch still enqueued |
| 3 | Per-query completeness could be padded by earlier queries' guids | **Real test gap** | Overlap test with a truncated second query |
| 4 | Tolerance upper bound and missing `totalCount` untested | **Real test gap** | Boundary 980/979 of 1000; missing total |
| 5 | A query answering `{"jobs": [], "totalCount": 0}` counted as complete → closes everything only it finds | **Real bug** (reproduced) | Empty first page → incomplete; decision recorded |
| 6 | `queries` enqueued positionally for every board — old workers fail even on ATS jobs | Real, low | Keyword, only when set; test asserts ATS jobs keep the old shape |
| 7 | Incomplete fetch still moves `last_fetch_at` → next Himalayas try ~a day later | Design, accepted | `decisions.md` |
| 8 | Loose config (empty query, queries on ATS, region on Himalayas) | Real, minor | Validation + tests |
| 9 | One malformed job raised out of the task; page delay untested | Real, minor | Fails the fetch cleanly; delay test |

False alarms: none. After fixes: 90 tests; eight mutants (on a temp copy, baseline first), each caught.

## 2026-11-12 — branch from main could not run tests: the shared DB was migrated by another branch

- **What:** the eligibility branch started from `main`, but the dev and test databases were already at the
  Himalayas migration from `pr3b-himalayas`. Alembic: "No such revision '6ba46df4d571'"; 10 test errors.
- **Fix:** rebased the uncommitted work onto `pr3b-himalayas` (which merges first anyway).
- **Rule:** one local Postgres serves every branch. Start a new branch from the branch with the newest migration,
  or downgrade both DBs before switching.
