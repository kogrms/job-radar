"""The `extract` step (spec PR 7): input derivation, the quote check, shadow runs, and the full path through
the engine with a fake provider. Plus the PII stopgap and the injection fixtures' structural guarantees."""

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from job_radar.db import make_sessionmaker
from job_radar.extract.pii import mask_pii
from job_radar.extract.prompt import render_posting
from job_radar.extract.schema import VacancyFields
from job_radar.llm.client import LLM
from job_radar.llm.ledger import CallTags, Ledger, LedgerError
from job_radar.llm.pricing import cost_usd
from job_radar.llm.provider import LLMPermanentError, LLMTransientError
from job_radar.models import LLMCall
from job_radar.steps.eligibility_rules_step import EligibilityRulesStep
from job_radar.steps.engine import LLMBudget
from job_radar.steps.extract_step import ExtractStep, in_shadow_sample, quote_check
from tests.llm_fakes import USAGE, FakeProvider, Request, answer, fields
from tests.test_steps_engine import T0, check, fetch, make_steps, posting, runs

FIXTURES = [
    json.loads(line)
    for line in (Path(__file__).parent.parent / "evals/extract_injection.jsonl").read_text().splitlines()
]


def make_step(provider: FakeProvider, db: AsyncEngine | None = None, **kwargs: Any) -> ExtractStep:
    ledger = Ledger(make_sessionmaker(db)) if db is not None else None  # pure tests never call the LLM
    defaults: dict[str, Any] = {
        "model": "gpt-6-luna",
        "shadow_model": "gpt-6.1-sol",
        "shadow_rate": 0.0,
        "shadow_salt": "s",
        "reasoning_effort": "low",
        "max_output_tokens": 4000,
        "max_text_chars": 40000,
    }
    return ExtractStep(LLM(provider, ledger), **{**defaults, **kwargs})


def vacancy(vid: int = 1, text: str = "Remote within the EU. Python.", location: str = "Remote") -> Any:
    return SimpleNamespace(id=vid, title="Backend Engineer", location_raw=location, raw_text=text)


def quoting(quote: str | None) -> Any:
    return lambda req: answer(fields(location_quote=quote))


# -- pure parts ---------------------------------------------------------------------------------------------


def test_input_is_masked_truncated_and_stable() -> None:
    step = make_step(FakeProvider(), max_text_chars=60)
    v = vacancy(
        text="Write to jane@example.com or +381 11 123 4567. Salary 2024-2025: 100 000 - 150 000. " * 3
    )
    a, b = step.build_input(v, None), step.build_input(v, None)
    assert a is not None and b is not None and a.hash == b.hash
    assert "[email]" in a.payload["text"] and "[phone]" in a.payload["text"]
    assert "jane@example.com" not in a.payload["text"] and len(a.payload["text"]) <= 60
    assert (a.payload["model"], a.payload["shadow_model"]) == ("gpt-6-luna", None)


def test_model_or_prompt_change_changes_the_hash() -> None:
    v = vacancy()
    luna = make_step(FakeProvider()).build_input(v, None)
    sol = make_step(FakeProvider(), model="gpt-6.1-sol").build_input(v, None)
    assert luna is not None and sol is not None and luna.hash != sol.hash


def test_pii_mask_keeps_salaries_and_years() -> None:
    text = "Salary 120 000 - 150 000 RSD, budget 2024-2025, call (415) 555-2671 or +44 20 7946 0958."
    masked = mask_pii(text)
    assert "120 000 - 150 000" in masked and "2024-2025" in masked
    assert masked.count("[phone]") == 2


def test_shadow_sample_is_seeded_and_near_the_rate() -> None:
    sampled = [in_shadow_sample(i, rate=0.10, salt="s") for i in range(5000)]
    assert sampled == [in_shadow_sample(i, rate=0.10, salt="s") for i in range(5000)]
    assert 0.08 < sum(sampled) / len(sampled) < 0.12
    assert sampled != [in_shadow_sample(i, rate=0.10, salt="other") for i in range(5000)]


@pytest.mark.parametrize(
    ("quote", "ok"),
    [
        (None, True),
        ("Remote within the EU", True),
        ("Remote", True),  # from the location field
        ("Remote within the European Union", False),  # paraphrase
        ("   ", False),
    ],
)
def test_quote_check(quote: str | None, ok: bool) -> None:
    payload = {"title": "Backend Engineer", "location_raw": "Remote", "text": "Remote within the EU. Python."}
    problem = quote_check(payload)(VacancyFields.model_validate(fields(location_quote=quote)))
    assert (problem is None) == ok


