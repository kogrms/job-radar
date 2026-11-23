# Agent log

Mistakes made by the coding agent (Claude Code), how they were caught, and what changed afterwards.
Raw material for the "how I work with coding agents" interview story.

Format: date — what happened — how it was caught — fix — rule going forward.

## Review scoreboard

One row per independent review; counts from the review tables below. **Escaped** = a defect found later in
code that this review had passed (live run, owner, a later review). It is the number that says where review
is weak: an area that keeps escaping gets its own reviewer lens. Pre-review catches (tests, live runs before
the review) are not escapes.

| Date | PR | Reviewer | Findings | Real bugs | Test gaps | Design / NIT | False alarms | Escaped later |
|---|---|---|---|---|---|---|---|---|
| 2026-11-11 | 2 storage | Sonnet 5.5 | 11 | 3 | 4 | 4 | 0 | 0 known |
| 2026-11-11 | 3 ATS collectors | Sonnet 5.5 | 10 | 3 | 3 | 4 | 0 | 0 known |
| 2026-11-11 | 3b Himalayas | Sonnet 5.5 | 9 | 4 | 4 | 1 | 0 | 0 known |
| 2026-11-16 | 5 eligibility rules | Sonnet 5.5 | 13 | 9 | 1 | 3 | 0 | 0 known |
| 2026-11-16 | 6 step pipeline | Sonnet 5.5 | 17 | 3 | 3 | 11 | 0 | 0 known |
| 2026-11-17 | 7 LLM client + extract | Sonnet 5.5 | 9 | 5 | 2 | 2 | 0 | 0 known |
| 2026-11-16 | spec week 2 (draft 1) | Sonnet 5.5 | 12 groups, ~40 items | 2 design bugs (outbox cursor loses events; arq redelivery makes kill -9 recovery 10 min) | — | 5 gaps, 2 contradictions, scope, security | 0 | — |

Caveat: "0 known" escapes because nobody was looking until 2026-11-16. From now on every later-found defect in
reviewed code is logged here against the review that passed it. All four reviews used one model family, the
implementer's (docs/tasks/0002).

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

## 2026-11-16 — eligibility baseline independent review (reviewer: Claude Sonnet 5.5 with pr-reviewer instructions)

Run as a general-purpose subagent reading `.claude/agents/pr-reviewer.md`: the project agent type is not loaded
when the session starts outside the repo. ~450 hand-built strings, every live wording, 34 mutants on a temp copy
(baseline first, import path checked). Every finding below was reproduced by the implementer before a verdict.

| # | Finding | Verdict | Action |
|---|---|---|---|
| A1 | `[+-−]` is a code-point range, not three signs: en dash / non-breaking hyphen in "UTC –1 to UTC +1" → `ValueError` | **Real bug, crash** | Signs listed explicitly (`[+\-−–‑]`); parser cannot fail by construction; fuzz-ish test |
| A2 | Silent drops (yes → no): "non-EU", "not US", "work with us" (`\busa?\b` with IGNORECASE), "no Serbian language required", "except Belarus", first TZ range only | **Real bugs** | Negated geography stripped first; `US` case-sensitive; `serbia\b`; Belarus and Russia-as-location exclusions no longer "no" (owner ruling); every band checked |
| A3 | Wasted applications (no → yes): "anywhere in the US", "global team, US only", "not worldwide", "Serbia excluded", "candidates from Russia are not accepted", "US, CET overlap" | **Real bugs** | "anywhere in X" and "not worldwide" stripped; "global" + a place → unclear; wider Serbia negation; "from Russia … not accepted" is a location exclusion → neutral; CET + a country → unclear |
| A4 | Relocation rule fires on "no relocation assistance"; `\bremote\b` misses "remotely" | **Real bug** | "no relocation …" stripped; `remote(ly)` |
| A5 | Novi Sad spelling variants, "remote" as a substring escape | Real, low (safe direction) | `novi[\s-]*sad`; substring escape left as is |
| B1 | Eval crashes on empty / unlabeled / malformed CSV (ZeroDivisionError, KeyError) | Real, medium | Clear errors for missing file/columns, no labeled rows, bad `n_postings` |
| B2 | Labels like " Yes" silently dropped; half-labeled file gives a confident report | **Real bug** (could mislead a result) | Labels normalised; skipped rows counted and printed |
| B3 | Freeze check silently skipped without `.meta` | Real, low | Held-out files without `.meta` say "freeze NOT checked" |
| B4 | Sampler can draw one wording twice from two sources | Real, did not bite this time | Cross-source dedupe (posting counts summed); all earlier held-out batches excluded |
| B5 | `_allocate` edge cases, overwrite guard not atomic, name shadowing | NIT | Allocation validates totals; `open("x")` for CSV and `.meta`; renamed counter |
| C | One synthetic case per rule; 13 surviving mutants; no test of any wrong-answer input; two loose `!=` asserts | **Real test gap** | Tests now run the 100-row challenge set; 43 mutants on a temp copy, 0 survive (2 survivors were dead patterns — deleted) |
| D | No personal data in tracked files | OK | — |
| E | "0 costly errors" on 60 rows says nothing about wording shapes absent from the sample | Real, framing | decisions.md caveat |

