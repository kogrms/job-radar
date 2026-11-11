"""What a collector hands to storage, plus the identity helpers: content hash and cross-source dedup key."""

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Posting:
    source_job_id: str
    url: str
    title: str
    company: str
    location_raw: str
    raw_text: str
    # Full API response for this posting. Not part of the content hash: it carries volatile fields
    # (updated_at, view counters) that would turn every fetch into a "changed" event.
    raw_payload: dict[str, Any] = field(default_factory=dict, compare=False)


def content_hash(p: Posting) -> str:
    canonical = json.dumps(
        [p.url, p.title, p.company, p.location_raw, p.raw_text], ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


_SYMBOL_TOKENS = [(r"c\+\+", " cpp "), (r"c#", " csharp "), (r"f#", " fsharp "), (r"\.net\b", " dotnet ")]
_COMPOUNDS = {
    ("front", "end"): "frontend",
    ("back", "end"): "backend",
    ("full", "stack"): "fullstack",
    ("dev", "ops"): "devops",
    ("ml", "ops"): "mlops",
    ("data", "ops"): "dataops",
}
_SYNONYMS = {
    "sr": "senior",
    "snr": "senior",
    "jr": "junior",
    "jnr": "junior",
    "mid": "middle",
    "eng": "engineer",
    "dev": "developer",
    "swe": "software engineer",
}
_COMPANY_SUFFIXES = {"inc", "ltd", "llc", "gmbh", "doo", "plc", "bv", "ag", "sa", "corp", "co", "limited"}


def _words(text: str) -> list[str]:
    """Casefolded word tokens in any script (Cyrillic stays Cyrillic), diacritics removed."""
    s = text.casefold()
    for pattern, token in _SYMBOL_TOKENS:
        s = re.sub(pattern, token, s)
    # Dotted abbreviations: "d.o.o." -> "doo", "s.a." -> "sa", "b.v" -> "bv".
    s = re.sub(r"\b(?:\w\.)+\w\b\.?", lambda m: m.group(0).replace(".", ""), s)
    s = "".join(ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch))
    tokens = re.findall(r"[^\W_]+", s)
    out: list[str] = []
    for t in tokens:
        if out and (out[-1], t) in _COMPOUNDS:
            out[-1] = _COMPOUNDS[(out[-1], t)]
        else:
            out.append(t)
    return out


def normalize_title(title: str) -> str:
    words: list[str] = []
    for w in _words(title):
        words.extend(_SYNONYMS.get(w, w).split())
    return " ".join(words)


def normalize_company(company: str) -> str:
    words = _words(company)
    while words and words[-1] in _COMPANY_SUFFIXES:
        words.pop()
    return " ".join(words)


def dedup_key(company: str, title: str) -> str:
    """Same job seen on two sources gets the same key.

    Seniority is kept: "Senior X" and "X" are different jobs; only spellings ("Sr.", "Senior") are unified.
    """
    return f"{normalize_company(company)}|{normalize_title(title)}"
