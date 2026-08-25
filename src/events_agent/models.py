"""RawEvent: the common shape every source adapter yields, before normalisation.

Also: the strict-JSON output shapes the LLM scoring pass (Stage 3) must
validate against — see CLAUDE.md's Stage 3 spec for the exact schema.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


@dataclass
class RawEvent:
    source_name: str
    source_event_id: str

    title: str
    category: str | None  # theatre | music | cinema | comedy | other — a hint, not authoritative

    venue_name: str
    venue_city: str | None = None
    venue_postcode: str | None = None
    venue_latitude: float | None = None
    venue_longitude: float | None = None
    venue_type: str | None = None  # source-provided venue category, e.g. "bar", "theatre"

    event_date: datetime | None = None
    event_date_end: datetime | None = None

    status: str | None = None  # announced | on_sale | low_availability | sold_out | cancelled | past
    price_min: float | None = None
    price_max: float | None = None
    currency: str = "GBP"
    on_sale_date: datetime | None = None

    min_age: int | None = None
    doors_open: str | None = None  # source-provided time string, e.g. "19:30"

    url: str | None = None
    blurb: str | None = None

    raw: dict[str, Any] = field(default_factory=dict)


class ScoreResult(BaseModel):
    """One event's scoring-pass output — validated against the model's raw
    JSON before it's trusted. A parse/validation failure here is exactly
    what triggers the retry-once-then-flag-for-review path in scoring.py."""

    event_id: int
    score: int = Field(ge=0, le=100)
    audience: Literal["scott", "both", "partner"]
    reason: str
    urgency: Literal["none", "on_sale_soon", "selling_fast", "last_chance"]


class DuplicateAdjudication(BaseModel):
    """One duplicate_candidate pair's adjudication output."""

    pair_id: int
    same_event: bool
    reason: str
