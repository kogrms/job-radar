"""Ashby public job board API: one call returns every listed posting with its description."""

from typing import Any

from job_radar.collectors.base import BoardFetch, html_to_text, join_locations, make_posting, unique_by_id


def board_url(org: str, region: str | None = None) -> str:
    return f"https://api.ashbyhq.com/posting-api/job-board/{org}?includeCompensation=true"


def parse(data: Any, org: str, company: str) -> BoardFetch:
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        return BoardFetch.failed("unexpected shape")
    postings = []
    for j in data["jobs"]:
        if not j.get("isListed", True):
            continue
        remote = "Remote" if j.get("isRemote") else None
        secondary = [s.get("location") for s in j.get("secondaryLocations") or [] if isinstance(s, dict)]
        postings.append(
            make_posting(
                job_id=j["id"],
                url=j.get("jobUrl") or f"https://jobs.ashbyhq.com/{org}/{j['id']}",
                title=j.get("title", ""),
                company=company,
                location_raw=join_locations(j.get("location"), secondary, j.get("workplaceType"), remote),
                raw_text=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml") or ""),
                payload=j,
            )
        )
    return BoardFetch(unique_by_id(postings, f"ashby/{org}"), complete=True)
