"""Rule-based work-eligibility baseline: can the owner (lives in Serbia, Russian passport, does not relocate)
take this job, judging by the location wording alone?

Answers yes / no / unclear. "unclear" is a real answer: it routes the vacancy to a human. The rules encode the
labeling rules in docs/decisions.md (2026-11-11, refined 2026-11-16).

Russia or Belarus excluded from a list of places ("worldwide except Russia") is ignored: the owner lives in
Serbia, so it does not apply to him (owner's ruling, 2026-11-16), and neither country is in the place list, so
no rule reads it. Excluding *people from* Russia ("не берут из РФ", "cannot hire from Russia") is different:
it may mean citizenship — rule 2.

First, phrases that only look like restrictions are removed, so later rules do not misread them:
- a negated bloc or country ("non-EU", "outside the EU", "not US"), a negated "worldwide" ("not global"), and
  "anywhere in <somewhere>" (that is a restriction to <somewhere>, not "anywhere");
- "no relocation (assistance)" / "relocation not required" (says nothing about where one may work).

Then, in this order — the first rule that fires wins:
1. Russian nationals / citizens excluded              -> no
2. People "from Russia" excluded (citizenship or residence? cannot tell) -> unclear
3. Serbia explicitly negated ("not in Serbia", "Serbia excluded") -> no
4. Novi Sad / Kraljevo office without Belgrade or remote -> no (owner's commute constraint)
5. Serbia / Belgrade named, or a Serbian job board phrase -> yes
6. Russian candidates explicitly welcome            -> yes
7. Worldwide / anywhere / global                    -> yes; "global" with other geography named -> unclear
   ("global" often describes the company, not the hiring area: "Global team, US only")
8. Time-zone band that covers Serbia (UTC+1/+2)     -> yes; a band that does not -> no
9. Named time zones that cover Serbia (CET, EU time zones) -> yes
   Rules 8-9 give unclear when a country or city is named too ("Remote (US), CET overlap": hours, not
   location).
10. EU / EEA / eurozone (legal blocs Serbia is not in) -> no
11. "No remote / no visa / no relocation" statements -> no
12. Relocation to another country, remote not offered -> no
13. Region only (Europe, EMEA)                       -> unclear
14. Other regions (Americas, APAC, LATAM)            -> no
15. Named countries or cities, none of them Serbian  -> no
16. Anything else (bare "Remote", nothing usable)    -> unclear

This is the baseline an LLM classifier has to beat; it is deliberately simple and readable.
"""

import re
from dataclasses import dataclass
from typing import Literal

Label = Literal["yes", "no", "unclear"]


@dataclass(frozen=True)
class Verdict:
    label: Label
    rule: str
    evidence: str = ""


def _rx(*alternatives: str) -> re.Pattern[str]:
    return re.compile("|".join(f"(?:{a})" for a in alternatives), re.IGNORECASE)


# --- phrases removed before the rules run ---

_RU = r"(?:\bрф\b|\bросси[яиюей]\b|\brussia\b|\bbelarus\b|\bбеларус\w*|\bрб\b)"
_RU_LIST = rf"{_RU}(?:\s*(?:/|,|\band\b|\bor\b|\bи\b|\bили\b)\s*{_RU})*"
# People "from" Russia/Belarus excluded: unclear when Russia is among them; a Belarus-only one is ignored.
PEOPLE_FROM_RU_EXCLUDED = _rx(
    rf"(?:не\s+берут|не\s+рассматрива\w*|не\s+нанима\w*|not\s+(?:accept|consider|hire)\w*|cannot\s+hire"
    rf"|can't\s+hire)[^.;]{{0,40}}?{_RU_LIST}",
    rf"(?:\bиз|\bfrom)\s+{_RU_LIST}[^.;]{{0,30}}?(?:\bnot\b|\bне\s|\bineligible\b)",
)
RUSSIA_TOKEN = _rx(r"\bрф\b", r"\bросси[яиюей]\b", r"\brussia\b")
NEGATED_GEO = _rx(
    r"(?:\bnon|\bnot|\boutside(?:\s+of)?(?:\s+the)?|\bexcluding|\bexcept|\bno)[\s-]+(?:the\s+)?"
    r"(?:eu|european\s+union|eea|us|usa|u\.s\.|uk)\b",
    r"\b(?:not|no|non)[\s-]+(?:available\s+)?(?:world\s*wide|global(?:ly)?|anywhere)\b",
    r"\banywhere\s+in\s+(?!the\s+world)",
)
NEGATED_RELOCATION = _rx(
    r"\bno\s+relocation\w*(?:\s+(?:assistance|support|package|needed|required))?",
    r"\brelocation\s+(?:is\s+)?not\s+(?:required|needed|necessary)",
    r"без\s+релокац\w*",
)

