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
