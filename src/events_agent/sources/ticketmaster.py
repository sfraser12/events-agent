"""Ticketmaster Discovery API adapter.

Docs: https://developer.ticketmaster.com/products-and-docs/apis/discovery/v2/

Deep paging is capped at 1,000 items (`size * page < 1000`, per CLAUDE.md) —
a single unwindowed query for our full search horizon returns totalElements
well over that (1,402 events at 25mi/270 days, confirmed live), so events past
the cap would silently vanish. We slice the horizon into WINDOW_DAYS chunks
via startDateTime/endDateTime and page each chunk independently — a chunk
this small is nowhere near the 1,000-item cap in practice.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests

from events_agent.models import RawEvent
from events_agent.sources.base import RateLimiter, parse_iso_datetime

SEARCH_URL = "https://app.ticketmaster.com/discovery/v2/events.json"

PAGE_SIZE = 200  # Ticketmaster's documented max page size
WINDOW_DAYS = 30  # date-window slice size, well clear of the 1,000-item cap
DATE_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# Ticketmaster's own duplicate-detection nemesis lives in classifications, not
# a clean enum — Comedy is a *genre* under the "Arts & Theatre" segment.
COMEDY_GENRE = "Comedy"
SEGMENT_TO_CATEGORY = {
    "Music": "music",
    "Film": "cinema",
    "Arts & Theatre": "theatre",
}

# postponed/rescheduled get folded into cancelled — same precedent as
# Skiddle's rescheduledDate handling: the originally-listed instance is off,
# and we don't have a schema state for "moved" separate from "cancelled".
STATUS_MAP = {
    "onsale": "on_sale",
    "cancelled": "cancelled",
    "postponed": "cancelled",
    "rescheduled": "cancelled",
    "offsale": "announced",
}

MIN_REQUEST_INTERVAL_SECONDS = 0.25  # ~4 req/sec, under TM's stated 5/sec cap


class TicketmasterAdapter:
    name = "ticketmaster"

    def __init__(
        self,
        api_key: str,
        latitude: float,
        longitude: float,
        radius_miles: float,
        window_days: int = 270,
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
        window_start = since or datetime.now(UTC)
        horizon_end = window_start + timedelta(days=self.window_days)
        while window_start < horizon_end:
            window_end = min(window_start + timedelta(days=WINDOW_DAYS), horizon_end)
            yield from self._fetch_window(window_start, window_end)
            window_start = window_end

    def _fetch_window(self, window_start: datetime, window_end: datetime) -> Iterator[RawEvent]:
        page = 0
        while True:
            data = self._get(window_start, window_end, page)
            events = data.get("_embedded", {}).get("events", [])
            for raw in events:
                yield self._parse_event(raw)

            total_pages = data.get("page", {}).get("totalPages", 0)
            page += 1
            if page >= total_pages or page * PAGE_SIZE >= 1000:
                break

    def _get(self, window_start: datetime, window_end: datetime, page: int) -> dict[str, Any]:
        params = {
            "apikey": self.api_key,
            "latlong": f"{self.latitude},{self.longitude}",
            "radius": int(round(self.radius_miles)),
            "unit": "miles",
            "countryCode": "GB",
            "size": PAGE_SIZE,
            "page": page,
            "sort": "date,asc",
            "startDateTime": window_start.strftime(DATE_FORMAT),
            "endDateTime": window_end.strftime(DATE_FORMAT),
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
        cacheable = {k: v for k, v in params.items() if k != "apikey"}
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

    def _parse_event(self, raw: dict[str, Any]) -> RawEvent:
        venue = (raw.get("_embedded", {}).get("venues") or [{}])[0]
        location = venue.get("location") or {}
        city = venue.get("city") or {}

        classification = (raw.get("classifications") or [{}])[0]
        segment = (classification.get("segment") or {}).get("name")
        genre = (classification.get("genre") or {}).get("name")
        category = _map_category(segment, genre)

        price = (raw.get("priceRanges") or [{}])[0]

        start = raw.get("dates", {}).get("start", {})
        event_date = parse_iso_datetime(start.get("dateTime"))
        sales_public = raw.get("sales", {}).get("public") or {}
        on_sale_date = parse_iso_datetime(sales_public.get("startDateTime"))

        status_code = raw.get("dates", {}).get("status", {}).get("code")
        status = STATUS_MAP.get(status_code, "announced")

        return RawEvent(
            source_name=self.name,
            source_event_id=raw["id"],
            title=raw.get("name", ""),
            category=category,
            venue_name=venue.get("name", ""),
            venue_city=city.get("name"),
            venue_postcode=venue.get("postalCode"),
            venue_latitude=_to_float(location.get("latitude")),
            venue_longitude=_to_float(location.get("longitude")),
            # Ticketmaster doesn't expose Skiddle's bar/theatre/nightclub-style
            # venue category — nothing to map here.
            venue_type=None,
            event_date=event_date,
            event_date_end=None,
            status=status,
            price_min=price.get("min"),
            price_max=price.get("max"),
            currency=price.get("currency") or "GBP",
            on_sale_date=on_sale_date,
            min_age=None,
            doors_open=None,
            url=raw.get("url") or None,
            blurb=raw.get("pleaseNote") or raw.get("info") or None,
            # Stored for debugging, never read back programmatically (see
            # db.py's event_source.raw_json) -- but Ticketmaster's own
            # _embedded (full nested venue detail, already extracted above)
            # and images (a dozen-plus CDN URLs at every crop size, never
            # displayed anywhere) together were ~70% of every stored blob's
            # bytes and ~89% of the whole database's disk size, confirmed
            # live 2026-09-01 (113MB DB, 100MB of it raw_json). Drop both
            # before storing; everything actually used stays.
            raw={k: v for k, v in raw.items() if k not in ("_embedded", "images")},
        )


def _map_category(segment: str | None, genre: str | None) -> str:
    if segment == "Arts & Theatre" and genre == COMEDY_GENRE:
        return "comedy"
    return SEGMENT_TO_CATEGORY.get(segment, "other")


def _to_float(value: str | float | None) -> float | None:
    if value is None:
        return None
    return float(value)
