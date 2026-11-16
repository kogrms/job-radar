"""Collectors against synthetic responses shaped like the real APIs (shapes checked live 2026-11-11)."""

import html
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from job_radar.boards import load_boards
from job_radar.collectors import canonical_org, fetch_board
from job_radar.collectors.base import BoardFetch, html_to_text, make_client
from job_radar.config import Settings

ASHBY = {
    "apiVersion": "1",
    "jobs": [
        {
            "id": "a1",
            "title": "Senior Backend Engineer",
            "location": "Remote - Europe",
            "secondaryLocations": [{"location": "Belgrade, Serbia"}],
            "isListed": True,
            "isRemote": True,
            "workplaceType": "Remote",
            "jobUrl": "https://jobs.ashbyhq.com/examplecorp/a1",
            "descriptionPlain": "Build things.\n\nWe hire across Europe.",
            "descriptionHtml": "<p>ignored when plain exists</p>",
        },
        {"id": "a2", "title": "Hidden", "location": "X", "isListed": False, "descriptionPlain": "x"},
    ],
}
GREENHOUSE = {
    "jobs": [
        {
            "id": 101,
            "title": "Data Engineer",
            "absolute_url": "https://job-boards.eu.greenhouse.io/examplecorp/jobs/101",
            "location": {"name": "Remote, EMEA"},
            "offices": [{"name": "Amsterdam"}],
            "company_name": "Example Corp",
            "content": html.escape("<h2>About</h2><p>We &amp; you.</p><ul><li>Python</li><li>SQL</li></ul>"),
        }
    ],
    "meta": {"total": 1},
}
LEVER = [
    {
        "id": "l1",
        "text": "ML Engineer",
        "hostedUrl": "https://jobs.lever.co/ExampleCorp/l1",
        "categories": {"location": "Berlin", "allLocations": ["Berlin", "Remote - Germany"]},
        "workplaceType": "hybrid",
        "descriptionPlain": "Intro.",
        "lists": [{"text": "Requirements", "content": "<li>Python</li><li>Docker</li>"}],
        "additionalPlain": "Benefits.",
    }
]
WORKABLE = {
    "name": "Example Corp Ltd",
    "jobs": [
        {
            "shortcode": "ABC123",
            "title": "Platform Engineer",
            "url": "https://apply.workable.com/j/ABC123",
            "telecommuting": True,
            "locations": [
                {"city": "London", "region": "England", "country": "United Kingdom", "hidden": False},
                {"city": "Secret", "country": "Nowhere", "hidden": True},
            ],
            "description": "<p>Run the platform.</p>",
        }
    ],
}

URLS = {
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/examplecorp?includeCompensation=true",
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/examplecorp/jobs?content=true",
    "lever": "https://api.lever.co/v0/postings/ExampleCorp?mode=json",
    "workable": "https://apply.workable.com/api/v1/widget/accounts/examplecorp?details=true",
}
ORGS = {
    "ashby": "examplecorp",
    "greenhouse": "examplecorp",
    "lever": "ExampleCorp",
    "workable": "examplecorp",
}


async def no_sleep(_: float) -> None:
    return None


async def fetch(source: str, region: str | None = None) -> BoardFetch:
    async with make_client() as client:
        return await fetch_board(client, source, ORGS[source], "Example Corp", region, sleep=no_sleep)


@respx.mock
async def test_ashby_listed_jobs_with_all_locations() -> None:
    respx.get(URLS["ashby"]).respond(json=ASHBY)
    f = await fetch("ashby")
    assert f.complete and f.error is None
    [p] = f.postings  # the unlisted job is skipped
    assert (p.source_job_id, p.title, p.url) == (
        "a1",
        "Senior Backend Engineer",
        "https://jobs.ashbyhq.com/examplecorp/a1",
    )
    assert p.location_raw == "Remote - Europe; Belgrade, Serbia; Remote"
    assert p.raw_text == "Build things.\n\nWe hire across Europe."
    assert p.raw_payload["id"] == "a1"


