"""Step 1: the regex eligibility rules (src/job_radar/eligibility/rules.py) as a pipeline step."""

from typing import Any

from job_radar.eligibility import rules
from job_radar.models import StepRun, Vacancy
from job_radar.steps.base import StepInput, StepOutcome, input_hash


class EligibilityRulesStep:
    name = "eligibility_rules"
    # Bump when the rules change behaviour: the hash change re-runs the step on unchanged content.
    version = "v2"

    def build_input(self, vacancy: Vacancy, upstream: StepRun | None) -> StepInput | None:
        payload = {"location_raw": vacancy.location_raw}
        return StepInput(payload=payload, hash=input_hash(self.name, self.version, payload))

    async def run(self, payload: dict[str, Any]) -> StepOutcome:
        v = rules.classify(payload["location_raw"])
        return StepOutcome(
            result={"label": v.label, "rule": v.rule, "evidence": v.evidence},
            model=None,
            prompt_version=f"rules-{self.version}",
        )
