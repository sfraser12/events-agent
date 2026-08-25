"""TicketmasterAdapter tests. All HTTP calls are faked from saved fixtures — no network."""

import json
from datetime import UTC, datetime
from pathlib import Path

from events_agent.sources import ticketmaster as ticketmaster_module
from events_agent.sources.ticketmaster import TicketmasterAdapter

FIXTURES = Path(__file__).parent / "fixtures"


class FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class FakeSession:
    """Routes (startDateTime, page) -> a saved fixture file; errors on anything unmapped."""

    def __init__(self, routes: dict[tuple[str, int], str]):
        self.routes = routes
        self.calls: list[dict] = []

    def get(self, url, params, timeout):
        self.calls.append(dict(params))
        key = (params["startDateTime"], params["page"])
        if key not in self.routes:
            raise AssertionError(f"unexpected request: {key}")
        with (FIXTURES / self.routes[key]).open() as f:
            return FakeResponse(json.load(f))


def make_adapter(session, window_days=30, cache_dir=None):
    return TicketmasterAdapter(
        api_key="test-key",
        latitude=55.9410,
        longitude=-4.3170,
        radius_miles=25,
        window_days=window_days,
        session=session,
        cache_dir=cache_dir,
    )


def test_fetch_paginates_within_a_single_window():
    since = datetime(2026, 1, 1, tzinfo=UTC)
    start_str = since.strftime(ticketmaster_module.DATE_FORMAT)
    session = FakeSession(
        {
            (start_str, 0): "ticketmaster_search_window0_page0.json",
            (start_str, 1): "ticketmaster_search_window0_page1.json",
        }
    )
    adapter = make_adapter(session, window_days=30)

    events = list(adapter.fetch(since=since))

    assert len(session.calls) == 2
    assert len(events) == 212  # 200 on page 0 + 12 on page 1
    assert all(e.source_name == "ticketmaster" for e in events)


def test_fetch_slices_horizon_into_windows(monkeypatch):
    monkeypatch.setattr(ticketmaster_module, "WINDOW_DAYS", 10)
    since = datetime(2026, 1, 1, tzinfo=UTC)
    window1_start = since.strftime(ticketmaster_module.DATE_FORMAT)
    from datetime import timedelta

    window2_start = (since + timedelta(days=10)).strftime(ticketmaster_module.DATE_FORMAT)
    session = FakeSession(
        {
            (window1_start, 0): "ticketmaster_search_variety.json",
            (window2_start, 0): "ticketmaster_search_variety.json",
        }
    )
    adapter = make_adapter(session, window_days=20)

    events = list(adapter.fetch(since=since))

    assert len(session.calls) == 2  # one request per 10-day window, not one for the whole 20-day span
    assert len(events) == 10  # 5 events per window, fetched twice


def test_fetch_stops_at_totalpages_without_extra_request():
    since = datetime(2026, 1, 1, tzinfo=UTC)
    start_str = since.strftime(ticketmaster_module.DATE_FORMAT)
    session = FakeSession({(start_str, 0): "ticketmaster_search_variety.json"})
    adapter = make_adapter(session, window_days=30)

    events = list(adapter.fetch(since=since))

    assert len(session.calls) == 1  # totalPages: 1 in the fixture, so page 1 is never requested
    assert len(events) == 5


def test_parse_event_maps_real_ticketmaster_fields():
    with (FIXTURES / "ticketmaster_search_window0_page0.json").open() as f:
        raw = json.load(f)["_embedded"]["events"][0]

    adapter = make_adapter(session=FakeSession({}))
    event = adapter._parse_event(raw)

    assert event.source_name == "ticketmaster"
    assert event.source_event_id == "1AUZk36GkdYn_Hk"
    assert event.title == "Wednesday"
    assert event.category == "music"
    assert event.status == "on_sale"
    assert event.venue_name == "Galvanizers SWG3"
    assert event.venue_city == "Glasgow"
    assert event.venue_postcode == "G3 8QG"
    assert event.venue_latitude == 55.86371100
    assert event.venue_longitude == -4.29821400
    assert isinstance(event.venue_latitude, float)
    assert event.on_sale_date is not None
    assert event.url.startswith("https://www.ticketmaster.co.uk/")


def test_parse_event_comedy_genre_maps_to_comedy_not_theatre():
    with (FIXTURES / "ticketmaster_search_variety.json").open() as f:
        events = json.load(f)["_embedded"]["events"]
    comedy_raw = next(e for e in events if e["classifications"][0].get("genre", {}).get("name") == "Comedy")

    adapter = make_adapter(session=FakeSession({}))
    event = adapter._parse_event(comedy_raw)

    assert event.category == "comedy"


def test_parse_event_cancelled_status_maps_to_cancelled():
    with (FIXTURES / "ticketmaster_search_variety.json").open() as f:
        events = json.load(f)["_embedded"]["events"]
    cancelled_raw = next(e for e in events if e["dates"]["status"]["code"] == "cancelled")

    adapter = make_adapter(session=FakeSession({}))
    event = adapter._parse_event(cancelled_raw)

    assert event.status == "cancelled"


def test_parse_event_missing_price_ranges_is_none_not_zero():
    with (FIXTURES / "ticketmaster_search_variety.json").open() as f:
        events = json.load(f)["_embedded"]["events"]
    unpriced_raw = next(e for e in events if not e.get("priceRanges"))

    adapter = make_adapter(session=FakeSession({}))
    event = adapter._parse_event(unpriced_raw)

    assert event.price_min is None
    assert event.price_max is None


def test_parse_event_with_price_ranges_present():
    with (FIXTURES / "ticketmaster_search_variety.json").open() as f:
        events = json.load(f)["_embedded"]["events"]
    priced_raw = next(e for e in events if e.get("priceRanges"))

    adapter = make_adapter(session=FakeSession({}))
    event = adapter._parse_event(priced_raw)

    assert event.price_min == 25.0
    assert event.price_max == 85.0
    assert event.currency == "GBP"


def test_cache_avoids_second_http_call(tmp_path):
    since = datetime(2026, 1, 1, tzinfo=UTC)
    start_str = since.strftime(ticketmaster_module.DATE_FORMAT)
    session = FakeSession({(start_str, 0): "ticketmaster_search_variety.json"})
    adapter = make_adapter(session, window_days=30, cache_dir=tmp_path)

    first = list(adapter.fetch(since=since))
    second = list(adapter.fetch(since=since))

    assert len(session.calls) == 1  # second fetch served entirely from disk cache
    assert len(first) == len(second) == 5
