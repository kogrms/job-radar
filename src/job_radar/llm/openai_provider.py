"""OpenAI Responses API provider (openai SDK 3.x; methods checked against the SDK source of 3.52.0).

`responses.create` with a strict JSON-schema text format, not `responses.parse`: parse raises on invalid
output and the usage of that (billed) answer would be lost; here the raw text comes back with its usage and
the LLM layer validates it. `store=False`: vacancy texts are not kept on the provider side.
"""

from typing import Any, cast

import httpx2
import openai
from openai import AsyncOpenAI, omit
from openai.lib import type_to_response_format_param
from openai.types.responses import (
    ResponseFormatTextJSONSchemaConfigParam,
    ResponseInputParam,
    ResponseTextConfigParam,
)
from openai.types.shared import ReasoningEffort
from openai.types.shared_params import Reasoning
from pydantic import BaseModel

from job_radar.llm.provider import (
    LLMPermanentError,
    LLMTimeoutError,
    LLMTransientError,
    Message,
    OutOfCreditsError,
    RawCompletion,
    Usage,
)

# Error codes of an empty prepaid balance (help.openai.com, "prepaid billing"; the older name first).
_OUT_OF_CREDITS_CODES = frozenset({"insufficient_quota", "credit_balance_exhausted"})
_TRANSIENT_STATUS = frozenset({408, 409, 429})


def text_format_for(schema: type[BaseModel]) -> ResponseFormatTextJSONSchemaConfigParam:
    """The Responses `text.format` for a Pydantic model: the SDK's own strict-schema conversion."""
    fmt: Any = type_to_response_format_param(schema)
    js = fmt["json_schema"]
    return {"type": "json_schema", "name": js["name"], "schema": js["schema"], "strict": True}


def classify(exc: Exception) -> Exception:
    """Map an SDK exception to the engine's error classes. By class and structured code, never by text."""
    if isinstance(exc, openai.APIStatusError):
        if exc.code in _OUT_OF_CREDITS_CODES:
            return OutOfCreditsError(f"{exc.status_code} {exc.code}: {exc.message}")
        if exc.status_code in _TRANSIENT_STATUS or exc.status_code >= 500:
            return LLMTransientError(f"{exc.status_code}: {exc.message}")
        return LLMPermanentError(f"{exc.status_code}: {exc.message}")
    if isinstance(exc, openai.APITimeoutError):
        return LLMTimeoutError(f"{type(exc).__name__}: {exc}")
    if isinstance(exc, openai.APIConnectionError):
        return LLMTransientError(f"{type(exc).__name__}: {exc}")
    if isinstance(exc, openai.APIResponseValidationError):
        return LLMTransientError(f"unparseable provider response: {exc}")
    return exc


class OpenAIProvider:
    def __init__(
        self, api_key: str, *, timeout_seconds: float, http_client: httpx2.AsyncClient | None = None
    ) -> None:
        self._client = AsyncOpenAI(
            api_key=api_key, max_retries=0, timeout=timeout_seconds, http_client=http_client
        )

    async def close(self) -> None:
        await self._client.close()

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
        inputs: ResponseInputParam = [{"role": m.role, "content": m.content} for m in messages]
        text: ResponseTextConfigParam = {"format": text_format_for(schema)}
        reasoning: Reasoning | None = (
            {"effort": cast(ReasoningEffort, reasoning_effort)} if reasoning_effort is not None else None
        )
        try:
            response = await self._client.responses.create(
                model=model,
                instructions=instructions,
                input=inputs,
                text=text,
                max_output_tokens=max_output_tokens,
                store=False,
                reasoning=reasoning if reasoning is not None else omit,
            )
        except openai.OpenAIError as exc:
            raise classify(exc) from exc

        refusal = None
        for item in response.output:
            if item.type == "message":
                for content in item.content:
                    if content.type == "refusal":
                        refusal = content.refusal
        incomplete: str | None = None
        if response.status == "incomplete":
            reason = response.incomplete_details.reason if response.incomplete_details else None
            incomplete = reason or "unknown"
        elif response.status == "failed":
            # Generation failed server-side; whatever usage it reports is billed, so it goes through the
            # invalid-output path (ledger cost, one corrective retry) rather than a cost-free transient error.
            incomplete = f"failed: {response.error.message if response.error else 'no detail'}"
        usage = None
        if response.usage is not None:
            u = response.usage
            usage = Usage(
                input_tokens=u.input_tokens,
                cached_input_tokens=u.input_tokens_details.cached_tokens,
                output_tokens=u.output_tokens,
                reasoning_tokens=u.output_tokens_details.reasoning_tokens,
            )
        return RawCompletion(
            text=response.output_text,
            refusal=refusal,
            incomplete=incomplete,
            usage=usage,
            response_model=response.model,
            request_id=response._request_id,
        )
