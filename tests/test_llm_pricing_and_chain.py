"""Price arithmetic against hand-computed numbers, and the chain wiring: no key, no LLM steps."""

import pytest

from job_radar.config import Settings
from job_radar.llm.client import LLM
from job_radar.llm.pricing import cost_usd, price_of
from job_radar.llm.provider import Usage
from job_radar.steps.chain import build_chain, make_llm
from tests.llm_fakes import FakeProvider


def test_cost_with_cached_input_matches_the_price_table() -> None:
    usage = Usage(input_tokens=10_000, cached_input_tokens=4_000, output_tokens=2_000, reasoning_tokens=500)
    # luna: 6000 x $0.10 + 4000 x $0.01 + 2000 x $0.50, per million tokens
    assert cost_usd("gpt-6-luna", usage) == pytest.approx((600 + 40 + 1000) / 1e6)
    # sol: 6000 x $2 + 4000 x $0.10 + 2000 x $10
    assert cost_usd("gpt-6.1-sol", usage) == pytest.approx((12_000 + 400 + 20_000) / 1e6)


def test_unknown_model_is_an_error_not_free() -> None:
    with pytest.raises(ValueError, match="no price"):
        price_of("gpt-unknown")


def test_without_a_key_the_chain_stops_after_the_rules(settings: Settings) -> None:
    assert settings.openai_api_key is None
    assert make_llm(settings, None) is None  # type: ignore[arg-type]
    assert [s.name for s in build_chain(settings, None)] == ["eligibility_rules"]


def test_with_an_llm_the_chain_gains_extract(settings: Settings) -> None:
    llm = LLM(FakeProvider(), None)  # type: ignore[arg-type]
    chain = build_chain(settings, llm)
    assert [s.name for s in chain] == ["eligibility_rules", "extract"]
    assert chain[1].cost_estimate_usd > 0
