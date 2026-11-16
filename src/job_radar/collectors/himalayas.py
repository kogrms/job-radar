"""Himalayas public jobs API (aggregator): search, not a single company board.

Terms (himalayas.app/terms, clause 113, checked 2026-11-11): use `applicationLink` unchanged for every apply
action, show Himalayas as the source, never pass these jobs to third parties (so they never go into a
published dataset). Website scraping is prohibited; only the API is used. Data refreshes daily, so it is
polled once a day.

One "board" = one country (org, e.g. "RS") with several search queries. The board's listing is the union of
all queries' results, de-duplicated by job guid, so a job found by two queries is one posting. The fetch is
complete only if every query returned jobs, was read to its empty last page, and its own unique count (not the
union so far) is within a small tolerance of `totalCount`: live, the counter is 0-2 above what the API returns
(expired or hidden jobs). See docs/decisions.md, 2026-11-11.
"""

import asyncio
from typing import Any

import httpx

from job_radar.collectors.base import BoardFetch, FetchFailed, Sleep, get_json, html_to_text, make_posting

PAGE_SIZE = 20
MAX_PAGES = 60  # 1200 jobs per query; hitting it means the listing is incomplete
PAGE_DELAY_S = 1.0  # polite pacing without an API key


def total_tolerance(total: int) -> int:
    return max(3, total // 50)  # 3 jobs or 2%


def search_url(query: str, country: str, page: int) -> str:
    params = httpx.QueryParams({"q": query, "country": country, "page": page})
    return f"https://himalayas.app/jobs/api/search?{params}"


def _location(j: dict[str, Any]) -> str:
    places = [str(x) for x in j.get("locationRestrictions") or [] if x]
    zones = [z for z in j.get("timezoneRestrictions") or [] if isinstance(z, int | float)]
    parts = ["; ".join(places) if places else "No location restrictions listed"]
    if zones:
        parts.append("Time zones: " + ", ".join(f"UTC{z:+g}" for z in zones))
    return " | ".join(parts)


def to_posting(j: dict[str, Any]) -> Any:
    return make_posting(
        job_id=j["guid"],
        url=j["applicationLink"],  # terms: the apply link must be Himalayas' own, unchanged
        title=j.get("title", ""),
        company=j.get("companyName", ""),
        location_raw=_location(j),
        raw_text=html_to_text(j.get("description") or j.get("excerpt") or ""),
        payload=j,
    )


async def fetch(
    client: httpx.AsyncClient,
    country: str,
    queries: list[str],
    *,
    sleep: Sleep = asyncio.sleep,
    page_delay: float = PAGE_DELAY_S,
) -> BoardFetch:
    if not queries:
        return BoardFetch.failed("no queries configured")
    by_guid: dict[str, Any] = {}
    complete = True
    for query in queries:
        seen: set[str] = set()
        total: int | None = None
        for page in range(1, MAX_PAGES + 2):
            if page > MAX_PAGES:
                complete = False  # more pages than we are willing to read
                break
            try:
                data = await get_json(client, search_url(query, country, page), sleep=sleep)
            except FetchFailed as e:
                if not by_guid:
                    return BoardFetch.failed(f"query {query!r} page {page}: {e}")
                complete = False  # keep what we have; storage closes nothing on an incomplete fetch
                break
            if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
                return BoardFetch.failed("unexpected shape")
            if isinstance(data.get("totalCount"), int):
                total = data["totalCount"]
            if not data["jobs"]:
                if page == 1:
                    # A broad query with zero results is far likelier a transient or cached empty answer than
                    # a real empty market; trusting it would close every job only this query finds.
                    complete = False
                break
            for j in data["jobs"]:
                try:
                    posting = to_posting(j)
                except (KeyError, TypeError, AttributeError) as e:
                    return BoardFetch.failed(f"parse: {type(e).__name__}: {e}")
                seen.add(posting.source_job_id)
                by_guid.setdefault(posting.source_job_id, posting)
            await sleep(page_delay)
        if total is None or len(seen) < total - total_tolerance(total):
            complete = False  # a real gap (truncated or shifted results), or the API stopped reporting totals
    return BoardFetch(list(by_guid.values()), complete=complete)
