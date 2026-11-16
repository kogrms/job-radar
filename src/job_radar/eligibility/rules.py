"""Rule-based work-eligibility baseline: can the owner (lives in Serbia, Russian passport, does not relocate)
take this job, judging by the location wording alone?

Answers yes / no / unclear. "unclear" is a real answer: it routes the vacancy to a human. The rules encode the
labeling rules in docs/decisions.md (2026-11-11), in this order — the first rule that fires wins:

1. Russia explicitly excluded                       -> no
2. Serbia explicitly negated ("not Serbia")         -> no
3. Novi Sad / Kraljevo office without Belgrade or remote -> no (owner's commute constraint)
4. Serbia / Belgrade named, or a Serbian job board phrase -> yes
5. Russian candidates explicitly welcome            -> yes
6. Worldwide / anywhere / global                    -> yes
7. Time-zone band that covers Serbia (UTC+1/+2)     -> yes; a band that does not -> no
8. EU / EEA / eurozone (legal blocs Serbia is not in) -> no
9. "No remote / no visa / no relocation" statements  -> no
10. Relocation to another country, remote not offered -> no
11. Region only (Europe, EMEA)                       -> unclear
12. Other regions (Americas, APAC, LATAM)            -> no
13. Named countries or cities, none of them Serbian  -> no
14. Anything else (bare "Remote", nothing usable)    -> unclear

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


RUSSIA_EXCLUDED = _rx(
    r"(?:не\s+берут|не\s+рассматрива\w*|кроме|except(?:ing)?|exclud\w*|not\s+(?:accept|consider|hire)\w*)"
    r"[^.;]{0,40}(?:\bрф\b|росси\w*|russia\w*|belarus\w*|\bрб\b)",
    r"(?:russian|belarusian)\s+nationals[^.;]{0,80}(?:not|no)\b",
    r"\bnot\b[^.;]{0,60}(?:russian|belarusian)\s+nationals",
)
SERBIA_NEGATED = _rx(
    r"(?:\bне|\bnot|\bno|\bбез|\bwithout)\W{0,3}(?:serbia|сербии|сербия)",
    r"(?:сербии|serbia)\s+(?:нет|not\b)",
)
NOVI_SAD = _rx(r"novi\s*sad", r"нови[\s-]*сад\w*", r"kraljevo", r"кралево")
BELGRADE_OR_REMOTE = _rx(r"belgrade", r"beograd", r"белград\w*", r"remote", r"удал[её]н\w*")
SERBIA = _rx(
    r"\bserbia\b", r"srbija", r"серби[яиюей]\b", r"сербск\w*", r"belgrade", r"beograd", r"белград\w*"
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
    r"from\s+anywhere",
)
TZ_RANGE = re.compile(
    r"(?:utc|gmt)\s*([+-−]\s*\d{1,2})(?::\d\d)?\s*(?:to|до|и|and|-|–|—)\s*(?:utc|gmt)?\s*([+-−]\s*\d{1,2})",
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
REMOTE = _rx(r"\bremote\b", r"удал[её]н\w*", r"work\s+from\s+home")
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
# Countries and cities that occur in job location lines (EN + RU spellings). Serbia is not in this list.
PLACES = _rx(
    *(
        rf"\b{p}\b"
        for p in [
            r"united\s+states",
            r"\busa?\b",
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


def _tz_offset(raw: str) -> int:
    return int(raw.replace("−", "-").replace(" ", ""))


def _first(pattern: re.Pattern[str], text: str) -> str | None:
    m = pattern.search(text)
    return m.group(0) if m else None


def classify(text: str) -> Verdict:
    t = " ".join(text.split())
    if not t:
        return Verdict("unclear", "empty")
    if ev := _first(RUSSIA_EXCLUDED, t):
        return Verdict("no", "russia_excluded", ev)
    if ev := _first(SERBIA_NEGATED, t):
        return Verdict("no", "serbia_negated", ev)
    if (ev := _first(NOVI_SAD, t)) and not BELGRADE_OR_REMOTE.search(t):
        return Verdict("no", "novi_sad_office", ev)
    if ev := _first(SERBIA, t) or _first(SERBIAN_BOARD, t):
        return Verdict("yes", "serbia_named", ev)
    if ev := _first(RUSSIA_WELCOME, t):
        return Verdict("yes", "russia_welcome", ev)
    if ev := _first(WORLDWIDE, t):
        return Verdict("yes", "worldwide", ev)
    if m := TZ_RANGE.search(t):
        lo, hi = sorted((_tz_offset(m.group(1)), _tz_offset(m.group(2))))
        covers = lo <= 1 <= hi or lo <= 2 <= hi
        return Verdict("yes" if covers else "no", "tz_band", m.group(0))
    if ev := _first(TZ_COVERS_SERBIA, t):
        return Verdict("yes", "tz_named", ev)
    if ev := _first(EU_BLOC, t):
        return Verdict("no", "eu_bloc", ev)
    if ev := _first(NO_SUPPORT, t):
        return Verdict("no", "no_remote_visa_relocation", ev)
    if (ev := _first(RELOCATION, t)) and not REMOTE.search(t):
        return Verdict(
            "no", "relocation", ev
        )  # relocation offered *instead of* remote; "remote or relocate" is fine
    if ev := _first(REGION_EUROPE, t):
        return Verdict("unclear", "region_only", ev)
    if ev := _first(OTHER_REGIONS, t):
        return Verdict("no", "other_region", ev)
    if ev := _first(PLACES, t):
        return Verdict("no", "places_without_serbia", ev)
    return Verdict("unclear", "fallback")
