"""Lever postings API. Site names are case-sensitive; EU sites live on api.eu.lever.co."""

from typing import Any

from job_radar.collectors.base import BoardFetch, html_to_text, join_locations, make_posting, unique_by_id


def board_url(org: str, region: str | None = None) -> str:
    host = "api.eu.lever.co" if region == "eu" else "api.lever.co"
    return f"https://{host}/v0/postings/{org}?mode=json"


def _text(j: dict[str, Any]) -> str:
    parts = [j.get("descriptionPlain") or html_to_text(j.get("description") or "")]
    for block in j.get("lists") or []:
        if isinstance(block, dict):
            parts.append(f"{block.get('text', '')}\n{html_to_text(block.get('content') or '')}")
    parts.append(j.get("additionalPlain") or html_to_text(j.get("additional") or ""))
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


def parse(data: Any, org: str, company: str) -> BoardFetch:
    if not isinstance(data, list):
        return BoardFetch.failed("unexpected shape")
    postings = []
    for j in data:
        cats = j.get("categories") or {}
        postings.append(
            make_posting(
                job_id=j["id"],
                url=j.get("hostedUrl", ""),
                title=j.get("text", ""),
                company=company,
                location_raw=join_locations(
                    cats.get("location"), cats.get("allLocations") or [], j.get("workplaceType")
                ),
                raw_text=_text(j),
                payload=j,
            )
        )
    return BoardFetch(unique_by_id(postings, f"lever/{org}"), complete=True)
