---
status: todo
found: 2026-11-12, synthetic test while writing the rule baseline
week: 3
---

# Rules say "no" to "100% remote, or relocation to London or Dubai"

The listed cities read as the only allowed locations (`places_without_serbia`), although fully remote is offered.
Pinned as a known miss (`rules_answer`) in `evals/eligibility_challenge.csv`, with its sibling "Relocation to Berlin
or work remotely". Not fixed in the rules on purpose: context like this is what the LLM classifier should handle.
Close when the LLM classifier gets both right on the challenge set.