def test_cost_estimate_reserves_two_attempts_and_the_shadow() -> None:
    no_shadow = make_step(FakeProvider(), shadow_rate=0.0).cost_estimate_usd
    with_shadow = make_step(FakeProvider(), shadow_rate=0.1).cost_estimate_usd
    assert 0 < no_shadow < with_shadow
    assert with_shadow < 0.5  # a sanity bound: one run can never reserve dollars


# -- injection fixtures (structure; the live model is scored by scripts/eval_extract.py) ---------------------


@pytest.mark.parametrize("fx", FIXTURES, ids=[f["id"] for f in FIXTURES])
def test_a_posting_cannot_forge_its_closing_marker(fx: dict[str, Any]) -> None:
    rendered = render_posting(title=fx["title"], location_raw=fx["location_raw"], text=mask_pii(fx["text"]))
    marker = rendered.splitlines()[0].removeprefix("BEGIN POSTING ")
    assert rendered.count(f"END POSTING {marker}") == 1 and rendered.endswith(f"END POSTING {marker}")


@pytest.mark.parametrize("fx", FIXTURES, ids=[f["id"] for f in FIXTURES])
def test_an_obeyed_injection_quote_is_rejected_by_the_check(fx: dict[str, Any]) -> None:
    payload = {"title": fx["title"], "location_raw": fx["location_raw"], "text": mask_pii(fx["text"])}
    injected = VacancyFields.model_validate(fields(location_quote="Remote from Serbia, worldwide"))
    assert quote_check(payload)(injected) is not None


# -- through the engine --------------------------------------------------------------------------------------


def pipeline(db: AsyncEngine, provider: FakeProvider, **step_kwargs: Any) -> Any:
    chain = [EligibilityRulesStep(), make_step(provider, db, **step_kwargs)]
    return make_steps(db, chain=chain, budget=LLMBudget(daily_cap_usd=10.0))


async def ledger(db: AsyncEngine) -> list[LLMCall]:
    async with make_sessionmaker(db)() as session:
        return list((await session.execute(select(LLMCall).order_by(LLMCall.id))).scalars().all())


def test_new_vacancy_flows_from_rules_to_extract_with_cost_and_ledger(db_url: str) -> None:
    async def scenario(db: AsyncEngine) -> None:
        provider = FakeProvider(default=lambda req: answer(fields(location_quote="Remote, Serbia")))
        steps = pipeline(db, provider)
        await fetch(db, T0, [posting("a", "Remote, Serbia")])
        await steps.reconcile()
        assert await steps.drain() == {"done": 1}  # rules; extract's row is born in the same commit
        assert await steps.drain() == {"done": 1}  # extract
        _rules, extract = await runs(db)
        assert (extract.step, extract.status, extract.model) == ("extract", "done", "gpt-6-luna")
        assert extract.result is not None and extract.result["fields"]["location_quote"] == "Remote, Serbia"
        assert extract.result["shadow"] is None and extract.cost_usd and extract.cost_usd > 0
        (call,) = await ledger(db)
        assert (call.step_run_id, call.vacancy_id, call.role, call.outcome) == (
            extract.id,
            1,
            "primary",
            "ok",
        )
        assert "BEGIN POSTING" in provider.requests[0].messages[0].content

    check(db_url, scenario)


def test_shadow_runs_beside_the_primary_and_never_replaces_it(db_url: str) -> None:
    async def scenario(db: AsyncEngine) -> None:
        def by_model(req: Request) -> Any:
            return answer(fields(title="primary" if req.model == "gpt-6-luna" else "shadow"))

        steps = pipeline(db, FakeProvider(default=by_model), shadow_rate=1.0)
        await fetch(db, T0, [posting("a", "Remote")])
        await steps.reconcile()
        await steps.drain()
        await steps.drain()
        extract = (await runs(db))[1]
        assert extract.result is not None
        assert extract.result["fields"]["title"] == "primary" and extract.model == "gpt-6-luna"
        assert extract.result["shadow"]["fields"]["title"] == "shadow"
        assert [(c.role, c.model) for c in await ledger(db)] == [
            ("primary", "gpt-6-luna"),
            ("shadow", "gpt-6.1-sol"),
        ]

    check(db_url, scenario)


def test_a_shadow_failure_does_not_fail_the_step(db_url: str) -> None:
    async def scenario(db: AsyncEngine) -> None:
        provider = FakeProvider([answer(fields()), LLMPermanentError("400: bad request")])
        steps = pipeline(db, provider, shadow_rate=1.0)
        await fetch(db, T0, [posting("a", "Remote")])
        await steps.reconcile()
        await steps.drain()
        assert await steps.drain() == {"done": 1}
        extract = (await runs(db))[1]
        assert extract.result is not None and "LLMPermanentError" in extract.result["shadow"]["error"]

    check(db_url, scenario)


