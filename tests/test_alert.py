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


def test_status_flip_to_low_availability_produces_one_alert(conn):
    upsert_raw_event(conn, make_raw_event(status="on_sale"))
    upsert_raw_event(conn, make_raw_event(status="low_availability"))

    items = find_alertable_changes(conn, datetime.now(UTC))

    assert len(items) == 1
    assert items[0].title == "The Hush Club - Glasgow's Top Secret Magic Experience"
    assert items[0].venue_name == "Babbity Bowster"
    assert "low availability" in items[0].reason


def test_rerun_after_notifying_produces_no_alerts(conn):
    upsert_raw_event(conn, make_raw_event(status="on_sale"))
    upsert_raw_event(conn, make_raw_event(status="low_availability"))

    now = datetime.now(UTC)
    items = find_alertable_changes(conn, now)
    mark_notified(conn, [item.change_id for item in items], now)
    conn.commit()

    assert find_alertable_changes(conn, now) == []


def test_on_sale_date_far_out_does_not_alert_yet(conn):
    upsert_raw_event(conn, make_raw_event(on_sale_date=None))
    far_out = datetime.now(UTC) + timedelta(days=10)
    upsert_raw_event(conn, make_raw_event(on_sale_date=far_out))

    items = find_alertable_changes(conn, datetime.now(UTC))

    assert items == []


def test_on_sale_date_within_48h_alerts(conn):
    upsert_raw_event(conn, make_raw_event(on_sale_date=None))
    soon = datetime.now(UTC) + timedelta(hours=36)
    upsert_raw_event(conn, make_raw_event(on_sale_date=soon))

    items = find_alertable_changes(conn, datetime.now(UTC))

    assert len(items) == 1
    assert "on sale" in items[0].reason


def test_on_sale_date_recorded_far_out_fires_once_the_window_closes(conn):
    upsert_raw_event(conn, make_raw_event(on_sale_date=None))
    on_sale = datetime.now(UTC) + timedelta(days=10)
    upsert_raw_event(conn, make_raw_event(on_sale_date=on_sale))

    assert find_alertable_changes(conn, datetime.now(UTC)) == []

    later = on_sale - timedelta(hours=1)
    items = find_alertable_changes(conn, later)

    assert len(items) == 1


def test_on_sale_date_already_in_the_past_does_not_alert(conn):
    # An on_sale_date change logged for a date that has already passed
    # trivially satisfies "<= now + 48h" for any past date too — must also
    # require new_value >= now.
    upsert_raw_event(conn, make_raw_event(on_sale_date=None))
    past = datetime.now(UTC) - timedelta(days=200)
    upsert_raw_event(conn, make_raw_event(on_sale_date=past))

    assert find_alertable_changes(conn, datetime.now(UTC)) == []


def test_new_event_never_alerts_on_first_sight(conn):
    upsert_raw_event(conn, make_raw_event(status="low_availability"))

    assert find_alertable_changes(conn, datetime.now(UTC)) == []
