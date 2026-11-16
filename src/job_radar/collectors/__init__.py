"""ATS collectors.

Each module knows its board URL and how to parse the response; HTTP and retries are shared.
"""

from typing import Any, Protocol

import httpx

from job_radar.collectors import ashby, greenhouse, himalayas, lever, workable
from job_radar.collectors.base import BoardFetch, FetchFailed, Sleep, get_json


class Collector(Protocol):
    def board_url(self, org: str, region: str | None = None) -> str: ...
    def parse(self, data: Any, org: str, company: str) -> BoardFetch: ...


COLLECTORS: dict[str, Collector] = {
    "ashby": ashby,
    "greenhouse": greenhouse,
    "lever": lever,
    "workable": workable,
}

# Ashby and Greenhouse ignore case in org names (checked live 2026-11-11), so "AcmeCorp" and
# "acmecorp" are one board; Lever and Workable answer 404 for a wrong case, so their names are kept
# exactly as configured.
_CASE_INSENSITIVE = {"ashby", "greenhouse"}


def canonical_org(source: str, org: str) -> str:
    org = org.strip()
    if source == "himalayas":
        return org.upper()  # ISO country code
    return org.lower() if source in _CASE_INSENSITIVE else org


async def fetch_board(
    client: httpx.AsyncClient,
    source: str,
    org: str,
    company: str,
    region: str | None = None,
    queries: list[str] | None = None,
    *,
    sleep: Sleep | None = None,
) -> BoardFetch:
    if source == "himalayas":  # a multi-query search, not a single board URL
        return await (
            himalayas.fetch(client, org, queries or [], sleep=sleep)
            if sleep
            else himalayas.fetch(client, org, queries or [])
        )
    collector = COLLECTORS[source]
    url = collector.board_url(org, region)
    try:
        data = await (get_json(client, url, sleep=sleep) if sleep else get_json(client, url))
    except FetchFailed as e:
        return BoardFetch.failed(str(e))
    try:
        return collector.parse(data, org, company)
    except (
        KeyError,
        TypeError,
        AttributeError,
    ) as e:  # a job without id etc.: shape changed, do not trust it
        return BoardFetch.failed(f"parse: {type(e).__name__}: {e}")
