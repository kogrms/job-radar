"""Durable per-step pipeline (docs/specs/week-2.md, PR 6).

`base` defines what a step is, `engine` creates and executes step runs, `eligibility_rules_step` is the first
real step. The production chain lives in `chain.CHAIN`.
"""
