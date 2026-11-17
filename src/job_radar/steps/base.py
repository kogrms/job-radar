"""What a pipeline step is.

A step is a pure-ish unit: `build_input` derives the step's input from committed state (the vacancy row
and the upstream step's run, if any), `run` computes a result from that input alone. The engine owns
everything else: claiming, leases, retries, the fenced commit.

`input_hash` is computed from the step's *actual inputs* plus the step version, never from the whole vacancy:
a change to fields a step does not read must not re-run it. A downstream step hashes the upstream run's
`input_hash` and result into its own input, so a superseded upstream invalidates downstream work by key.
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Protocol

from job_radar.models import StepRun, Vacancy


@dataclass(frozen=True)
class StepInput:
    """The exact payload a step will run on, and its identity."""

    payload: dict[str, Any]
    hash: str


@dataclass(frozen=True)
class StepOutcome:
    """What a step produced. `result` goes into `step_runs.result` (must be JSON-serializable)."""

    result: dict[str, Any]
    cost_usd: float | None = None
    model: str | None = None
    prompt_version: str | None = None


class TransientStepError(Exception):
    """The call may succeed on retry (timeout, 429, 5xx, connection). The engine re-queues with backoff."""


class PermanentStepError(Exception):
    """Retrying cannot help (validation, other 4xx). The engine fails the run at once."""


def input_hash(step_name: str, version: str, payload: dict[str, Any]) -> str:
    """Stable identity of one step input. Any change to the canonical form invalidates every stored hash,
    so treat the format as frozen; bump the step's `version` to re-run a step on unchanged content."""
    canonical = json.dumps(
        {"step": step_name, "version": version, "input": payload},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


class Step(Protocol):
    """One pipeline step. Implementations are stateless; the engine passes all context explicitly."""

    name: str
    version: str

    def build_input(self, vacancy: Vacancy, upstream: StepRun | None) -> StepInput | None:
        """Input for this vacancy from committed state, or None when the step does not apply.

        Must read only committed columns (no I/O): the engine calls it inside transactions, including the
        commit-time input check.
        """
        ...

    async def run(self, payload: dict[str, Any]) -> StepOutcome:
        """Compute the result. Raise TransientStepError / PermanentStepError; anything else is a bug and is
        treated as permanent. Long calls must tolerate cancellation (the engine enforces a hard timeout)."""
        ...
