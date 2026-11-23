"""`LLM.structured`: one structured answer, validated, with at most one corrective retry.

Flow per attempt: ledger row (estimate) → provider request → Pydantic validation + the caller's check →
ledger row (real cost, outcome) → trace. An invalid answer (schema, refusal, truncation, failed check) gets
one retry with the problem attached; a second invalid answer is permanent. Provider errors propagate with
the cost already spent in this call sequence attached, so a failed run still records its spend.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from job_radar.llm.ledger import CallTags, Ledger, LedgerError
from job_radar.llm.pricing import cost_usd, estimate_tokens, estimate_usd
from job_radar.llm.provider import (
    LLMPermanentError,
    LLMTimeoutError,
    LLMTransientError,
    Message,
    OutOfCreditsError,
    Provider,
    RawCompletion,
)
from job_radar.llm.tracing import NullTracer, Tracer

T = TypeVar("T", bound=BaseModel)
MAX_ATTEMPTS = 2  # the first answer plus one retry with the validation error attached


@dataclass(frozen=True)
class Structured[S: BaseModel]:
    value: S
    model: str
    calls: int
    invalid_outputs: int
    cost_usd: float


class InvalidOutputError(LLMPermanentError):
    """Two answers in a row failed validation."""


def _describe(exc: ValidationError) -> str:
    parts = [f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}" for e in exc.errors()[:10]]
    return "; ".join(parts)


class LLM:
    def __init__(self, provider: Provider, ledger: Ledger, tracer: Tracer | None = None) -> None:
        self.provider = provider
        self.ledger = ledger
        self.tracer = tracer or NullTracer()

    async def close(self) -> None:
        self.tracer.flush()
        await self.provider.close()

    async def structured(
        self,
        *,
        tags: CallTags,
        model: str,
        prompt_version: str,
        instructions: str,
        user_input: str,
        schema: type[T],
        check: Callable[[T], str | None] | None = None,
        reasoning_effort: str | None = None,
        max_output_tokens: int = 4000,
    ) -> Structured[T]:
        messages = [Message("user", user_input)]
        spent = 0.0
        problem = ""
        try:
            for attempt in range(1, MAX_ATTEMPTS + 1):
                estimate = estimate_usd(
                    model,
                    input_tokens=estimate_tokens(instructions + "".join(m.content for m in messages)),
                    output_tokens=max_output_tokens,
                )
                # If this task is cancelled (hard timeout) or killed mid-request, the row keeps the estimate.
                call_id = await self.ledger.start(
                    tags, attempt=attempt, model=model, prompt_version=prompt_version, estimate_usd=estimate
                )
                gen = self.tracer.generation(
                    name=f"{tags.purpose}:{tags.role}",
                    model=model,
                    input={"instructions": instructions, "messages": [m.__dict__ for m in messages]},
                    metadata={
                        "step_run_id": tags.step_run_id,
                        "vacancy_id": tags.vacancy_id,
                        "attempt": attempt,
                        "prompt_version": prompt_version,
                    },
                    parameters={
                        "reasoning_effort": reasoning_effort or "default",
                        "max_output_tokens": max_output_tokens,
                    },
                )
                started = time.monotonic()
                try:
                    raw = await self.provider.complete(
                        model=model,
                        instructions=instructions,
                        messages=messages,
                        schema=schema,
                        reasoning_effort=reasoning_effort,
                        max_output_tokens=max_output_tokens,
                    )
                except (LLMTransientError, LLMPermanentError) as exc:
                    outcome = (
                        "out_of_credits"
                        if isinstance(exc, OutOfCreditsError)
                        else "transient_error"
                        if isinstance(exc, LLMTransientError)
                        else "permanent_error"
                    )
                    latency = int((time.monotonic() - started) * 1000)
                    billed = estimate if isinstance(exc, LLMTimeoutError) else 0.0
                    spent += billed
                    await self.ledger.finish(
                        call_id, outcome=outcome, cost_usd=billed, latency_ms=latency, error_message=str(exc)
                    )
                    gen.finish(output=None, usage=None, cost_usd=billed, error=str(exc), scores={})
                    exc.cost_usd = spent
                    raise
                latency = int((time.monotonic() - started) * 1000)
                cost = cost_usd(model, raw.usage) if raw.usage else estimate
                spent += cost

                value, problem = self._validate(raw, schema, check)
                outcome = "ok" if value is not None else ("refusal" if raw.refusal else "invalid")
                await self.ledger.finish(
                    call_id,
                    outcome=outcome,
                    cost_usd=cost,
                    usage=raw.usage,
                    latency_ms=latency,
                    response_model=raw.response_model,
                    request_id=raw.request_id,
                    error_message=problem or None,
                )
                gen.finish(
                    output=raw.refusal or raw.text,
                    usage=raw.usage,
                    cost_usd=cost,
                    error=problem or None,
                    scores={"invalid_output": 0.0 if value is not None else 1.0, "retry": float(attempt > 1)},
                )
                if value is not None:
                    return Structured(
                        value=value, model=model, calls=attempt, invalid_outputs=attempt - 1, cost_usd=spent
                    )
                messages += [
                    Message("assistant", raw.text or f"(refused: {raw.refusal})"),
                    Message(
                        "user",
                        f"Your previous answer was rejected: {problem}. Answer again with JSON that fits the "
                        "schema and follows every rule in the instructions.",
                    ),
                ]
        except LedgerError as exc:
            # The ledger write failed; whatever this sequence already spent is still spent.
            exc.cost_usd = spent
            raise
        raise InvalidOutputError(f"invalid output after {MAX_ATTEMPTS} attempts: {problem}", cost_usd=spent)

    @staticmethod
    def _validate(
        raw: RawCompletion, schema: type[T], check: Callable[[T], str | None] | None
    ) -> tuple[T | None, str]:
        if raw.refusal:
            return None, f"the model refused: {raw.refusal[:300]}"
        if raw.incomplete:
            return None, f"the answer was cut off ({raw.incomplete})"
        try:
            value = schema.model_validate_json(raw.text)
        except ValidationError as exc:
            return None, f"schema validation failed: {_describe(exc)}"
        if check is not None and (msg := check(value)):
            return None, msg
        return value, ""