# --- rules ---

_RU_NATIONALS = (
    r"(?:russian\s+(?:nationals|citizens|passport\s+holders|passports?)|граждан\w*\s+(?:рф|росси\w*))"
)
RUSSIAN_NATIONALS_EXCLUDED = _rx(
    rf"{_RU_NATIONALS}[^.;]{{0,80}}?\b(?:not|no|cannot|unable|ineligible|не)\b",
    rf"\b(?:not|no|cannot|unable|except(?:ing)?|exclud\w*|не|кроме)\b[^.;]{{0,80}}?{_RU_NATIONALS}",
)
_SERBIA_WORD = r"(?:serbia|сербии|сербия|сербию)\b"
SERBIA_NEGATED = _rx(
    rf"(?:\bне|\bnot|\bno|\bбез|\bwithout|\bexcept(?:ing)?|\bexclud\w*|\bкроме|\bcannot\s+hire|\bcan't\s+hire"
    rf"|\bunable\s+to\s+hire)\W{{1,3}}(?:(?:in|from|the|в|из)\W{{1,3}})?{_SERBIA_WORD}",
    rf"{_SERBIA_WORD}\s+(?:is\s+|are\s+)?(?:not\s+(?:supported|eligible|included|available|possible)|excluded"
    rf"|нет\b|не\s+(?:подходит|рассматрива\w*))",
)
NOVI_SAD = _rx(r"novi[\s-]*sad", r"нови[\s-]*сад\w*", r"kraljevo", r"кралево")
BELGRADE_OR_REMOTE = _rx(r"belgrade", r"beograd", r"белград\w*", r"\bremote", r"удал[её]н\w*")
SERBIA = _rx(
    r"\bserbia\b",
    r"srbija",
    r"серби[яиюей]\b",
    r"\bсербск\w*\b(?!\s+язык)",
    r"belgrade",
    r"beograd",
    r"белград\w*",
)
SERBIAN_BOARD = _rx(r"rad od ku[cć]e")
RUSSIA_WELCOME = _rx(r"(?:кандидат\w*|candidates?|applicants?)\s+(?:из|from)\s+(?:росси\w*|russia)")
WORLDWIDE = _rx(
    r"world\s*wide",
    r"anywhere",
    r"\bglobal(?:ly)?\b",
    r"all\s+countries",
    r"any\s+country",
    r"любой\s+точки\s+мира",
    r"\bпо\s+миру\b",
)
_SIGN = r"[+\-−–‑]"  # plus, ASCII hyphen-minus, minus sign, en dash, non-breaking hyphen
TZ_RANGE = re.compile(
    rf"(?:utc|gmt)\s*({_SIGN}\s*\d{{1,2}}(?:[.:]\d{{1,2}})?)\s*(?:to|до|и|and|-|–|—)\s*(?:utc|gmt)?\s*"
    rf"({_SIGN}\s*\d{{1,2}}(?:[.:]\d{{1,2}})?)",
    re.IGNORECASE,
)
TZ_COVERS_SERBIA = _rx(
    r"\bcet\b",
    r"\bcest\b",
    r"\beet\b",
    r"europe(?:an)?\s+time\s*zones?",
    r"\beu\s+time\s*zones?",
    r"\bemea\s+time\s*zones?",
)
EU_BLOC = _rx(
    r"european\s+union",
    r"\beu\b(?!\s+time\s*zones?)",
    r"eurozone",
    r"еврозон\w*",
    r"\beea\b",
    r"schengen",
    r"\bевросоюз\w*",
)
NO_SUPPORT = _rx(
    r"(?:unable|not\s+able|cannot|can't)\s+to\s+(?:offer|support)[^.;]{0,60}(?:remote|visa|relocation)",
    r"(?:relocation|visa)[^.;]{0,30}(?:is|are)\s+not\s+(?:offered|supported|provided)",
    r"не\s+удал[её]нк\w*",
    r"remote\s+work\s+available:\s*no",
)
RELOCATION = _rx(r"relocat\w*", r"релокац\w*", r"переезд\w*")
REMOTE = _rx(r"\bremote(?:ly)?\b", r"удал[её]н\w*", r"work\s+from\s+home")
REGION_EUROPE = _rx(r"\beurope\b", r"европ\w*", r"\bemea\b", r"\bcee\b")
OTHER_REGIONS = _rx(
    r"north\s+america",
    r"\bamericas\b",
    r"\blatam\b",
    r"latin\s+america",
    r"\bapac\b",
    r"\bapj\b",
    r"\bafrica\b",
    r"middle\s+east",
    r"\basia\b",
)
# "US" is matched case-sensitively: with IGNORECASE it also matches the pronoun ("work with us").
US = re.compile(r"\bU\.S\.(?:A\.)?|\bUSA?\b")
# Countries and cities that occur in job location lines (EN + RU spellings). Serbia is not in this list.
PLACES = _rx(
    *(
        rf"\b{p}\b"
        for p in [
            r"united\s+states",
            r"united\s+kingdom",
            r"\buk\b",
            r"england",
            r"ireland",
            r"germany",
            r"netherlands",
            r"spain",
            r"portugal",
            r"france",
            r"italy",
            r"poland",
            r"czech\w*",
            r"slovakia",
            r"slovenia",
            r"croatia",
            r"hungary",
            r"romania",
            r"bulgaria",
            r"greece",
            r"cyprus",
            r"malta",
            r"estonia",
            r"latvia",
            r"lithuania",
            r"finland",
            r"sweden",
            r"norway",
            r"denmark",
            r"switzerland",
            r"austria",
            r"belgium",
            r"luxembourg",
            r"montenegro",
            r"bosnia",
            r"albania",
            r"north\s+macedonia",
            r"moldova",
            r"ukraine",
            r"georgia",
            r"armenia",
            r"turkey",
            r"israel",
            r"kazakhstan",
            r"uzbekistan",
            r"canada",
            r"mexico",
            r"brazil",
            r"argentina",
            r"india",
            r"australia",
            r"singapore",
            r"uae",
            r"london",
            r"berlin",
            r"munich",
            r"amsterdam",
            r"madrid",
            r"barcelona",
            r"lisbon",
            r"paris",
            r"warsaw",
            r"krak[oó]w",
            r"prague",
            r"bratislava",
            r"sofia",
            r"bucharest",
            r"tallinn",
            r"riga",
            r"vilnius",
            r"dublin",
            r"zurich",
            r"vienna",
            r"limassol",
            r"tel\s+aviv",
            r"yerevan",
            r"tbilisi",
            r"dubai",
            r"new\s+york",
            r"boston",
            r"san\s+francisco",
            r"chi[sș]in[aă]u",
            r"ptuj",
            r"d[uü]sseldorf",
            r"freiburg",
            r"the\s+hague",
            r"сша",
            r"германи\w*",
            r"нидерланд\w*",
            r"испани\w*",
            r"португали\w*",
            r"франци\w*",
            r"итали\w*",
            r"польш\w*",
            r"чехи\w*",
            r"словаки\w*",
            r"словени\w*",
            r"хорвати\w*",
            r"болгари\w*",
            r"кипр\w*",
            r"мальт\w*",
            r"эстони\w*",
            r"израил\w*",
            r"казахстан\w*",
            r"армени\w*",
            r"грузи\w*",
            r"украин\w*",
            r"черногори\w*",
            r"лондон\w*",
            r"берлин\w*",
            r"амстердам\w*",
            r"мадрид\w*",
            r"барселон\w*",
            r"варшав\w*",
            r"краков\w*",
            r"праг\w*",
            r"братислав\w*",
            r"софи[яи]",
            r"лимасол\w*",
            r"тель-авив\w*",
            r"ереван\w*",
            r"тбилиси",
            r"дюссельдорф\w*",
            r"птуй",
        ]
    )
)


