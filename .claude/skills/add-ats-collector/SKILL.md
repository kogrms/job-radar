---
name: add-ats-collector
description: Add a new ATS (applicant tracking system) collector to job-radar, e.g. SmartRecruiters, Recruitee, Personio, BambooHR. Use when the user wants vacancies from a job board that the existing ashby/greenhouse/lever/workable collectors do not cover.
---

# Adding an ATS collector

A collector is a module in `src/job_radar/collectors/` with two functions: `board_url(org, region)` and
`parse(data, org, company) -> BoardFetch`. HTTP, retries and failure handling are shared (`collectors/base.py`).
The one rule that matters: **`complete=True` only when the whole listing was read**. A complete fetch closes every
posting it does not contain; a wrong `complete=True` closes real vacancies.

## Steps

1. **Probe the live API first** (curl, a real public board and a made-up one). Record in the PR description:
   - the full-listing endpoint and whether it includes descriptions or needs one call per job;
   - pagination: does one call return everything? Is there a total count to check against (Greenhouse `meta.total`)?
   - what a missing board returns (404? 200 with an error body? an empty list?) — an empty-list answer for a missing
     board must NOT be treated as an empty board;
   - whether the org name is case-insensitive (try the name in another case);
   - regional hosts (Lever and Greenhouse differ; check, do not assume).
2. **Module** `collectors/<ats>.py` modelled on an existing one: stable job id, canonical job URL, title, company,
   `location_raw` via `join_locations` (raw wording, all locations, workplace type), `raw_text` via the API's
   plain text or `html_to_text`. Return `BoardFetch.failed("unexpected shape")` when the top-level shape is wrong.
3. **Register** it in `COLLECTORS` (`collectors/__init__.py`); add it to `_CASE_INSENSITIVE` only if step 1 proved it.
4. **Schema:** add the source to `SOURCES` in `models.py` and to `BoardConfig.source`; generate an Alembic
   migration — the `source_known` CHECK constraint must change, autogenerate will not notice it, write it by hand.
5. **Tests** in `tests/test_collectors.py`, synthetic data shaped like the real response (no real company data):
   parsing of every field, empty board → complete with zero postings, missing board → failed, truncated listing →
   not complete (if the API gives a total), and add the source to the empty-board parametrization.
6. **Live check:** add one real board to the private `boards.yml`, run `collect_board` twice through the queue —
   the second run must produce zero events. Spot-check `raw_text` for leftover HTML.
7. Record API quirks in `upstream-bugs.md`.