False alarms: none. Lesson for the implementer (me): the held-out sample confirmed the rules on the shapes it
happened to contain; an adversarial set built by a second model found more costly errors in 5 minutes than the
labeled sample did. Both are needed.

## 2026-11-16 — a company name from the owner's applications reached a pushed commit

- **What:** `docs/decisions.md` quoted a private label row that names a payroll platform ("B2B via <platform>").
  The same company is in the owner's applications. The pre-commit scan printed the hit, but the scan and
  `git commit && git push` were chained in one command, so nothing stopped on it. Commit 0d4ce64, pushed to
  the private repo.
- **Fix:** the quote paraphrased in a follow-up; history rewrite only with the owner's OK.
- **Rule:** the personal-data scan is a gate, not a printout: it exits non-zero on any hit, and commit runs only
  after it passes. Run it as a separate step and read the result before committing. Don't quote private label
  rows in tracked files; paraphrase them.

## 2026-11-16 — week-2 spec review (draft 1): two design bugs caught before any code

- **What:** draft 1 of `docs/specs/week-2.md` proposed an event cursor over `vacancy_events.id`. Concurrent board
  fetches commit out of id order, so the cursor would skip events and lose vacancies silently. It also left step
  retries to arq, whose redelivery after `kill -9` takes `job_timeout + 10` s (610 s) and whose job-id dedup
  blocks re-enqueue for an hour. Both claims checked by the implementer (arq source in `.venv`, `store.py` flush
  and commit points).
