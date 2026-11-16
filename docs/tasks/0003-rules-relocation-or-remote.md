---
status: todo
found: 2026-11-12, synthetic test while writing the rule baseline
week: 3
---

# Rules say "no" to "100% remote, or relocation to London or Dubai"

The listed cities read as the only allowed locations (`places_without_serbia`), although fully remote is offered.
Kept as a strict xfail in `tests/test_eligibility_rules.py`. Not fixed in the rules on purpose: context like this
is what the LLM classifier should handle. Close when the LLM classifier gets it right on the eval set.
