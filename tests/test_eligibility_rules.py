"""Rules against the synthetic challenge set (evals/eligibility_challenge.csv). No owner data here: the real
benchmark runs in scripts/eval_eligibility.py on private labels.

Each challenge row has the true label; `rules_answer` is set only where the rules are known to answer
differently (a documented limitation, mostly the safe "unclear"). The test pins the rules to exactly that
answer, so an improvement shows up as a failing test to update, and a regression as a failing test to fix.
"""

import csv
from pathlib import Path

import pytest

from job_radar.eligibility.rules import classify

CHALLENGE = Path(__file__).parent.parent / "evals" / "eligibility_challenge.csv"
ROWS = list(csv.DictReader(CHALLENGE.open(encoding="utf-8")))


@pytest.mark.parametrize("row", ROWS, ids=[f"{r['id']}:{r['text'][:40]}" for r in ROWS])
def test_challenge_row(row: dict[str, str]) -> None:
    expected = row["rules_answer"] or row["label"]
    assert classify(row["text"]).label == expected, row["why"]


def test_challenge_set_is_well_formed() -> None:
    labels = {"yes", "no", "unclear"}
    assert len(ROWS) >= 90
    assert all(r["label"] in labels and r["rules_answer"] in labels | {""} for r in ROWS)
    assert len({r["text"] for r in ROWS}) == len(ROWS)


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        ("Russian nationals are not eligible", "russian_nationals_excluded"),
        ("Remote, not in Serbia", "serbia_negated"),
        ("Hybrid, 3 days in our Novi Sad office", "novi_sad_office"),
        ("Belgrade, Serbia — hybrid", "serbia_named"),
        ("We consider candidates from Russia and other countries", "russia_welcome"),
        ("Worldwide except Russia", "worldwide"),
        ("Global team, US only", "global_with_places"),
        ("Remote (UTC-3 to UTC+3)", "tz_band"),
        ("Remote, CET working hours", "tz_named"),
        ("Remote (US), CET overlap", "tz_named_with_places"),
        ("Remote - European Union", "eu_bloc"),
        ("Remote work available: No", "no_remote_visa_relocation"),
        ("Relocation package to our Lisbon hub", "relocation"),
        ("Remote - EMEA", "region_only"),
        ("Remote (North America)", "other_region"),
        ("Remote, USA", "places_without_serbia"),
        ("Remote", "fallback"),
        ("", "empty"),
    ],
)
def test_rule_names(text: str, rule: str) -> None:
    assert classify(text).rule == rule


def test_tz_band_with_places() -> None:
    assert classify("UTC+0 to UTC+3, Poland").rule == "tz_band_with_places"


def test_no_input_raises() -> None:
    for text in [
        "UTC x1 to UTC y2",
        "UTC.3 to UTC.4",
        "UTC+99 to UTC-99",
        "GMT + 5:3 to + 1.5",
        "\x00",
        "—" * 1000,
    ]:
        classify(text)
