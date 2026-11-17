"""The production step chain, in execution order. Week 2 PR 6 ships one real step; PR 7/8/9 append theirs."""

from job_radar.steps.base import Step
from job_radar.steps.eligibility_rules_step import EligibilityRulesStep

CHAIN: list[Step] = [EligibilityRulesStep()]
