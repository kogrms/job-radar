---
status: todo
found: 2026-11-16, held-out evaluation (12 of 42 "no" fell through to "unclear")
week: 2
---

# The rules' hand-made place list misses most cities, US states and several countries

`PLACES` in `eligibility/rules.py` is a short hand list, so "Texas; Colorado; Remote", "Kuala Lumpur; onsite",
"Saudi Arabia; Remote" end in `fallback` → unclear. Safe (a human sees them) but it is review load: 22 of 129
open postings in the held-out set. Options: a real gazetteer (country list from ISO 3166 + large cities), or leave
it to the LLM classifier. Any fix is measured on a fresh held-out batch, not on the 60 rows that exposed it.