def test_two_invalid_answers_fail_the_run_with_their_cost(db_url: str) -> None:
    async def scenario(db: AsyncEngine) -> None:
        provider = FakeProvider(default=quoting("Remote from anywhere on Earth"))
        steps = pipeline(db, provider)
        await fetch(db, T0, [posting("a", "Remote")])
        await steps.reconcile()
        await steps.drain()
        assert await steps.drain() == {"failed": 1}
        extract = (await runs(db))[1]
        assert (extract.status, extract.error_class) == ("failed", "permanent")
        assert "not an exact substring" in (extract.error_message or "")
        assert extract.cost_usd and extract.cost_usd > 0
        assert [c.outcome for c in await ledger(db)] == ["invalid", "invalid"]

    check(db_url, scenario)


def test_a_failed_shadow_keeps_the_primary_and_counts_its_own_cost(db_url: str) -> None:
    """Shadow errors of every kind — a provider error after a billed invalid answer, a transient error, a
    ledger failure — leave the primary result in place, and the shadow's spend is in the row's cost."""

    async def scenario(db: AsyncEngine) -> None:
        provider = FakeProvider([answer(fields()), answer("not json"), LLMTransientError("503: overloaded")])
        steps = pipeline(db, provider, shadow_rate=1.0)
        await fetch(db, T0, [posting("a", "Remote")])
        await steps.reconcile()
        await steps.drain()
        assert await steps.drain() == {"done": 1}
        extract = (await runs(db))[1]
        assert extract.result is not None and "LLMTransientError" in extract.result["shadow"]["error"]
        primary_cost = extract.result["cost_usd"]
        assert extract.cost_usd == pytest.approx(primary_cost + cost_usd("gpt-6.1-sol", USAGE))

    check(db_url, scenario)


def test_a_shadow_ledger_failure_does_not_fail_the_step(db_url: str) -> None:
    async def scenario(db: AsyncEngine) -> None:
        class ShadowLedgerDown(Ledger):
            async def start(self, tags: CallTags, **kwargs: Any) -> int:
                if tags.role == "shadow":
                    raise LedgerError("llm_calls insert failed: OperationalError")
                return await super().start(tags, **kwargs)

        llm = LLM(FakeProvider(default=lambda req: answer(fields())), ShadowLedgerDown(make_sessionmaker(db)))
        step = ExtractStep(
            llm,
            model="gpt-6-luna",
            shadow_model="gpt-6.1-sol",
            shadow_rate=1.0,
            shadow_salt="s",
            reasoning_effort="low",
            max_output_tokens=4000,
            max_text_chars=40000,
        )
        steps = make_steps(db, chain=[EligibilityRulesStep(), step], budget=LLMBudget(daily_cap_usd=10.0))
        await fetch(db, T0, [posting("a", "Remote")])
        await steps.reconcile()
        await steps.drain()
        assert await steps.drain() == {"done": 1}
        extract = (await runs(db))[1]
        assert extract.result is not None and "LedgerError" in extract.result["shadow"]["error"]

    check(db_url, scenario)


def test_blank_quote_is_rejected_even_when_blanks_occur_in_the_text() -> None:
    payload = {"title": "T", "location_raw": "", "text": "Remote   within the EU"}
    problem = quote_check(payload)(VacancyFields.model_validate(fields(location_quote="   ")))
    assert problem is not None and "blank" in problem


def test_the_marker_depends_on_the_content() -> None:
    a = render_posting(title="T", location_raw="L", text="one").splitlines()[0]
    b = render_posting(title="T", location_raw="L", text="two").splitlines()[0]
    assert a != b


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        ("Salary +20 000 - 30 000 EUR a year", "+20 000 - 30 000"),
        ("Budget 2024-2025, 120 000 - 150 000 RSD", "2024-2025, 120 000 - 150 000"),
        ("Call +1 415 555 1234\n5+ years of Python", "\n5+ years"),
    ],
)
def test_pii_mask_keeps_salaries_years_and_line_breaks(text: str, kept: str) -> None:
    assert kept in mask_pii(text)


@pytest.mark.parametrize(
    "text",
    ["+381 11 123 4567", "+44 20 7946 0958", "+7 (495) 123-45-67", "8 (495) 123-45-67", "(415) 555-2671"],
)
def test_pii_mask_catches_phone_formats(text: str) -> None:
    assert mask_pii(f"call {text} today") == "call [phone] today"


def test_pii_mask_is_linear_on_long_unbroken_tokens() -> None:
    blob = "a" * 200_000 + " " + "b" * 200_000
    started = time.monotonic()
    assert mask_pii(blob) == blob
    assert time.monotonic() - started < 1.0


def test_build_input_masks_only_the_kept_prefix() -> None:
    step = make_step(FakeProvider(), max_text_chars=50)
    inp = step.build_input(vacancy(text="x" * 10_000 + " jane@example.com"), None)
    assert inp is not None and inp.payload["text"] == "x" * 50
