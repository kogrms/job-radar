"""The OpenAI provider against canned HTTP responses (httpx2.MockTransport): the request we send, how the
answer is read, and how errors are classified. No network, no retries."""

import json
from typing import Any

import httpx2
import pytest

from job_radar.extract.schema import VacancyFields
from job_radar.llm.openai_provider import OpenAIProvider, text_format_for
from job_radar.llm.provider import (
    LLMPermanentError,
    LLMTimeoutError,
    LLMTransientError,
    Message,
    OutOfCreditsError,
)
from tests.llm_fakes import fields


def response_json(
    *, status: str = "completed", content: list[dict[str, Any]] | None = None, **extra: Any
) -> dict[str, Any]:
    body = {
        "id": "resp_1",
        "object": "response",
        "created_at": 1,
        "status": status,
        "model": "gpt-6-luna-2026-05-18",
        "output": [
            {"type": "reasoning", "id": "rs_1", "summary": []},
            {
                "type": "message",
                "id": "msg_1",
                "role": "assistant",
                "status": "completed",
                "content": content
                if content is not None
                else [{"type": "output_text", "text": json.dumps(fields()), "annotations": []}],
            },
        ],
        "usage": {
            "input_tokens": 1200,
            "input_tokens_details": {"cached_tokens": 200, "cache_write_tokens": 0},
            "output_tokens": 300,
            "output_tokens_details": {"reasoning_tokens": 100},
            "total_tokens": 1500,
        },
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
    }
    return {**body, **extra}


class Recorder:
    def __init__(self, status: int, body: Any, headers: dict[str, str] | None = None) -> None:
        self.status, self.body, self.headers = status, body, headers or {}
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if isinstance(self.body, Exception):
            raise self.body
        return httpx2.Response(self.status, json=self.body, headers=self.headers)


def provider(handler: Recorder) -> OpenAIProvider:
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return OpenAIProvider("sk-test", timeout_seconds=5, http_client=client)


async def complete(p: OpenAIProvider) -> Any:
    return await p.complete(
        model="gpt-6-luna",
        instructions="extract",
        messages=[Message("user", "posting")],
        schema=VacancyFields,
        reasoning_effort="low",
        max_output_tokens=4000,
    )


async def test_request_shape_and_answer_reading() -> None:
    handler = Recorder(200, response_json(), {"x-request-id": "req_abc"})
    raw = await complete(provider(handler))
    (req,) = handler.requests
    assert req.url.path.endswith("/responses")
    sent = json.loads(req.content)
    assert sent["store"] is False and sent["model"] == "gpt-6-luna"
    assert sent["reasoning"] == {"effort": "low"} and sent["max_output_tokens"] == 4000
    fmt = sent["text"]["format"]
    assert (fmt["type"], fmt["strict"]) == ("json_schema", True)
    assert fmt["schema"]["additionalProperties"] is False
    assert set(fmt["schema"]["required"]) == set(VacancyFields.model_fields)
    assert VacancyFields.model_validate_json(raw.text).title == "Backend Engineer"
    assert (raw.refusal, raw.incomplete, raw.request_id) == (None, None, "req_abc")
    assert raw.response_model == "gpt-6-luna-2026-05-18"
    assert raw.usage is not None
    assert (raw.usage.input_tokens, raw.usage.cached_input_tokens) == (1200, 200)
    assert (raw.usage.output_tokens, raw.usage.reasoning_tokens) == (300, 100)


async def test_refusal_and_truncation_are_reported() -> None:
    refused = await complete(
        provider(Recorder(200, response_json(content=[{"type": "refusal", "refusal": "no"}])))
    )
    assert (refused.refusal, refused.text) == ("no", "")
    cut = await complete(
        provider(
            Recorder(
                200,
                response_json(status="incomplete", incomplete_details={"reason": "max_output_tokens"}),
            )
        )
    )
    assert cut.incomplete == "max_output_tokens"


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        (429, "insufficient_quota", OutOfCreditsError),
        (429, "credit_balance_exhausted", OutOfCreditsError),
        (429, "rate_limit_exceeded", LLMTransientError),
        (408, None, LLMTransientError),
        (409, None, LLMTransientError),
        (500, None, LLMTransientError),
        (503, None, LLMTransientError),
        (400, "invalid_request_error", LLMPermanentError),
        (401, "invalid_api_key", LLMPermanentError),
        (404, "model_not_found", LLMPermanentError),
    ],
)
async def test_errors_are_classified_and_never_retried(status: int, code: str | None, expected: type) -> None:
    handler = Recorder(status, {"error": {"message": "boom", "type": "x", "code": code, "param": None}})
    with pytest.raises(expected) as err:
        await complete(provider(handler))
    if expected is LLMTransientError:
        assert not isinstance(err.value, OutOfCreditsError)
    assert len(handler.requests) == 1  # max_retries=0: the engine is the only retry layer


async def test_connection_failure_is_transient() -> None:
    handler = Recorder(0, httpx2.ConnectError("refused"))
    with pytest.raises(LLMTransientError):
        await complete(provider(handler))
    assert len(handler.requests) == 1


def test_text_format_is_the_sdk_strict_schema() -> None:
    fmt = text_format_for(VacancyFields)
    assert fmt["name"] == "VacancyFields" and fmt["strict"] is True


async def test_a_failed_response_is_reported_with_its_usage() -> None:
    body = response_json(status="failed", error={"code": "server_error", "message": "model crashed"})
    raw = await complete(provider(Recorder(200, body)))
    assert raw.incomplete is not None and "model crashed" in raw.incomplete
    assert raw.usage is not None and raw.usage.input_tokens == 1200


async def test_a_timeout_is_its_own_class() -> None:
    handler = Recorder(0, httpx2.ReadTimeout("slow"))
    with pytest.raises(LLMTimeoutError):
        await complete(provider(handler))
