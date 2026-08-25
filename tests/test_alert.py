from datetime import UTC, datetime, timedelta

import pytest

from events_agent.db import get_connection, init_db, upsert_raw_event
from events_agent.delivery.alert import find_alertable_changes, mark_notified
from events_agent.models import RawEvent


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "test.db"
    init_db(db_path)
    connection = get_connection(db_path)
    yield connection
    connection.close()


def make_raw_event(**overrides) -> RawEvent:
    defaults = dict(
        source_name="skiddle",
        source_event_id="42462366",
        title="The Hush Club - Glasgow's Top Secret Magic Experience",
        category="theatre",
        venue_name="Babbity Bowster",
        venue_city="Glasgow",
        status="on_sale",
        price_min=17.0,
        price_max=17.0,
        url="https://www.skiddle.com/whats-on/example/",
        raw={"id": "42462366"},
    )
    defaults.update(overrides)
    return RawEvent(**defaults)


def make_household(**overrides) -> dict:
    # No radius/price/blackout constraint that would exclude the events these
    # tests create (none of which set venue coordinates, so radius is a
    # no-op anyway) — permissive by default so these stay tests of alert
    # logic, not of the constraint filter (see test_household_scoping.py-style
    # cases below for that).
    defaults = dict(
        home_latitude=55.9410,
        home_longitude=-4.3170,
        radius_miles=1000,
        price_ceiling=None,
        blackout_dates=None,
    )
    defaults.update(overrides)
    return defaults


def test_status_flip_to_low_availability_produces_one_alert(conn):
    upsert_raw_event(conn, make_raw_event(status="on_sale"))
    upsert_raw_event(conn, make_raw_event(status="low_availability"))

    items = find_alertable_changes(conn, datetime.now(UTC), make_household())

    assert len(items) == 1
    assert items[0].title == "The Hush Club - Glasgow's Top Secret Magic Experience"
    assert items[0].venue_name == "Babbity Bowster"
    assert "low availability" in items[0].reason


def test_rerun_after_notifying_produces_no_alerts(conn):
    upsert_raw_event(conn, make_raw_event(status="on_sale"))
    upsert_raw_event(conn, make_raw_event(status="low_availability"))

    now = datetime.now(UTC)
    items = find_alertable_changes(conn, now, make_household())
    mark_notified(conn, [item.change_id for item in items], now)
    conn.commit()

    assert find_alertable_changes(conn, now, make_household()) == []


def test_on_sale_date_far_out_does_not_alert_yet(conn):
    upsert_raw_event(conn, make_raw_event(on_sale_date=None))
    far_out = datetime.now(UTC) + timedelta(days=10)
    upsert_raw_event(conn, make_raw_event(on_sale_date=far_out))

    items = find_alertable_changes(conn, datetime.now(UTC), make_household())

    assert items == []


def test_on_sale_date_within_48h_alerts(conn):
    upsert_raw_event(conn, make_raw_event(on_sale_date=None))
    soon = datetime.now(UTC) + timedelta(hours=36)
    upsert_raw_event(conn, make_raw_event(on_sale_date=soon))

    items = find_alertable_changes(conn, datetime.now(UTC), make_household())

    assert len(items) == 1
    assert "on sale" in items[0].reason


def test_on_sale_date_recorded_far_out_fires_once_the_window_closes(conn):
    upsert_raw_event(conn, make_raw_event(on_sale_date=None))
    on_sale = datetime.now(UTC) + timedelta(days=10)
    upsert_raw_event(conn, make_raw_event(on_sale_date=on_sale))

    assert find_alertable_changes(conn, datetime.now(UTC), make_household()) == []

    later = on_sale - timedelta(hours=1)
    items = find_alertable_changes(conn, later, make_household())

    assert len(items) == 1


def test_on_sale_date_already_in_the_past_does_not_alert(conn):
    # Regression: an on_sale_date change logged for a date that has already
    # passed (e.g. backfilled from a source that only now started providing
    # the field) trivially satisfies "<= now + 48h" for any past date too —
    # must also require new_value >= now.
    upsert_raw_event(conn, make_raw_event(on_sale_date=None))
    past = datetime.now(UTC) - timedelta(days=200)
    upsert_raw_event(conn, make_raw_event(on_sale_date=past))

    assert find_alertable_changes(conn, datetime.now(UTC), make_household()) == []


def test_new_event_never_alerts_on_first_sight(conn):
    upsert_raw_event(conn, make_raw_event(status="low_availability"))

    assert find_alertable_changes(conn, datetime.now(UTC), make_household()) == []


def test_household_scoping_excludes_out_of_range_events(conn):
    # Real Barrowland coordinates, ~2.5km from home — but a 1-mile household
    # radius rules it out. This is the actual point of household-scoping:
    # a household shouldn't get alerted about a change outside their own
    # radius/budget/blackout, which matters once more than one exists.
    upsert_raw_event(
        conn,
        make_raw_event(
            status="on_sale",
            venue_name="Barrowland Ballroom",
            venue_latitude=55.8550553,
            venue_longitude=-4.2369184,
        ),
    )
    upsert_raw_event(
        conn,
        make_raw_event(
            status="low_availability",
            venue_name="Barrowland Ballroom",
            venue_latitude=55.8550553,
            venue_longitude=-4.2369184,
        ),
    )

    narrow_household = make_household(home_latitude=55.9410, home_longitude=-4.3170, radius_miles=1)

    assert find_alertable_changes(conn, datetime.now(UTC), narrow_household) == []