@respx.mock
async def test_greenhouse_unescapes_html_and_checks_total() -> None:
    respx.get(URLS["greenhouse"]).respond(json=GREENHOUSE)
    f = await fetch("greenhouse")
    [p] = f.postings
    assert f.complete
    assert p.source_job_id == "101" and p.location_raw == "Remote, EMEA; Amsterdam"
    assert p.raw_text == "About\nWe & you.\n\n- Python\n- SQL"


@respx.mock
async def test_greenhouse_truncated_listing_is_not_complete() -> None:
    respx.get(URLS["greenhouse"]).respond(json={**GREENHOUSE, "meta": {"total": 5}})
    f = await fetch("greenhouse")
    assert len(f.postings) == 1 and not f.complete


@respx.mock
async def test_lever_keeps_case_joins_lists_and_uses_eu_host() -> None:
    respx.get(URLS["lever"]).respond(json=LEVER)
    [p] = (await fetch("lever")).postings
    assert p.location_raw == "Berlin; Remote - Germany; hybrid"
    assert p.raw_text == "Intro.\n\nRequirements\n- Python\n- Docker\n\nBenefits."
    eu = respx.get("https://api.eu.lever.co/v0/postings/ExampleCorp?mode=json").respond(json=[])
    assert (await fetch("lever", region="eu")).complete and eu.called


@respx.mock
async def test_workable_skips_hidden_locations_and_marks_remote() -> None:
    respx.get(URLS["workable"]).respond(json=WORKABLE)
    [p] = (await fetch("workable")).postings
    assert (p.source_job_id, p.company) == ("ABC123", "Example Corp Ltd")
    assert p.location_raw == "London, England, United Kingdom; Remote"
    assert p.raw_text == "Run the platform."


@pytest.mark.parametrize("source", list(URLS))
@respx.mock
async def test_empty_board_is_a_complete_fetch(source: str) -> None:
    empty: Any = {
        "ashby": {"jobs": []},
        "greenhouse": {"jobs": [], "meta": {"total": 0}},
        "lever": [],
        "workable": {"jobs": []},
    }
    respx.get(URLS[source]).respond(json=empty[source])
    f = await fetch(source)
    assert f.complete and f.postings == [] and f.error is None


COMMON_FAILURES: list[tuple[str, list[Any]]] = [
    ("http 404", [httpx.Response(404, text="Not Found")]),
    ("http 503 after 4 attempts", [httpx.Response(503)] * 4),
    ("not json", [httpx.Response(200, text="<html>Maintenance</html>")]),
    ("empty body", [httpx.Response(200, text="  ")]),
    ("too many redirects", [httpx.TooManyRedirects("loop")]),
    ("transport: ConnectError after", [httpx.ConnectError("down")] * 4),
]
# Per-ATS bodies that are valid JSON but must not be trusted: wrong top-level shape, a job without its id.
BAD_SHAPE: dict[str, Any] = {
    "ashby": {"error": "x"},
    "greenhouse": {"status": 404},
    "lever": {"ok": False},
    "workable": {"error": "x"},
}
NO_ID: dict[str, Any] = {
    "ashby": {"jobs": [{"title": "no id"}]},
    "greenhouse": {"jobs": [{"title": "no id"}], "meta": {"total": 1}},
    "lever": [{"text": "no id"}],
    "workable": {"jobs": [{"title": "no id"}]},
}
CASES = [
    (src, expected, responses)
    for src in URLS
    for expected, responses in [
        *COMMON_FAILURES,
        ("unexpected shape", [httpx.Response(200, json=BAD_SHAPE[src])]),
        ("parse: KeyError", [httpx.Response(200, json=NO_ID[src])]),
    ]
]


@pytest.mark.parametrize(("source", "expected", "responses"), CASES, ids=[f"{c[0]}-{c[1]}" for c in CASES])
@respx.mock
async def test_failures_are_never_complete(source: str, expected: str, responses: list[Any]) -> None:
    respx.get(URLS[source]).mock(side_effect=responses)
    f = await fetch(source)
    assert not f.complete and f.postings == []
    assert f.error is not None and f.error.startswith(expected.removesuffix(" after")), f.error


