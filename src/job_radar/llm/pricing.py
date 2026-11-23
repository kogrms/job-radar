"""Model prices, USD per million tokens, standard tier, short context.

From OpenAI's pricing page on 2026-11-17 (developers.openai.com/api/docs/pricing). Recheck before raising the
budget; an unknown model id fails at startup instead of being billed as free.
"""

from dataclasses import dataclass

from job_radar.llm.provider import Usage


@dataclass(frozen=True)
class Price:
    input: float
    cached_input: float
    output: float


PRICES: dict[str, Price] = {
    "gpt-6-luna": Price(input=0.10, cached_input=0.01, output=0.50),
    "gpt-6.1-sol": Price(input=2.00, cached_input=0.10, output=10.00),
}


def price_of(model: str) -> Price:
    try:
        return PRICES[model]
    except KeyError:
        raise ValueError(f"no price for model {model!r}; add it to job_radar.llm.pricing.PRICES") from None


def cost_usd(model: str, usage: Usage) -> float:
    """Reasoning tokens are part of `output_tokens` (billed as output); cached input is a subset of input."""
    p = price_of(model)
    uncached = max(usage.input_tokens - usage.cached_input_tokens, 0)
    return (
        uncached * p.input + usage.cached_input_tokens * p.cached_input + usage.output_tokens * p.output
    ) / 1e6


def estimate_usd(model: str, *, input_tokens: int, output_tokens: int) -> float:
    p = price_of(model)
    return (input_tokens * p.input + output_tokens * p.output) / 1e6


def estimate_tokens(text: str) -> int:
    """A deliberately high token count for a text: ~2 characters per token (English runs ~4, Cyrillic ~2.5).
    Used for budget reservations and the eval's cost preview, never for billing."""
    return len(text) // 2 + 1
