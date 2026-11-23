# Upstream bugs and oddities

Behaviour of third-party libraries, APIs and tools that surprised us. Each entry is a candidate issue or PR.

Format: project — what — repro — status / link.

## career-ops — work-eligibility gate (PR #6484, issue #6482)

- **What:** `buildWorkplacePolicy` (`providers/_country.mjs`):
  1. Timezone abbreviations in the location field resolved as ISO country codes (`ET` → Ethiopia,
     `PT` → Portugal, `MT` → Malta, `EST` → Estonia).
  2. Docs example listed `Europe`, `European Union` and `EU` together, so non-EU Europeans pass EU-only roles.
  3. Conditional region ("any EMEA country where we have a hiring presence") returned eligible.
- **Repro:** 5 failing cases + 5 controls, run against the PR branch on 2026-11-05.
- **Status:** review comment posted 2026-11-05 (kogrms). The author fixed all three in `e0ab297` with our cases
  as tests; re-run confirmed the same day. Merged by the maintainer on 2026-11-05.
- **Use here:** all ten cases go into this project's eligibility benchmark.

## ATS / job-board APIs (week-0 dataset recovery)

- **Greenhouse:** EU boards (`job-boards.eu.greenhouse.io`) are served by the regular `boards-api.greenhouse.io`.
  *Corrected 2026-11-11:* the earlier note claimed an EU API host; `boards-api.eu.greenhouse.io` does not even resolve.
  The week-0 script only worked because it fell back to the regular host.
- **Lever:** same split — `jobs.eu.lever.co` postings live on `api.eu.lever.co`.
- **hh.ru API:** 403 without an `HH-User-Agent` containing a contact email; the vacancy page has a schema.org
  JobPosting JSON-LD block that works without it.
- **Wayback Machine:** availability API rate-limits fast (429); job pages are rarely archived (1 of 45).
- **Closed ATS postings** are deleted, not archived: store raw text at collection time.

## Week 1 stack

- **arq 0.28.0 + redis-py 5.3:** `Worker.close()` calls the deprecated `close()` instead of `aclose()`
  (DeprecationWarning in our worker test). Small upstream PR candidate if arq is still maintained — check first.
- **FastAPI 0.162 / Starlette:** `TestClient` warns "Using `httpx` with `starlette.testclient` is deprecated;
  install `httpx2` instead". Revisit when PR 3 adds httpx-based collectors.
- **Langfuse v4 self-hosted:** `GET /api/public/traces/{id}` returns "not available in events_only mode";
  spans land in ClickHouse `events_full`. Docs for the replacement read API to check.
- **Homebrew on an Intel-prefix brew (`/usr/local`):** `brew install uv` builds LLVM and Rust from source.
  Installed `uv` from PyPI instead (`pip install --user uv`).

## ATS board APIs (week 1 collectors, checked live 2026-11-11)

- **Org name case:** Ashby and Greenhouse ignore it; Lever and Workable return 404 for a wrong case
  (`Name` vs `name`). Our own board list had one Ashby org spelled in two cases — two "boards" for one company.
- **Missing board:** all four answer 404 — distinguishable from an empty board, which is a valid JSON listing.
- **Greenhouse `content`** is HTML-escaped HTML (`&lt;div&gt;`): unescape before stripping tags, or the tags end up in
  the text. Caught by a test before any real data was stored.
- **Greenhouse `meta.total`** gives the board's own count — a cheap completeness check other APIs lack.
- **Lever** EU sites are on `api.eu.lever.co` (verified, unlike the Greenhouse claim above).

## Himalayas API (PR 3b, checked live 2026-11-11)

- **`totalCount` is 0-2 above the jobs the search actually returns** even when every page is read to an empty one
  with no duplicates (python 638/636, typescript 229/227). Probably expired or hidden jobs counted. Our completeness
  rule tolerates max(3, 2%).
- `country=RS` includes worldwide roles; `locationRestrictions: []` means no restriction listed.
- Search pages are 20 jobs; responses are cached by Cloudflare (`s-maxage=3600`), so re-reads within an hour are free.