@respx.mock
async def test_greenhouse_without_total_fails_closed() -> None:
    respx.get(URLS["greenhouse"]).respond(json={"jobs": GREENHOUSE["jobs"]})
    f = await fetch("greenhouse")
    assert len(f.postings) == 1 and not f.complete


@respx.mock
async def test_decoding_error_is_retried() -> None:
    respx.get(URLS["ashby"]).mock(
        side_effect=[httpx.DecodingError("truncated gzip"), httpx.Response(200, json=ASHBY)]
    )
    f = await fetch("ashby")
    assert f.complete and len(f.postings) == 1


def test_empty_env_var_means_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOARDS_FILE", "")
    monkeypatch.setenv("PRIVATE_DIR", "")
    s = Settings(_env_file=None)
    assert s.boards_file is None and s.private_dir is None


@respx.mock
async def test_retries_429_and_honours_retry_after() -> None:
    respx.get(URLS["ashby"]).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(502),
            httpx.Response(200, json=ASHBY),
        ]
    )
    delays: list[float] = []

    async def record(d: float) -> None:
        delays.append(d)

    async with make_client() as client:
        f = await fetch_board(client, "ashby", "examplecorp", "Example Corp", sleep=record)
    assert f.complete and len(f.postings) == 1
    assert delays == [7.0, 4.0]  # Retry-After wins over the default 1 s; then the second default delay


@respx.mock
async def test_nul_and_lone_surrogates_are_cleaned() -> None:
    job = {**ASHBY["jobs"][0], "descriptionPlain": "a\x00b\ud800c", "extra": {"k\x00": ["v\x00"]}}
    # Sent as escaped JSON ("\\u0000", "\\ud800"), exactly how an API would put them on the wire.
    body = json.dumps({"jobs": [job]}).encode()
    respx.get(URLS["ashby"]).respond(content=body, headers={"Content-Type": "application/json"})
    [p] = (await fetch("ashby")).postings
    assert p.raw_text == "ab�c"
    assert p.raw_payload["extra"] == {"k": ["v"]}


@respx.mock
async def test_duplicate_ids_keep_the_first() -> None:
    first = ASHBY["jobs"][0]
    respx.get(URLS["ashby"]).respond(json={"jobs": [first, {**first, "title": "Second copy"}]})
    [p] = (await fetch("ashby")).postings
    assert p.title == "Senior Backend Engineer"


def test_canonical_org_lowercases_only_case_insensitive_ats() -> None:
    assert canonical_org("ashby", " AcmeCorp ") == "acmecorp"
    assert canonical_org("greenhouse", "GlobexInc") == "globexinc"
    assert canonical_org("lever", "Initech") == "Initech"
    assert canonical_org("workable", "UmbrellaCo") == "UmbrellaCo"


def test_load_boards_canonicalizes_and_dedupes(tmp_path: Path) -> None:
    f = tmp_path / "boards.yml"
    f.write_text(
        "boards:\n"
        "  - {source: ashby, org: AcmeCorp, company: Acme Corp}\n"
        "  - {source: ashby, org: acmecorp, company: Acme Corp}\n"
        "  - {source: lever, org: initech, company: Initech, region: eu}\n"
    )
    boards = load_boards(f)
    assert [(b.source, b.org, b.region) for b in boards] == [
        ("ashby", "acmecorp", None),
        ("lever", "initech", "eu"),
    ]
    f.write_text("boards:\n  - {source: smartrecruiters, org: x, company: X}\n")
    with pytest.raises(ValueError):
        load_boards(f)


def test_html_to_text_keeps_structure() -> None:
    assert html_to_text("<p>One</p><p>Two<br>Three</p><script>x()</script>") == "One\nTwo\nThree"


# --- Himalayas (aggregator search) ------------------------------------------------------------------------


def hjob(n: int, **over: Any) -> dict[str, Any]:
    guid = f"https://himalayas.app/companies/co{n}/jobs/role-{n}"
    return {
        "guid": guid,
        "applicationLink": f"{guid}?utm_source=api",
        "title": f"Engineer {n}",
        "companyName": f"Co {n}",
        "locationRestrictions": ["Serbia", "Croatia"],
        "timezoneRestrictions": [1, 2],
        "description": f"<p>Job {n}</p>",
        **over,
    }


