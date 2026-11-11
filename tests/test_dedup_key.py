from hypothesis import given
from hypothesis import strategies as st

from job_radar.postings import dedup_key

words = st.lists(
    st.sampled_from(["backend", "data", "platform", "ml", "python", "engineer", "developer"]),
    min_size=1,
    max_size=4,
)
companies = st.lists(st.sampled_from(["acme", "nova", "labs", "group", "tech"]), min_size=1, max_size=3)


def noisy(text: str, upper: bool, pad: str, punct: str) -> str:
    out = text.upper() if upper else text
    return pad + out.replace(" ", f" {punct} ") + pad


@given(
    companies, words, st.booleans(), st.sampled_from(["", " ", "  \t"]), st.sampled_from(["", "-", ",", "/"])
)
def test_stable_under_case_spacing_punctuation(
    c: list[str], w: list[str], upper: bool, pad: str, punct: str
) -> None:
    company, title = " ".join(c), " ".join(w)
    assert dedup_key(noisy(company, upper, pad, punct), noisy(title, upper, pad, punct)) == dedup_key(
        company, title
    )


@given(companies, words, st.sampled_from(["Inc", "Inc.", "Ltd", "GmbH", "d.o.o.", "LLC"]))
def test_company_legal_suffix_ignored(c: list[str], w: list[str], suffix: str) -> None:
    company, title = " ".join(c), " ".join(w)
    assert dedup_key(f"{company} {suffix}", title) == dedup_key(company, title)


@given(companies, words)
def test_seniority_synonyms_match_but_levels_differ(c: list[str], w: list[str]) -> None:
    company, title = " ".join(c), " ".join(w)
    assert dedup_key(company, f"Sr. {title}") == dedup_key(company, f"Senior {title}")
    assert dedup_key(company, f"Senior {title}") != dedup_key(company, title)


def test_accents_folded() -> None:
    assert dedup_key("Čačak Soft", "Développeur Python") == dedup_key("cacak soft", "developpeur python")


def test_non_latin_names_keep_their_letters() -> None:
    assert dedup_key("Яндекс", "Инженер") != dedup_key("Озон", "Инженер")
    assert dedup_key("Яндекс", "Инженер") != dedup_key("Яндекс", "Разработчик")
    assert dedup_key("ЯНДЕКС", "инженер") == dedup_key("Яндекс", "Инженер")


def test_language_symbols_are_distinct() -> None:
    keys = {dedup_key("x", t) for t in ["C++ Engineer", "C# Engineer", "C Engineer", ".NET Engineer"]}
    assert len(keys) == 4


def test_dotted_legal_suffix_any_spelling() -> None:
    for variant in ["Acme D.O.O.", "Acme d.o.o", "Acme DOO", "Acme d.o.o."]:
        assert dedup_key(variant, "Engineer") == dedup_key("Acme", "Engineer"), variant
    assert dedup_key("Acme B.V.", "x") == dedup_key("Acme BV", "x") == dedup_key("Acme", "x")
    assert dedup_key("Acme S.A.", "x") == dedup_key("Acme", "x")


def test_compound_spellings_match() -> None:
    assert dedup_key("x", "Front-end Engineer") == dedup_key("x", "Frontend Engineer")
    assert dedup_key("x", "Dev Ops Engineer") == dedup_key("x", "DevOps Engineer")
    assert dedup_key("x", "Full Stack Dev") == dedup_key("x", "Fullstack Developer")
