"""Workable public widget API (account names are case-sensitive)."""

from typing import Any

from job_radar.collectors.base import BoardFetch, html_to_text, join_locations, make_posting, unique_by_id


def board_url(org: str, region: str | None = None) -> str:
    return f"https://apply.workable.com/api/v1/widget/accounts/{org}?details=true"


def _place(loc: dict[str, Any]) -> str:
    return ", ".join(str(loc[k]) for k in ("city", "region", "country") if loc.get(k))


def parse(data: Any, org: str, company: str) -> BoardFetch:
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        return BoardFetch.failed("unexpected shape")
    postings = []
    for j in data["jobs"]:
        places = [
            _place(loc) for loc in j.get("locations") or [] if isinstance(loc, dict) and not loc.get("hidden")
        ]
        if not places:
            places = [_place({"city": j.get("city"), "region": j.get("state"), "country": j.get("country")})]
        postings.append(
            make_posting(
                job_id=j["shortcode"],
                url=j.get("url") or f"https://apply.workable.com/j/{j['shortcode']}",
                title=j.get("title", ""),
                company=data.get("name") or company,
                location_raw=join_locations(places, "Remote" if j.get("telecommuting") else None),
                raw_text=html_to_text(j.get("description") or ""),
                payload=j,
            )
        )
    return BoardFetch(unique_by_id(postings, f"workable/{org}"), complete=True)
