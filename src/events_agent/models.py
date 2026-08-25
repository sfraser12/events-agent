"""RawEvent: the common shape every source adapter yields, before normalisation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


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

    min_age: int | None = None
    doors_open: str | None = None  # source-provided time string, e.g. "19:30"

    url: str | None = None
    blurb: str | None = None

    raw: dict[str, Any] = field(default_factory=dict)