def _tz_offset(raw: str) -> float:
    """'+5:30' -> 5.5, any minus-like sign -> negative. The regex guarantees one sign and digits only."""
    sign = -1 if raw[0] != "+" else 1
    hours, _, minutes = raw[1:].strip().replace(".", ":").partition(":")
    frac = 0.0
    if minutes:  # "5:30" is half past; "3.5" read as "3:5" means 3.5 hours
        frac = int(minutes) / 60 if len(minutes) == 2 else int(minutes) / 10
    return sign * (int(hours) + frac)


def _first(pattern: re.Pattern[str], text: str) -> str | None:
    m = pattern.search(text)
    return m.group(0) if m else None


def _country_or_city(text: str) -> str | None:
    return _first(US, text) or _first(PLACES, text)


def _place(text: str) -> str | None:
    """Any named geography other than Serbia: a country, city, US, a region or a legal bloc."""
    return _country_or_city(text) or _first(OTHER_REGIONS, text) or _first(EU_BLOC, text)


def _strip(pattern: re.Pattern[str], text: str) -> str:
    return " ".join(pattern.sub(" ", text).split())


def classify(text: str) -> Verdict:
    original = " ".join(text.split())
    if not original:
        return Verdict("unclear", "empty")
    if ev := _first(RUSSIAN_NATIONALS_EXCLUDED, original):
        return Verdict("no", "russian_nationals_excluded", ev)
    for m in PEOPLE_FROM_RU_EXCLUDED.finditer(original):
        if RUSSIA_TOKEN.search(m.group(0)):
            return Verdict("unclear", "russia_people_excluded", m.group(0))
    t = original
    for pattern in (NEGATED_GEO, NEGATED_RELOCATION):
        t = _strip(pattern, t)

    if ev := _first(SERBIA_NEGATED, t):
        return Verdict("no", "serbia_negated", ev)
    if (ev := _first(NOVI_SAD, t)) and not BELGRADE_OR_REMOTE.search(t):
        return Verdict("no", "novi_sad_office", ev)
    if ev := _first(SERBIA, t) or _first(SERBIAN_BOARD, t):
        return Verdict("yes", "serbia_named", ev)
    if ev := _first(RUSSIA_WELCOME, t):
        return Verdict("yes", "russia_welcome", ev)
    if ev := _first(WORLDWIDE, t):
        if ev.lower().startswith("global") and (place := _place(t)):
            return Verdict("unclear", "global_with_places", f"{ev} / {place}")
        return Verdict("yes", "worldwide", ev)
    if bands := list(TZ_RANGE.finditer(t)):
        for m in bands:
            lo, hi = sorted((_tz_offset(m.group(1)), _tz_offset(m.group(2))))
            if lo <= 1 <= hi or lo <= 2 <= hi:
                if place := _country_or_city(t):
                    return Verdict("unclear", "tz_band_with_places", f"{m.group(0)} / {place}")
                return Verdict("yes", "tz_band", m.group(0))
        return Verdict("no", "tz_band", bands[0].group(0))
    if ev := _first(TZ_COVERS_SERBIA, t):
        if place := _country_or_city(t):
            return Verdict("unclear", "tz_named_with_places", f"{ev} / {place}")
        return Verdict("yes", "tz_named", ev)
    if ev := _first(EU_BLOC, t):
        return Verdict("no", "eu_bloc", ev)
    if ev := _first(NO_SUPPORT, t):
        return Verdict("no", "no_remote_visa_relocation", ev)
    if (ev := _first(RELOCATION, t)) and not REMOTE.search(t):
        # relocation offered *instead of* remote; "remote or relocate" is not a no
        return Verdict("no", "relocation", ev)
    if ev := _first(REGION_EUROPE, t):
        return Verdict("unclear", "region_only", ev)
    if ev := _first(OTHER_REGIONS, t):
        return Verdict("no", "other_region", ev)
    if ev := _country_or_city(t):
        return Verdict("no", "places_without_serbia", ev)
    return Verdict("unclear", "fallback")