def hpage(jobs: list[dict[str, Any]], total: int) -> httpx.Response:
    return httpx.Response(200, json={"jobs": jobs, "totalCount": total, "offset": 0, "limit": 20})


def hroute(query: str, page: int) -> Any:
    return respx.get(
        "https://himalayas.app/jobs/api/search", params={"q": query, "country": "RS", "page": str(page)}
    )


async def hfetch(queries: list[str]) -> BoardFetch:
    async with make_client() as client:
        return await fetch_board(client, "himalayas", "RS", "", None, queries, sleep=no_sleep)


@respx.mock
async def test_himalayas_pages_until_empty_and_merges_queries() -> None:
    hroute("python", 1).mock(return_value=hpage([hjob(1), hjob(2)], 3))
    hroute("python", 2).mock(return_value=hpage([hjob(3)], 3))
    hroute("python", 3).mock(return_value=hpage([], 3))
    hroute("llm", 1).mock(
        return_value=hpage([hjob(2), hjob(4, locationRestrictions=[], timezoneRestrictions=[])], 2)
    )
    hroute("llm", 2).mock(return_value=hpage([], 2))
    f = await hfetch(["python", "llm"])
    assert f.complete
    assert sorted(p.title for p in f.postings) == [
        "Engineer 1",
        "Engineer 2",
        "Engineer 3",
        "Engineer 4",
    ]  # job 2 once
    p1 = next(p for p in f.postings if p.title == "Engineer 1")
    assert p1.url.endswith("?utm_source=api")  # terms: their apply link, unchanged
    assert p1.company == "Co 1" and p1.raw_text == "Job 1"
    assert p1.location_raw == "Serbia; Croatia | Time zones: UTC+1, UTC+2"
    p4 = next(p for p in f.postings if p.title == "Engineer 4")
    assert p4.location_raw == "No location restrictions listed"


@respx.mock
async def test_himalayas_total_gap_beyond_tolerance_is_incomplete() -> None:
    hroute("python", 1).mock(return_value=hpage([hjob(1)], 50))  # says 50, delivered 1
    hroute("python", 2).mock(return_value=hpage([], 50))
    f = await hfetch(["python"])
    assert len(f.postings) == 1 and not f.complete


@respx.mock
async def test_himalayas_small_counter_gap_is_complete() -> None:
    # Live behaviour: totalCount 638, 636 returned — expired/hidden jobs are counted but not listed.
    hroute("python", 1).mock(return_value=hpage([hjob(1), hjob(2)], 4))
    hroute("python", 2).mock(return_value=hpage([], 4))
    f = await hfetch(["python"])
    assert len(f.postings) == 2 and f.complete


@respx.mock
async def test_himalayas_failure_after_first_page_keeps_data_but_is_incomplete() -> None:
    hroute("python", 1).mock(return_value=hpage([hjob(1)], 2))
    hroute("python", 2).mock(side_effect=[httpx.Response(503)] * 4)
    f = await hfetch(["python"])
    assert [p.title for p in f.postings] == ["Engineer 1"] and not f.complete and f.error is None


@respx.mock
async def test_himalayas_failure_on_first_page_is_a_failed_fetch() -> None:
    hroute("python", 1).mock(return_value=httpx.Response(404))
    f = await hfetch(["python"])
    assert f.error and not f.postings and not f.complete


async def test_himalayas_without_queries_is_a_failed_fetch() -> None:
    f = await hfetch([])
    assert f.error == "no queries configured"


@respx.mock
async def test_himalayas_page_cap_makes_the_fetch_incomplete(monkeypatch: pytest.MonkeyPatch) -> None:
    from job_radar.collectors import himalayas

    monkeypatch.setattr(himalayas, "MAX_PAGES", 2)
    hroute("python", 1).mock(return_value=hpage([hjob(1)], 3))
    hroute("python", 2).mock(return_value=hpage([hjob(2)], 3))
    f = await hfetch(["python"])
    assert len(f.postings) == 2 and not f.complete


