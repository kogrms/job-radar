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
