"""Shared pieces of the ATS collectors: the fetch result, HTTP with retries, text cleanup.

Contract with storage (see job_radar.store): `complete=True` only when the whole board listing was read and
parsed. A 404, a network failure, a non-JSON body (HTML error page), an empty body or an unexpected shape is a
*failed* fetch: no postings, `complete=False`, and storage is not called at all, so nothing gets closed.
A valid JSON listing with zero jobs is a legitimately empty board and is complete.
"""

import asyncio
import html
import json
import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from job_radar.postings import Posting

log = logging.getLogger(__name__)

USER_AGENT = "job-radar/0.1 (personal job-search pipeline)"
TIMEOUT_S = 20.0
RETRY_DELAYS_S = (1.0, 4.0, 15.0)  # after attempts 1, 2, 3; then give up
MAX_RETRY_AFTER_S = 60.0

Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class BoardFetch:
    postings: list[Posting] = field(default_factory=list)
    complete: bool = False
    error: str | None = None  # set iff the fetch failed

    @classmethod
    def failed(cls, error: str) -> "BoardFetch":
        return cls(error=error)


class FetchFailed(Exception):
    pass


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=TIMEOUT_S,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        follow_redirects=True,
    )


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    if value and value.strip().isdigit():
        return min(float(value), MAX_RETRY_AFTER_S)
    return None


async def get_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    retry_delays: Sequence[float] = RETRY_DELAYS_S,
    sleep: Sleep = asyncio.sleep,
) -> Any:
    """GET and parse JSON. Retries 429, 5xx and transport errors; raises FetchFailed on anything else."""
    attempts = len(retry_delays) + 1
    for attempt in range(attempts):
        last = attempt == attempts - 1
        try:
            resp = await client.get(url)
        except httpx.TooManyRedirects as e:
            raise FetchFailed("too many redirects") from e
        except (httpx.TransportError, httpx.DecodingError) as e:  # DecodingError: e.g. truncated gzip body
            if last:
                raise FetchFailed(f"transport: {type(e).__name__}") from e
            await sleep(retry_delays[attempt])
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            if last:
                raise FetchFailed(f"http {resp.status_code} after {attempts} attempts")
            await sleep(_retry_after(resp) or retry_delays[attempt])
            continue
        if resp.status_code != 200:
            raise FetchFailed(f"http {resp.status_code}")
        if not resp.content.strip():
            raise FetchFailed("empty body")
        try:
            return resp.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise FetchFailed(f"not json: {resp.text[:80]!r}") from e
    raise AssertionError("unreachable")


_BLOCK_END = re.compile(r"(?i)<\s*(br\s*/?|/p|/div|/h[1-6]|/tr|/ul|/ol)\s*>")
_LI_START = re.compile(r"(?i)<\s*li\b[^>]*>")
_TAG = re.compile(r"(?s)<[^>]+>")
_SCRIPT = re.compile(r"(?is)<(script|style)\b.*?</\1>")
_SURROGATE = re.compile(r"[\ud800-\udfff]")


def html_to_text(fragment: str) -> str:
    """Readable plain text from an HTML fragment: block ends become newlines, bullets keep a marker."""
    s = _SCRIPT.sub(" ", fragment)
    s = _LI_START.sub("\n- ", s)
    s = _BLOCK_END.sub("\n", s)
    s = html.unescape(_TAG.sub(" ", s))
    lines = (re.sub("[ \t\u00a0]+", " ", line).strip() for line in s.splitlines())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def clean(value: Any) -> Any:
    """Make API data safe for Postgres TEXT/JSONB: drop NULs, replace lone surrogates. Recurses into JSON."""
    if isinstance(value, str):
        return _SURROGATE.sub("�", value.replace("\x00", ""))
    if isinstance(value, list):
        return [clean(v) for v in value]
    if isinstance(value, dict):
        return {clean(k): clean(v) for k, v in value.items()}
    return value


def join_locations(*parts: Any) -> str:
    """'; '-joined unique non-empty location strings, order kept. Raw wording, no interpretation."""
    seen: list[str] = []
    for p in parts:
        for item in p if isinstance(p, list) else [p]:
            s = str(item).strip() if item is not None else ""
            if s and s not in seen:
                seen.append(s)
    return "; ".join(seen)


def make_posting(
    *,
    job_id: Any,
    url: str,
    title: str,
    company: str,
    location_raw: str,
    raw_text: str,
    payload: dict[str, Any],
) -> Posting:
    return Posting(
        source_job_id=clean(str(job_id)),
        url=clean(url),
        title=clean(title.strip()),
        company=clean(company),
        location_raw=clean(location_raw),
        raw_text=clean(raw_text),
        raw_payload=clean(payload),
    )


def unique_by_id(postings: list[Posting], board: str) -> list[Posting]:
    """Keep the first of postings sharing an id (an API quirk); storage rejects duplicates in one fetch."""
    seen: set[str] = set()
    out: list[Posting] = []
    for p in postings:
        if p.source_job_id in seen:
            log.warning("duplicate job id %s on %s, keeping the first", p.source_job_id, board)
            continue
        seen.add(p.source_job_id)
        out.append(p)
    return out
