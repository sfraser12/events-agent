"""Title/venue normalisation and event fingerprinting.

Fingerprint = sha256(normalise(title) | normalise(venue_name) | event_date.date().isoformat()).
Exact match on this key means "same event" and rows are merged on upsert. Near-misses
(fuzzy title match, same venue/date) are Phase 2's job (rapidfuzz + duplicate_candidate) —
not handled here.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime

# Multi-word phrases first — must be stripped before punctuation collapses them
# into tokens that no longer match.
PHRASE_STOPWORDS = (
    "an evening with",
    "plus special guests",
    "+ support",
)

# Single-word noise tokens, removed after tokenising.
WORD_STOPWORDS = {"the", "live", "tour", "presents", "feat"}

_PUNCTUATION_RE = re.compile(r"[^\w\s]")
_WHITESPACE_RE = re.compile(r"\s+")


def normalise_text(text: str) -> str:
    text = text.lower()
    for phrase in PHRASE_STOPWORDS:
        text = text.replace(phrase, " ")
    text = _PUNCTUATION_RE.sub(" ", text)
    tokens = [t for t in text.split() if t not in WORD_STOPWORDS]
    return _WHITESPACE_RE.sub(" ", " ".join(tokens)).strip()


def fingerprint(title: str, venue_name: str, event_date: datetime | None) -> str:
    date_key = event_date.date().isoformat() if event_date else "undated"
    key = f"{normalise_text(title)}|{normalise_text(venue_name)}|{date_key}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()
