from datetime import UTC, date, datetime, timedelta

import pytest

from events_agent.db import (
    get_connection,
    get_household_as_dict,
    init_db,
    set_verdict,
    upsert_household,
    upsert_raw_event,
    upsert_score,
)
from events_agent.delivery.digest import build_digest, build_digest_html, build_digest_plain
from events_agent.models import RawEvent


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "test.db"
    init_db(db_path)
    connection = get_connection(db_path)
    yield connection
    connection.close()


@pytest.fixture
def household(conn, tmp_path):
    taste_path = tmp_path / "taste-profile.md"
    taste_path.write_text("test")
    upsert_household(
        conn,
        household_id=1,
        label="Test household",
        home_latitude=55.9410,
        home_longitude=-4.3170,
        radius_miles=25,
        near_days=7,
        month_days=31,
        max_drive_minutes=90,
        price_ceiling=200,
        blackout_dates=[],
        taste_profile_path=str(taste_path),
        digest_threshold=60,
        alert_threshold=45,
        digest_day="sunday",
        digest_hour=8,
        email_to="test@example.com",
    )
    conn.commit()
    return get_household_as_dict(conn, 1)


def make_raw_event(**overrides) -> RawEvent:
    defaults = dict(
        source_name="skiddle",
        source_event_id="1",
        title="Karine Polwart",
        category="music",
        venue_name="Oran Mor",
        venue_city="Glasgow",
        event_date=datetime.now(UTC) + timedelta(days=3),
        status="on_sale",
        price_min=25.0,
        price_max=25.0,
        url="https://example.com/karine",
        raw={},
    )
    defaults.update(overrides)
    return RawEvent(**defaults)


def score_event(conn, household_id, event_id, score, **kw):
    defaults = dict(audience="both", score_reason="great fit", urgency="none", scored_at=datetime.now(UTC).isoformat())
    defaults.update(kw)
    upsert_score(conn, household_id, event_id, score, **defaults)


def test_high_scoring_event_appears_in_digest(conn, household):
    event_id, _ = upsert_raw_event(conn, make_raw_event())
    score_event(conn, household["id"], event_id, 80)
    conn.commit()

    horizons = build_digest(conn, household)

    all_events = [e for events in horizons.values() for e in events]
    assert len(all_events) == 1
    assert all_events[0].title == "Karine Polwart"


def test_below_threshold_event_is_excluded(conn, household):
    event_id, _ = upsert_raw_event(conn, make_raw_event())
    score_event(conn, household["id"], event_id, 40)  # below digest_threshold=60
    conn.commit()

    horizons = build_digest(conn, household)

    assert sum(len(e) for e in horizons.values()) == 0


def test_verdict_no_excludes_event_from_next_digest(conn, household):
    # The literal Phase 4 "done when" bar: marking something no keeps it out.
    event_id, _ = upsert_raw_event(conn, make_raw_event())
    score_event(conn, household["id"], event_id, 90)
    conn.commit()
    assert sum(len(e) for e in build_digest(conn, household).values()) == 1

    set_verdict(conn, household["id"], event_id, "no")
    conn.commit()

    assert sum(len(e) for e in build_digest(conn, household).values()) == 0


def test_snoozed_event_is_excluded_until_snooze_expires(conn, household):
    event_id, _ = upsert_raw_event(conn, make_raw_event())
    score_event(conn, household["id"], event_id, 90)
    future_snooze = (datetime.now(UTC) + timedelta(days=30)).isoformat()
    set_verdict(conn, household["id"], event_id, "interested", snoozed_until=future_snooze)
    conn.commit()

    assert sum(len(e) for e in build_digest(conn, household).values()) == 0


def test_events_are_grouped_by_horizon(conn, household):
    today = datetime.now(UTC)
    near_id, _ = upsert_raw_event(
        conn, make_raw_event(source_event_id="near", title="Near event", event_date=today + timedelta(days=2))
    )
    month_id, _ = upsert_raw_event(
        conn, make_raw_event(source_event_id="month", title="Month event", event_date=today + timedelta(days=20))
    )
    later_id, _ = upsert_raw_event(
        conn, make_raw_event(source_event_id="later", title="Later event", event_date=today + timedelta(days=200))
    )
    for eid in (near_id, month_id, later_id):
        score_event(conn, household["id"], eid, 90)
    conn.commit()

    horizons = build_digest(conn, household, today=today.date())

    assert [e.title for e in horizons["this week"]] == ["Near event"]
    assert [e.title for e in horizons["this month"]] == ["Month event"]
    assert [e.title for e in horizons["announced for later"]] == ["Later event"]


def test_imminent_on_sale_date_takes_priority_over_event_date_horizon(conn, household):
    today = datetime.now(UTC)
    event_id, _ = upsert_raw_event(
        conn,
        make_raw_event(
            title="Far-off gig, on sale soon",
            event_date=today + timedelta(days=200),
            on_sale_date=today + timedelta(days=2),
        ),
    )
    score_event(conn, household["id"], event_id, 90)
    conn.commit()

    horizons = build_digest(conn, household, today=today.date())

    assert [e.title for e in horizons["on sale soon"]] == ["Far-off gig, on sale soon"]
    assert horizons["announced for later"] == []


def test_build_digest_html_includes_event_titles(conn, household):
    event_id, _ = upsert_raw_event(conn, make_raw_event())
    score_event(conn, household["id"], event_id, 80)
    conn.commit()

    horizons = build_digest(conn, household)
    html = build_digest_html(household, horizons)

    assert "Karine Polwart" in html
    assert household["label"] in html


def test_build_digest_plain_says_nothing_new_when_empty(household):
    empty_horizons = {"on sale soon": [], "this week": [], "this month": [], "announced for later": []}
    plain = build_digest_plain(household, empty_horizons)

    assert "Nothing new this week." in plain
