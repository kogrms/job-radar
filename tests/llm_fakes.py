"""A scripted fake provider: each `complete` call pops the next scripted answer (a RawCompletion, an exception
to raise, or a callable computing one from the request). Records every request."""

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from job_radar.llm.provider import Message, RawCompletion, Usage

USAGE = Usage(input_tokens=1000, cached_input_tokens=0, output_tokens=200, reasoning_tokens=50)


def answer(payload: dict[str, Any] | str, *, usage: Usage = USAGE) -> RawCompletion:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return RawCompletion(
        text=text, refusal=None, incomplete=None, usage=usage, response_model="fake-snap", request_id="req_1"
    )


def refusal(reason: str = "I can't help with that") -> RawCompletion:
    return RawCompletion(
        text="", refusal=reason, incomplete=None, usage=USAGE, response_model="fake-snap", request_id="req_r"
    )


def truncated(text: str = '{"title": "Eng') -> RawCompletion:
    return RawCompletion(
        text=text,
        refusal=None,
        incomplete="max_output_tokens",
        usage=USAGE,
        response_model="fake-snap",
        request_id="req_t",
    )


def fields(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": "Backend Engineer",
        "seniority": "senior",
        "location_quote": None,
        "workplace_type": "remote",
        "countries": [],
        "regions": [],
        "salary": None,
        "stack": ["Python"],
        "language": "en",
    }
    return {**base, **overrides}


@dataclass
class Request:
    model: str
    instructions: str
    messages: list[Message]
    schema: type[BaseModel]


@dataclass
class FakeProvider:
    script: list[Any] = field(default_factory=list)
    default: Callable[[Request], RawCompletion] | None = None
    hang: asyncio.Event | None = None  # when set, every call blocks until cancelled
    requests: list[Request] = field(default_factory=list)

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
        req = Request(model, instructions, list(messages), schema)
        self.requests.append(req)
        if self.hang is not None:
            await asyncio.Event().wait()
        item = self.script.pop(0) if self.script else self.default
        if item is None:
            raise AssertionError("fake provider: script exhausted")
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item(req)  # type: ignore[no-any-return]
        assert isinstance(item, RawCompletion)
        return item

    async def close(self) -> None:
        return None