- **Also found:** no fenced commit (a worker whose lease expired could overwrite another's result); content
  changes would store a result under the wrong input hash; the prompt author has seen every existing eval set;
  "no duplicate Telegram messages" is impossible as stated; the owner profile would have landed in a tracked prompt
  file; the 15% unfiltered sample was not measurable as specified.
- **Fix:** draft 2 of the spec (state-based reconcile, cron drain with leases and fenced commits, input check at
  commit, prompt freeze before scoring a fresh batch, at-least-once sends stated honestly, profile in the private
  dir, sample drawn among rejected vacancies with stored probability).
- **Lesson:** the spec review paid for itself on the first try: both design bugs would have been expensive after
  PR 6 was written. Concurrency claims in a spec need the same "how does this commit?" check as code.

## 2026-11-16 — PR 6 review: step engine (17 findings, 3 real bugs, 2 of them reproduced by the reviewer)

Review of the uncommitted `w2-pipeline` tree by a read-only Sonnet session; it ran the suite, 11 own mutants
and probe scripts. Verdict: "do not merge as is" — all blockers fixed the same day, see actions.

| # | Severity | Finding | Action |
|---|---|---|---|
| 1 | blocker | New open-set invariant in `test_out_of_order_fetches_never_regress` ignored stale fetches; Hypothesis counterexample (late empty fetch applied first) failed the suite. Implementation was right, the assertion wrong. | Fixed: invariant computed over the accepted (non-stale) subsequence |
| 2 | should-fix | Lease decay on batch tails: drain claimed 10 rows but heartbeat only the running one; rows 2..N expired mid-drain, got reclaimed by an overlapping drain and double-run (reviewer reproduced: 3 executions for 2 rows). Harmless for regex, double cost for PR 7 LLM steps. | Fixed: a heartbeat per claimed row starts before the first row runs; a row whose lease is lost is not started |
| 3 | should-fix | Reconcile's multi-row INSERT and expanding `IN` binds hit asyncpg's 32767-parameter limit at ~8k rows (reviewer reproduced); reconcile would die permanently at 3 chain steps x 3000 vacancies. | Fixed: statements chunked (2000 rows / 10000 ids) |
| 4 | should-fix | Race: commit-time next-step insert landing between reconcile's chain walk and its stale SELECT was superseded spuriously (healed a minute later). | Fixed: supersede only rows whose (vacancy, step) has a desired hash that differs; underivable rows are left to the claim-time input check |
| 5 | should-fix | `updated_at` never changed: `onupdate` fires on ORM flushes only, every engine write is raw SQL; `steps status` "oldest waiting_human" actually measured age since creation. | Fixed: `updated_at=now()` on every raw UPDATE |
| 6 | should-fix | Property test never exercised partial fetches (drew a `complete` flag and ignored it). | Fixed: flag passed through |
| 7 | should-fix | Overlapping-drain pile-up concern. | No code change: arq cron jobs are unique, a running drain blocks the next enqueue; documented in worker.py |
| 8 | should-fix | Backoff untested: a mutant making pending rows ignore `lease_until` survived. | Fixed: `test_backoff_delays_the_next_claim`; mutant added to the engine mutation list (9/9 killed) |
| 9-11 | nit | Heartbeat died silently on a DB error; error paths recorded no latency; exhaustion overwrote the last real error message. | All three fixed |
| 12-17 | nit | Reconcile scale (per-vacancy queries, unbounded skipped scan), `ORDER BY id` starving new rows during a backfill, baseline flap edge, migration importing the whole chain, dangling decisions.md reference, claim subquery hardening. | Migration now imports the step directly; claim uses a MATERIALIZED CTE; two-empty decision recorded and the 2026-11-11 one marked superseded; the rest accepted as PR 7+ scale work |

Clean per the reviewer: fencing (every post-claim write), crash safety (single-transaction commits), the
migration backfill (verified on a scratch DB with pre-seeded vacancies), two-empty-listings implementation,
CLI injection surface, mypy/ruff. Note for the story: findings 2 and 3 were both *reproduced with probe
scripts by the reviewer* before I touched anything — that is the review style worth keeping.


## 2026-11-17 — PR 7 review: LLM layer and `extract` (9 findings, 5 real bugs, 2 decisions.md claims were false)

Review of the uncommitted `w2-llm` tree by a read-only Sonnet session, barred from calling the API. It ran the
suite, mypy, ruff, its own mutants and probe patches in a temp copy. The budget gate, the baseline protection
(no key = rules-only chain) and the SDK facts were checked and found clean.

| # | Severity | Finding | Action |
|---|---|---|---|
| 1 | bug | A ledger write failure (raw SQLAlchemy error) reached the engine as an unclassified exception = permanent: a billed valid result was thrown away; a ledger failure on the *shadow* call failed the whole step, contradicting decisions.md. Reviewer reproduced all three cases with a patched ledger. | Fixed: `LedgerError(TransientStepError)` wraps ledger DB errors and carries the spend so far; the shadow path catches every exception. Tests for both |
| 2 | bug | The out-of-credits pause still burned an attempt per hourly probe: after 5 hours without a top-up rows went `failed/exhausted` — decisions.md said the opposite. | Fixed: `StepError.counts_attempt`; `OutOfCreditsError` hands the attempt back. Test: 5 probes, attempts stay 0 |
| 3 | test gap | Nothing tested the advisory lock: replacing it with `pass` passed every test. Reviewer wrote a 4-engine `gather` test that fails without the lock. | Fixed: concurrent-claimers test; lock mutant added |
| 4 | bug | Email regex quadratic, run on the full untruncated text at every reconcile: 50 000-char token = 7.4 s, enough to stall reconcile past its cron timeout. | Fixed: bounded patterns, cut before masking; linearity test on 400k chars |
| 5 | bug (minor) | PII false positives ("+20 000 - 30 000 EUR" became `[phone]`, a phone's `\s` ate the next line's digit) and misses (Russian "8 (495) ..."). | Fixed: digit-count threshold, no newline/" - " joins, Russian pattern; tests per case. Remaining misses documented |
| 6 | test gaps | Surviving mutants: cost formula (tests used `cost_usd` as their own oracle), chain wiring `if True`, shadow cost, transient shadow, blank-quote branch, 408, ignored `failed` status, `spent_today` time zone; near-tautological marker test. | All covered by new tests; 13 of them added to the mutation list |
| 7 | bug (minor) | `compose.yml` passed only the key to the worker: the cap/model settings in `.env` silently did nothing in Docker. | Fixed: LLM settings passed through, empty = config default |
| 8 | nit | Timeouts and `failed` responses wrote cost 0 over the estimate though they may be billed; a shadow hanging past the hard timeout discards the primary result. | Timeouts keep the estimate; `failed` goes through the billed invalid-output path. Shadow hang accepted (needs a stuck DB under 4x60 s request timeouts) |
| 9 | nit | Retry after truncation reuses the same output limit; `stack` max 20 unenforced; title/location untruncated; multi-LLM-step starvation; reservations double-count started calls; `AsyncOpenAI` never closed; bad key fails rows one by one; model/price assumptions unverified live. | Title/location cut to 500 chars; client closed at shutdown; live eval (76/76 valid) verified models and prices; the rest accepted for now (one LLM step, conservative counting, `retry-failed` recovers) |

Lesson: two of the five bugs were **claims in decisions.md that the code did not do** ("a shadow failure
never fails the step", "out of credits does not burn attempts"). The reviewer read the decision record as a
spec and tested it. Rule kept: a decisions.md claim about failure behaviour needs a test that would fail if
the claim were false.