def test_board_config_rules(tmp_path: Path) -> None:
    f = tmp_path / "boards.yml"
    f.write_text("boards:\n  - {source: himalayas, org: rs, queries: [python, llm]}\n")
    [b] = load_boards(f)
    assert (b.org, b.queries, b.company) == ("RS", ["python", "llm"], "")
    for bad in ["{source: himalayas, org: rs}", "{source: ashby, org: x}"]:
        f.write_text(f"boards:\n  - {bad}\n")
        with pytest.raises(ValueError):
            load_boards(f)


def hpages(jobs: list[dict[str, Any]], total: int | None) -> Any:
    """Route side effect serving `jobs` 20 per page for any query; total=None omits totalCount."""

    def respond(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        chunk = jobs[(page - 1) * 20 : page * 20]
        body: dict[str, Any] = {"jobs": chunk}
        if total is not None:
            body["totalCount"] = total
        return httpx.Response(200, json=body)

    return respond


@pytest.mark.parametrize(("seen", "complete"), [(980, True), (979, False)])
@respx.mock
async def test_himalayas_tolerance_boundary(seen: int, complete: bool) -> None:
    # total 1000 -> tolerance max(3, 2%) = 20 -> 980 is the lowest complete count
    respx.get("https://himalayas.app/jobs/api/search").mock(
        side_effect=hpages([hjob(n) for n in range(seen)], 1000)
    )
    f = await hfetch(["python"])
    assert len(f.postings) == seen and f.complete is complete


@respx.mock
async def test_himalayas_missing_total_is_incomplete() -> None:
    respx.get("https://himalayas.app/jobs/api/search").mock(side_effect=hpages([hjob(1)], None))
    assert not (await hfetch(["python"])).complete


@respx.mock
async def test_himalayas_query_with_no_results_is_incomplete() -> None:
    hroute("python", 1).mock(return_value=hpage([hjob(1)], 1))
    hroute("python", 2).mock(return_value=hpage([], 1))
    hroute("llm", 1).mock(return_value=hpage([], 0))  # transient / cached empty answer
    f = await hfetch(["python", "llm"])
    assert len(f.postings) == 1 and not f.complete


@respx.mock
async def test_himalayas_overlap_does_not_hide_a_truncated_query() -> None:
    first = [hjob(n) for n in range(40)]
    hroute("python", 1).mock(return_value=hpage(first[:20], 40))
    hroute("python", 2).mock(return_value=hpage(first[20:], 40))
    hroute("python", 3).mock(return_value=hpage([], 40))
    # "llm" claims 40 jobs but delivers 1; that one overlaps "python", so the union looks big enough.
    hroute("llm", 1).mock(return_value=hpage([first[0]], 40))
    hroute("llm", 2).mock(return_value=hpage([], 40))
    f = await hfetch(["python", "llm"])
    assert len(f.postings) == 40 and not f.complete


@respx.mock
async def test_himalayas_malformed_job_fails_the_fetch() -> None:
    hroute("python", 1).mock(return_value=hpage([hjob(1), {"title": "no guid"}], 2))
    f = await hfetch(["python"])
    assert f.error is not None and f.error.startswith("parse: KeyError") and not f.postings


@respx.mock
async def test_himalayas_waits_between_pages() -> None:
    hroute("python", 1).mock(return_value=hpage([hjob(1)], 1))
    hroute("python", 2).mock(return_value=hpage([], 1))
    delays: list[float] = []

    async def record(d: float) -> None:
        delays.append(d)

    async with make_client() as client:
        await fetch_board(client, "himalayas", "RS", "", None, ["python"], sleep=record)
    assert delays == [1.0]  # terms politeness: PAGE_DELAY_S after each non-empty page


@pytest.mark.parametrize(
    "bad",
    [
        "{source: himalayas, org: rs, queries: ['  ']}",
        "{source: himalayas, org: rs, queries: [python], region: eu}",
        "{source: ashby, org: x, company: X, queries: [python]}",
    ],
)
def test_board_config_rejects_misplaced_fields(tmp_path: Path, bad: str) -> None:
    f = tmp_path / "boards.yml"
    f.write_text(f"boards:\n  - {bad}\n")
    with pytest.raises(ValueError):
        load_boards(f)
