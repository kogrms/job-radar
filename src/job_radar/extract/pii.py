"""Stopgap PII masking before a vacancy text leaves the machine (week 4 replaces it with proper masking).

Only recruiters' contact details occur in postings: emails and phone numbers. Patterns are bounded (linear
time on any input) and narrow on purpose: salary ranges and years ("+20 000 - 30 000", "2024-2025") must
survive, because extraction needs them. Known misses: numbers without a country code, a NANP-like grouping or
Russian "(495)" brackets, and obfuscated emails ("name [at] corp.com").
"""

import re

_EMAIL = re.compile(r"[\w.+-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63}){1,8}")
# "+" country code, then digit groups joined by at most one space/dot/hyphen (never a newline or " - ").
_INTERNATIONAL = re.compile(r"(?<![\w+])\+\d{1,3}(?:[ .-]?\(?\d{1,5}\)?){1,6}(?!\d)")
_NANP = re.compile(r"(?<![\d+])\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?!\d)")
_RU_DOMESTIC = re.compile(r"(?<![\d+])[78][ -]?\(\d{3}\)[ -]?\d{3}-?\d{2}-?\d{2}(?!\d)")
_MIN_PHONE_DIGITS = 8


def _phone(match: re.Match[str]) -> str:
    digits = sum(c.isdigit() for c in match.group())
    return "[phone]" if digits >= _MIN_PHONE_DIGITS else match.group()


def mask_pii(text: str) -> str:
    text = _EMAIL.sub("[email]", text)
    text = _INTERNATIONAL.sub(_phone, text)
    text = _RU_DOMESTIC.sub("[phone]", text)
    return _NANP.sub("[phone]", text)
