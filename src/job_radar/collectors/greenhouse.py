"""Greenhouse job board API. EU boards (job-boards.eu.greenhouse.io) are served by the same API host."""

import html
from typing import Any

from job_radar.collectors.base import BoardFetch, html_to_text, join_locations, make_posting, unique_by_id


def board_url(org: str, region: str | None = None) -> str:
    return f"https://boards-api.greenhouse.io/v1/boards/{org}/jobs?content=true"


def parse(data: Any, org: str, company: str) -> BoardFetch:
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        return BoardFetch.failed("unexpected shape")
    postings = []
    for j in data["jobs"]:
        offices = [o.get("name") for o in j.get("offices") or [] if isinstance(o, dict)]
        postings.append(
            make_posting(
                job_id=j["id"],
                url=j.get("absolute_url", ""),
                title=j.get("title", ""),
                company=j.get("company_name") or company,
                location_raw=join_locations((j.get("location") or {}).get("name"), offices),
                # `content` is HTML-escaped HTML ("&lt;p&gt;"): unescape first, or the tags survive as text.
                raw_text=html_to_text(html.unescape(j.get("content") or "")),
                payload=j,
            )
        )
    # meta.total is the board's own count: fewer jobs than that means a truncated listing, not closures.
    # Every live response has it; if it is ever missing, fail closed: store what we got, close nothing.
    total = (data.get("meta") or {}).get("total")
    complete = isinstance(total, int) and total == len(data["jobs"])
    return BoardFetch(unique_by_id(postings, f"greenhouse/{org}"), complete=complete)
