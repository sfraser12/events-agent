"""SkiddleAdapter tests. All HTTP calls are faked from saved fixtures — no network."""

import json
from pathlib import Path

from events_agent.sources import skiddle as skiddle_module
from events_agent.sources.skiddle import SkiddleAdapter

FIXTURES = Path(__file__).parent / "fixtures"


class FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class FakeSession:
    """Routes (eventcode, offset) -> a saved fixture file; errors on anything unmapped."""

    def __init__(self, routes: dict[tuple[str, int], str]):
        self.routes = routes
        self.calls: list[dict] = []

    def get(self, url, params, timeout):
        self.calls.append(dict(params))
        key = (params["eventcode"], params["offset"])
        if key not in self.routes:
            raise AssertionError(f"unexpected request: {key}")
        with (FIXTURES / self.routes[key]).open() as f:
            return FakeResponse(json.load(f))


def make_adapter(session, cache_dir=None):
    return SkiddleAdapter(
        api_key="test-key",
        latitude=55.9410,
        longitude=-4.3170,
        radius_miles=25,
        session=session,
        cache_dir=cache_dir,
    )


def test_fetch_paginates_across_offsets_until_totalcount_reached(monkeypatch):
    monkeypatch.setattr(skiddle_module, "EVENTCODES", ("THEATRE",))
    session = FakeSession(
        {
            ("THEATRE", 0): "skiddle_search_theatre_offset0.json",
            ("THEATRE", 5): "skiddle_search_theatre_offset5.json",
            ("THEATRE", 10): "skiddle_search_theatre_offset10.json",
        }
    )
    adapter = make_adapter(session)

    events = list(adapter.fetch())

    assert len(session.calls) == 3  # stops after offset 10 + 1 result >= totalcount 11
    assert len(events) == 11
    assert {e.source_event_id for e in events} == {
        "42462366", "41790194", "42624500", "42594309", "42462367",
        "42594310", "41790307", "42644334", "42462368", "41790310",
        "42483349",
    }
    assert all(e.category == "theatre" for e in events)
    assert all(e.source_name == "skiddle" for e in events)


def test_fetch_stops_after_single_full_page(monkeypatch):
    monkeypatch.setattr(skiddle_module, "EVENTCODES", ("FEST",))
    session = FakeSession({("FEST", 0): "skiddle_search_fest.json"})
    adapter = make_adapter(session)

    events = list(adapter.fetch())

    assert len(session.calls) == 1
    assert len(events) == 8
    assert all(e.category == "music" for e in events)


def test_fetch_queries_every_configured_eventcode(monkeypatch):
    monkeypatch.setattr(skiddle_module, "EVENTCODES", ("FEST", "THEATRE"))
    session = FakeSession(
        {
            ("FEST", 0): "skiddle_search_fest.json",
            ("THEATRE", 0): "skiddle_search_theatre_offset0.json",
            ("THEATRE", 5): "skiddle_search_theatre_offset5.json",
            ("THEATRE", 10): "skiddle_search_theatre_offset10.json",
        }
    )
    adapter = make_adapter(session)

    events = list(adapter.fetch())

    assert len(events) == 8 + 11
    categories = {e.category for e in events}
    assert categories == {"music", "theatre"}


def test_parse_event_maps_real_skiddle_fields():
    with (FIXTURES / "skiddle_search_theatre_offset0.json").open() as f:
        raw = json.load(f)["results"][0]

    adapter = make_adapter(session=FakeSession({}))
    event = adapter._parse_event(raw, "THEATRE")

    assert event.source_name == "skiddle"
    assert event.source_event_id == "42462366"
    assert event.title == "The Hush Club - Glasgow's Top Secret Magic Experience"
    assert event.category == "theatre"
    assert event.status == "cancelled"  # raw["cancelled"] == "1"
    assert event.venue_name == "Babbity Bowster"
    assert event.venue_city == "Glasgow"
    assert event.venue_postcode == "G1 1PE"
    assert event.venue_type == "bar"
    assert event.price_min == 17
    assert event.price_max == 17
    assert event.currency == "GBP"
    assert event.min_age == 16
    assert event.doors_open == "19:30"
    assert event.event_date.isoformat() == "2026-08-22T19:30:00+00:00"
    assert event.url.startswith("https://www.skiddle.com/whats-on/")
    assert event.raw["id"] == "42462366"


def test_parse_event_not_cancelled_maps_to_on_sale():
    with (FIXTURES / "skiddle_search_theatre_offset0.json").open() as f:
        raw = json.load(f)["results"][1]  # cancelled == "0", hotSeller == false

    adapter = make_adapter(session=FakeSession({}))
    event = adapter._parse_event(raw, "THEATRE")

    assert event.status == "on_sale"


def test_parse_event_hot_seller_maps_to_low_availability():
    with (FIXTURES / "skiddle_search_theatre_offset0.json").open() as f:
        raw = json.load(f)["results"][3]  # hotSeller == true, cancelled == "0"

    adapter = make_adapter(session=FakeSession({}))
    event = adapter._parse_event(raw, "THEATRE")

    assert event.status == "low_availability"


def test_parse_event_rescheduled_date_maps_to_cancelled():
    with (FIXTURES / "skiddle_search_theatre_offset0.json").open() as f:
        raw = json.load(f)["results"][2]  # cancelled == "0" but rescheduledDate is set

    adapter = make_adapter(session=FakeSession({}))
    event = adapter._parse_event(raw, "THEATRE")

    assert event.status == "cancelled"


def test_parse_event_zero_price_treated_as_unknown_not_free():
    with (FIXTURES / "skiddle_search_theatre_offset0.json").open() as f:
        raw = json.load(f)["results"][4]  # ticketpricing minPrice/maxPrice == 0/0

    adapter = make_adapter(session=FakeSession({}))
    event = adapter._parse_event(raw, "THEATRE")

    assert event.price_min is None
    assert event.price_max is None


def test_parse_event_blank_min_age_is_none():
    with (FIXTURES / "skiddle_search_theatre_offset0.json").open() as f:
        raw = json.load(f)["results"][2]  # minage == ""

    adapter = make_adapter(session=FakeSession({}))
    event = adapter._parse_event(raw, "THEATRE")

    assert event.min_age is None


def test_cache_avoids_second_http_call(tmp_path, monkeypatch):
    monkeypatch.setattr(skiddle_module, "EVENTCODES", ("FEST",))
    session = FakeSession({("FEST", 0): "skiddle_search_fest.json"})
    adapter = make_adapter(session, cache_dir=tmp_path)

    first = list(adapter.fetch())
    second = list(adapter.fetch())

    assert len(session.calls) == 1  # second fetch served entirely from disk cache
    assert len(first) == len(second) == 8
