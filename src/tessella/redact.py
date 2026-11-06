"""Masks secrets and identifiers as data is read, before anything is stored or sent."""

from __future__ import annotations

import re

MASK = "[REDACTED]"

_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?i)bearer\s+[a-z0-9._\-]+"),
    re.compile(r"eyJ[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+"),  # JWT
    re.compile(r"\b(?:\d[ -]?){13,19}\b"),  # card-like numbers
    re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"),  # IBAN-like
]


def redact_text(text: str, keys: list[str] | None = None) -> str:
    """Mask known secret patterns and `key: value` / `"key": "value"` pairs for given keys."""
    if not text:
        return text
    out = text
    for p in _PATTERNS:
        out = p.sub(MASK, out)
    for key in keys or []:
        k = re.escape(key)
        out = re.sub(rf'(?i)("{k}"\s*:\s*)"[^"]*"', rf'\1"{MASK}"', out)
        out = re.sub(rf"(?i)(\b{k}\b\s*[:=]\s*)[^\s,;&]+", rf"\1{MASK}", out)
    return out


def redact_headers(headers: dict[str, str], keys: list[str]) -> dict[str, str]:
    lowered = {k.lower() for k in keys}
    return {h: (MASK if h.lower() in lowered else v) for h, v in headers.items()}
