"""A step whose run time comes from the environment: the kill -9 test makes the child hang and the parent
finish instantly, while both compute identical input hashes (same name, version, payload)."""

import asyncio
import os
from typing import Any

from job_radar.models import StepRun, Vacancy
from job_radar.steps.base import RunContext, StepInput, StepOutcome, input_hash


class SleepyStep:
    name = "sleepy"
    version = "t1"
    cost_estimate_usd = 0.0

    def build_input(self, vacancy: Vacancy, upstream: StepRun | None) -> StepInput | None:
        payload = {"location_raw": vacancy.location_raw}
        return StepInput(payload=payload, hash=input_hash(self.name, self.version, payload))

    async def run(self, payload: dict[str, Any], ctx: RunContext) -> StepOutcome:
        await asyncio.sleep(float(os.environ.get("SLEEPY_SECONDS", "0")))
        return StepOutcome(result={"ok": True})
