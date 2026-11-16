"""One synthetic case per rule. No owner data here: the real benchmark runs in scripts/eval_eligibility.py."""

import pytest

from job_radar.eligibility.rules import classify

CASES = [
    ("Remote (Global); we cannot hire from Russia or Belarus", "no", "russia_excluded"),
    ("Remote; не берут из РФ и РБ", "no", "russia_excluded"),
    ("Office hub in Bulgaria — not Serbia", "no", "serbia_negated"),
    ("Hybrid, 3 days in our Novi Sad office", "no", "novi_sad_office"),
    ("Remote; Belgrade, Novi Sad, Yerevan", "yes", "serbia_named"),
    ("Belgrade, Serbia — hybrid", "yes", "serbia_named"),
    ("Список стран включает Сербию и Грузию", "yes", "serbia_named"),
    ("Remote (Rad od kuće)", "yes", "serbia_named"),
    ("We consider candidates from Russia and other countries", "yes", "russia_welcome"),
    ("Remote — Anywhere in the world", "yes", "worldwide"),
    ("Remote (UTC-3 to UTC+3)", "yes", "tz_band"),
    ("Remote, GMT-8 to GMT-4 only", "no", "tz_band"),
    ("Remote, CET working hours", "yes", "tz_named"),
    ("Remote - European Union", "no", "eu_bloc"),
    ("Remote, EU timezones", "yes", "tz_named"),
    (
        "We are currently unable to offer remote work, visa sponsorship or relocation",
        "no",
        "no_remote_visa_relocation",
    ),
    ("Relocation package to our Lisbon hub", "no", "relocation"),
    ("Remote - EMEA", "unclear", "region_only"),
    ("Remote (North America)", "no", "other_region"),
    ("Remote; Bulgaria; Poland; United Kingdom", "no", "places_without_serbia"),
    ("Remote", "unclear", "fallback"),
    ("", "unclear", "empty"),
]


@pytest.mark.parametrize(("text", "label", "rule"), CASES, ids=[c[2] + ":" + c[0][:30] for c in CASES])
def test_rules(text: str, label: str, rule: str) -> None:
    v = classify(text)
    assert (v.label, v.rule) == (label, rule), v


def test_relocation_alongside_remote_is_not_a_no() -> None:
    assert classify("Remote. No relocation offered").label != "no"


def test_international_alone_is_not_worldwide() -> None:
    assert classify("Relocation support in case of international mobility").rule != "worldwide"


@pytest.mark.xfail(
    strict=True,
    reason="known limitation of rules: a city named as an optional relocation target "
    "reads as the only allowed location. An LLM classifier should get this right.",
)
def test_remote_or_optional_relocation_is_not_a_no() -> None:
    assert classify("100% remote, or sponsored relocation to London or Dubai").label != "no"
