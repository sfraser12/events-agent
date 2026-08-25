"""Skiddle Events API adapter.

Docs: https://github.com/Skiddle/web-api/wiki/Events-API

Skiddle event codes are promoter-chosen and unreliable (per CLAUDE.md), but they're
the only category signal this source gives us, and the API only accepts one
`eventcode` per request — so we make one paged request per code in EVENTCODES
and treat the mapped category as a hint for later scoring, not a fact.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests

from events_agent.models import RawEvent
from events_agent.sources.base import RateLimiter

SEARCH_URL = "https://www.skiddle.com/api/v1/events/search/"

EVENTCODES = ("LIVE", "FEST", "THEATRE", "COMEDY", "CLUB")

CATEGORY_BY_EVENTCODE = {
    "LIVE": "music",
    "FEST": "music",
    "CLUB": "music",
    "THEATRE": "theatre",
    "COMEDY": "comedy",
}

PAGE_LIMIT = 100

# Undocumented rate limit — Skiddle's public docs don't state one, so this is a
# conservative guess to stay polite rather than a value taken from their docs.
MIN_REQUEST_INTERVAL_SECONDS = 0.3


class SkiddleAdapter:
    name = "skiddle"

    def __init__(
        self,
        api_key: str,
        latitude: float,
        longitude: float,
        radius_miles: float,
        window_days: int = 90,
        session: requests.Session | None = None,
        cache_dir: Path | None = None,
    ):
        self.api_key = api_key
        self.latitude = latitude
        self.longitude = longitude
        self.radius_miles = radius_miles
        self.window_days = window_days
        self.session = session or requests.Session()
        self.cache_dir = cache_dir
        self._limiter = RateLimiter(MIN_REQUEST_INTERVAL_SECONDS)

    def fetch(self, since: datetime | None = None) -> Iterator[RawEvent]:
        min_date = (since.date() if since else datetime.now().date())
        max_date = min_date + timedelta(days=self.window_days)
        for eventcode in EVENTCODES:
            yield from self._fetch_eventcode(eventcode, min_date, max_date)

    def _fetch_eventcode(self, eventcode: str, min_date: date, max_date: date) -> Iterator[RawEvent]:
        offset = 0
        while True:
            data = self._get(eventcode, min_date, max_date, offset)
            results = data.get("results", [])
            for raw in results:
                yield self._parse_event(raw, eventcode)

            offset += len(results)
            totalcount = int(data.get("totalcount", 0) or 0)
            if not results or offset >= totalcount:
                break

    def _get(self, eventcode: str, min_date: date, max_date: date, offset: int) -> dict[str, Any]:
        params = {
            "api_key": self.api_key,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "radius": int(round(self.radius_miles)),
            "country": "GB",
            "eventcode": eventcode,
            "minDate": min_date.isoformat(),
            "maxDate": max_date.isoformat(),
            "description": 1,
            "order": "date",
            "limit": PAGE_LIMIT,
            "offset": offset,
        }

        cached = self._read_cache(params)
        if cached is not None:
            return cached

        self._limiter.wait()
        response = self.session.get(SEARCH_URL, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()

        self._write_cache(params, data)
        return data

    def _cache_key(self, params: dict[str, Any]) -> str:
        cacheable = {k: v for k, v in params.items() if k != "api_key"}
        digest = hashlib.sha256(urlencode(sorted(cacheable.items())).encode()).hexdigest()
        return digest

    def _read_cache(self, params: dict[str, Any]) -> dict[str, Any] | None:
        if self.cache_dir is None:
            return None
        cache_file = self.cache_dir / f"{self._cache_key(params)}.json"
        if not cache_file.exists():
            return None
        with cache_file.open() as f:
            return json.load(f)

    def _write_cache(self, params: dict[str, Any], data: dict[str, Any]) -> None:
        if self.cache_dir is None:
            return
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = self.cache_dir / f"{self._cache_key(params)}.json"
        with cache_file.open("w") as f:
            json.dump(data, f)

    def _parse_event(self, raw: dict[str, Any], eventcode: str) -> RawEvent:
        venue = raw.get("venue") or {}
        pricing = raw.get("ticketpricing") or {}
        opening_times = raw.get("openingtimes") or {}

        event_date = _parse_datetime(raw.get("startdate"))
        event_date_end = _parse_datetime(raw.get("enddate"))
        category = CATEGORY_BY_EVENTCODE.get(raw.get("EventCode", eventcode), "other")
        status = _parse_status(raw)
        price_min, price_max = _parse_price_range(pricing)

        return RawEvent(
            source_name=self.name,
            source_event_id=str(raw["id"]),
            title=raw.get("eventname", ""),
            category=category,
            venue_name=venue.get("name", ""),
            venue_city=venue.get("town") or None,
            venue_postcode=venue.get("postcode") or None,
            venue_latitude=venue.get("latitude"),
            venue_longitude=venue.get("longitude"),
            venue_type=venue.get("type") or None,
            event_date=event_date,
            event_date_end=event_date_end,
            status=status,
            price_min=price_min,
            price_max=price_max,
            currency=raw.get("currency") or "GBP",
            min_age=_parse_min_age(raw.get("minage")),
            doors_open=opening_times.get("doorsopen") or None,
            url=raw.get("link") or None,
            blurb=raw.get("description") or None,
            raw=raw,
        )


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _parse_status(raw: dict[str, Any]) -> str:
    # cancellationDate/rescheduledDate can be populated even when the "cancelled"
    # flag itself is "0" — treat any of the three as a definitive cancellation.
    if raw.get("cancelled") == "1" or raw.get("cancellationDate") or raw.get("rescheduledDate"):
        return "cancelled"
    if raw.get("hotSeller"):
        return "low_availability"
    return "on_sale"


def _parse_price_range(pricing: dict[str, Any]) -> tuple[float | None, float | None]:
    min_price = pricing.get("minPrice")
    max_price = pricing.get("maxPrice")
    # Skiddle uses 0/0 to mean "no pricing data yet", not "free" — leave both NULL.
    if not min_price and not max_price:
        return None, None
    return min_price, max_price


def _parse_min_age(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None
