"""Step 2: structured extraction of vacancy fields by an LLM (docs/specs/week-2.md, PR 7).

Primary model on every vacancy; on a seeded share of vacancies a shadow model runs the same input. Only the
primary result feeds the pipeline; the shadow lands next to it (`result.shadow`) and in the call ledger as
`role='shadow'`, to measure the quality gap. A shadow failure never fails the step.
"""

import hashlib
from typing import Any

from job_radar.extract.pii import mask_pii
from job_radar.extract.prompt import INSTRUCTIONS, PROMPT_SHA, PROMPT_VERSION, render_posting
from job_radar.extract.schema import VacancyFields
from job_radar.llm.client import LLM, Structured
from job_radar.llm.ledger import CallTags
from job_radar.llm.pricing import estimate_tokens, estimate_usd
from job_radar.models import StepRun, Vacancy
from job_radar.steps.base import RunContext, StepInput, StepOutcome, input_hash

FIELD_CHARS = 500  # title and ATS location field; real ones are far shorter


def in_shadow_sample(vacancy_id: int, *, rate: float, salt: str) -> bool:
    """Seeded, stable per vacancy (not per content): a vacancy is either in the shadow sample or not."""
    digest = hashlib.sha256(f"{salt}:{vacancy_id}".encode()).digest()
    return int.from_bytes(digest[:8]) / 2**64 < rate


def quote_check(posting: dict[str, Any]) -> Any:
    """The location quote must be an exact, non-blank span of what the model was shown."""
    sources = (posting["title"], posting["location_raw"], posting["text"])

    def check(fields: VacancyFields) -> str | None:
        quote = fields.location_quote
        if quote is None:
            return None
        if not quote.strip():
            return "location_quote is blank; use null when the posting says nothing about location"
        if not any(quote in s for s in sources):
            return (
                "location_quote is not an exact substring of the posting; copy the span character for "
                "character, or use null"
            )
        return None

    return check


def worst_case_usd(
    *, model: str, shadow_model: str | None, max_output_tokens: int, max_text_chars: int
) -> float:
    """Reservation per run for the budget gate: two attempts on the primary and, if a shadow is configured
    at all, two on the shadow (the claim cannot tell which vacancies are sampled)."""
    first = estimate_tokens(INSTRUCTIONS) + (max_text_chars + 2 * FIELD_CHARS) // 2 + 200
    retry = first + max_output_tokens + 200

    def two_attempts(m: str) -> float:
        return estimate_usd(m, input_tokens=first, output_tokens=max_output_tokens) + estimate_usd(
            m, input_tokens=retry, output_tokens=max_output_tokens
        )

    return two_attempts(model) + (two_attempts(shadow_model) if shadow_model else 0.0)


class ExtractStep:
    name = "extract"
    # Bump to re-run on unchanged content for a reason the payload does not capture.
    version = "v1"

    def __init__(
        self,
        llm: LLM,
        *,
        model: str,
        shadow_model: str | None,
        shadow_rate: float,
        shadow_salt: str,
        reasoning_effort: str | None,
        max_output_tokens: int,
        max_text_chars: int,
    ) -> None:
        self.llm = llm
        self.model = model
        self.shadow_model = shadow_model
        self.shadow_rate = shadow_rate if shadow_model else 0.0
        self.shadow_salt = shadow_salt
        self.reasoning_effort = reasoning_effort
        self.max_output_tokens = max_output_tokens
        self.max_text_chars = max_text_chars
        self.cost_estimate_usd = worst_case_usd(
            model=model,
            shadow_model=shadow_model if shadow_rate > 0 else None,
            max_output_tokens=max_output_tokens,
            max_text_chars=max_text_chars,
        )

    def build_input(self, vacancy: Vacancy, upstream: StepRun | None) -> StepInput | None:
        shadow = (
            self.shadow_model
            if in_shadow_sample(vacancy.id, rate=self.shadow_rate, salt=self.shadow_salt)
            else None
        )
        # Cut before masking: the masking regexes then run on a bounded string whatever the posting holds.
        payload = {
            "title": vacancy.title[:FIELD_CHARS],
            "location_raw": vacancy.location_raw[:FIELD_CHARS],
            "text": mask_pii(vacancy.raw_text[: self.max_text_chars]),
            "model": self.model,
            "shadow_model": shadow,
            "reasoning_effort": self.reasoning_effort,
            "max_output_tokens": self.max_output_tokens,
            "prompt": f"{PROMPT_VERSION}:{PROMPT_SHA}",
        }
        return StepInput(payload=payload, hash=input_hash(self.name, self.version, payload))

    async def _extract(
        self, payload: dict[str, Any], model: str, tags: CallTags
    ) -> Structured[VacancyFields]:
        return await self.llm.structured(
            tags=tags,
            model=model,
            prompt_version=PROMPT_VERSION,
            instructions=INSTRUCTIONS,
            user_input=render_posting(
                title=payload["title"], location_raw=payload["location_raw"], text=payload["text"]
            ),
            schema=VacancyFields,
            check=quote_check(payload),
            reasoning_effort=payload["reasoning_effort"],
            max_output_tokens=payload["max_output_tokens"],
        )

    async def run(self, payload: dict[str, Any], ctx: RunContext) -> StepOutcome:
        tags = CallTags(purpose=self.name, step_run_id=ctx.step_run_id, vacancy_id=ctx.vacancy_id)
        primary = await self._extract(payload, payload["model"], tags)
        cost = primary.cost_usd

        shadow: dict[str, Any] | None = None
        if payload["shadow_model"]:
            shadow_tags = CallTags(
                purpose=self.name, role="shadow", step_run_id=ctx.step_run_id, vacancy_id=ctx.vacancy_id
            )
            try:
                s = await self._extract(payload, payload["shadow_model"], shadow_tags)
            except Exception as exc:  # any shadow failure, ledger errors included, stays in the shadow
                cost += getattr(exc, "cost_usd", None) or 0.0
                shadow = {"model": payload["shadow_model"], "error": f"{type(exc).__name__}: {exc}"[:500]}
            else:
                cost += s.cost_usd
                shadow = {
                    "model": s.model,
                    "fields": s.value.model_dump(mode="json"),
                    "calls": s.calls,
                    "invalid_outputs": s.invalid_outputs,
                    "cost_usd": s.cost_usd,
                }

        return StepOutcome(
            result={
                "fields": primary.value.model_dump(mode="json"),
                "calls": primary.calls,
                "invalid_outputs": primary.invalid_outputs,
                "cost_usd": primary.cost_usd,
                "shadow": shadow,
            },
            cost_usd=cost,
            model=primary.model,
            prompt_version=PROMPT_VERSION,
        )
