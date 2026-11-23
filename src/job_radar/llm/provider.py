"""What the LLM layer needs from a provider, and the errors it may raise.

Errors subclass the step errors so the engine classifies them by class: transient ones re-queue with
backoff, permanent ones fail the run. `OutOfCreditsError` is transient for the row and also pauses every
costly step through the budget gate (the ledger records it).
"""

from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel

from job_radar.steps.base import PermanentStepError, TransientStepError


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int  # includes reasoning tokens
    reasoning_tokens: int


@dataclass(frozen=True)
class Message:
    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True)
class RawCompletion:
    """One provider answer before validation. Exactly one of text / refusal is meaningful; `incomplete`
    names why generation stopped early (e.g. max_output_tokens), if it did."""

    text: str
    refusal: str | None
    incomplete: str | None
    usage: Usage | None
    response_model: str | None
    request_id: str | None


class LLMTransientError(TransientStepError):
    pass


class LLMPermanentError(PermanentStepError):
    pass


class LLMTimeoutError(LLMTransientError):
    """No answer in time. The provider may still have billed the request, so the ledger keeps the estimate."""


class OutOfCreditsError(LLMTransientError):
    """The prepaid balance is exhausted. Retrying the row later may work once the owner tops up; the attempt
    is handed back and the budget gate pauses costly claims meanwhile."""

    counts_attempt = False


class Provider(Protocol):
    async def complete(
        self,
        *,
        model: str,
        instructions: str,
        messages: list[Message],
        schema: type[BaseModel],
        reasoning_effort: str | None,
        max_output_tokens: int,
    ) -> RawCompletion:
        """One request, no retries. Raise LLMTransientError / LLMPermanentError / OutOfCreditsError."""
        ...

    async def close(self) -> None: ...
