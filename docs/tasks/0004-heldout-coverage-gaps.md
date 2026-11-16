---
status: todo
found: 2026-11-16, reading the sampled held-out set
week: 2
---

# Held-out set has no Himalayas rows without Serbia and no "No location restrictions listed"

`scripts/sample_heldout.py` (seed 20261112) drew 15 Himalayas wordings at random; all 15 name Serbia. Two groups
present in the live data are therefore not measured: Himalayas country lists without Serbia, and the
"No location restrictions listed | Time zones: ..." wording, which a large share of Himalayas postings carry.
Fix: after the current held-out set is evaluated, sample a small second batch stratified on these groups, label it
before running the rules, and report it separately.
