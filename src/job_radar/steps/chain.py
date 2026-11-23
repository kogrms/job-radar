"""The production step chain, in execution order.

`CHAIN` is the LLM-free part (always runs). `build_chain` appends the LLM steps when an API key is configured;
`STEP_NAMES` lists every step the CLI may address, whether or not this process can run it.
"""

from langfuse import Langfuse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from job_radar.config import Settings
from job_radar.llm.client import LLM
from job_radar.llm.ledger import Ledger
from job_radar.llm.openai_provider import OpenAIProvider
from job_radar.llm.tracing import LangfuseTracer, NullTracer, Tracer
from job_radar.steps.base import Step
from job_radar.steps.eligibility_rules_step import EligibilityRulesStep
from job_radar.steps.extract_step import ExtractStep

CHAIN: list[Step] = [EligibilityRulesStep()]
STEP_NAMES = ("eligibility_rules", "extract")


def make_tracer(settings: Settings) -> Tracer:
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        return NullTracer()
    return LangfuseTracer(
        Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key.get_secret_value(),
            base_url=settings.langfuse_base_url,
        )
    )


def make_llm(settings: Settings, sessionmaker: async_sessionmaker[AsyncSession]) -> LLM | None:
    if settings.openai_api_key is None:
        return None
    provider = OpenAIProvider(
        settings.openai_api_key.get_secret_value(), timeout_seconds=settings.llm_timeout_seconds
    )
    return LLM(provider, Ledger(sessionmaker), make_tracer(settings))


def make_extract_step(settings: Settings, llm: LLM) -> ExtractStep:
    return ExtractStep(
        llm,
        model=settings.extract_model,
        shadow_model=settings.extract_shadow_model,
        shadow_rate=settings.extract_shadow_rate,
        shadow_salt=settings.extract_shadow_salt,
        reasoning_effort=settings.extract_reasoning_effort,
        max_output_tokens=settings.extract_max_output_tokens,
        max_text_chars=settings.extract_max_text_chars,
    )


def build_chain(settings: Settings, llm: LLM | None) -> list[Step]:
    chain = list(CHAIN)
    if llm is not None:
        chain.append(make_extract_step(settings, llm))
    return chain
