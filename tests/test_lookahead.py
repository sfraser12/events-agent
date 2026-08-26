from datetime import UTC, datetime, timedelta

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
from events_agent.delivery.lookahead import (
    LookaheadEvent,
    build_lookahead_html,
    build_lookahead_plain,
    select_lookahead_events,
)
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
        email_to="test@example.com",
    )
    conn.commit()
    return get_household_as_dict(conn, 1)


def make_raw_event(**overrides) -> RawEvent:
    defaults = dict(
        source_name="skiddle",
        source_event_id="1",
        title="Some Gig",
        category="music",
        venue_name="Oran Mor",
        event_date=datetime.now(UTC) + timedelta(days=5),
        price_min=20.0,
        price_max=20.0,
        url="https://example.com/gig",
    )
    defaults.update(overrides)
    return RawEvent(**defaults)


def score_event(conn, household_id, event_id, score, **kw):
    defaults = dict(audience="both", score_reason="near miss", urgency="none", scored_at=datetime.now(UTC).isoformat())
    defaults.update(kw)
    upsert_score(conn, household_id, event_id, score, **defaults)


def test_below_digest_threshold_within_window_is_included(conn, household):
    event_id, _ = upsert_raw_event(conn, make_raw_event())
    score_event(conn, household["id"], event_id, 50)  # between alert_threshold(45) and digest_threshold(60)
    conn.commit()

    events = select_lookahead_events(conn, household)

    assert len(events) == 1
    assert events[0].title == "Some Gig"


def test_event_at_or_above_digest_threshold_is_excluded(conn, household):
    # Already covered by the weekly digest — showing it here too is exactly
    # the clutter this email exists to avoid.
    event_id, _ = upsert_raw_event(conn, make_raw_event())
    score_event(conn, household["id"], event_id, 60)
    conn.commit()

    assert select_lookahead_events(conn, household) == []


def test_event_below_alert_threshold_is_excluded(conn, household):
    event_id, _ = upsert_raw_event(conn, make_raw_event())
    score_event(conn, household["id"], event_id, 30)  # genuinely poor fit
    conn.commit()

    assert select_lookahead_events(conn, household) == []


def test_event_outside_the_fortnight_window_is_excluded(conn, household):
    event_id, _ = upsert_raw_event(conn, make_raw_event(event_date=datetime.now(UTC) + timedelta(days=30)))
    score_event(conn, household["id"], event_id, 50)
    conn.commit()

    assert select_lookahead_events(conn, household) == []


def test_event_never_scored_is_excluded(conn, household):
    # No household_event_state row at all yet (scoring hasn't run on it) —
    # nothing to join against, so it can't appear here either. Distinct from
    # the NULL-score case below, where a row exists but scoring failed.
    upsert_raw_event(conn, make_raw_event())
    conn.commit()

    assert select_lookahead_events(conn, household) == []


def test_null_score_row_is_included(conn, household):
    event_id, _ = upsert_raw_event(conn, make_raw_event())
    score_event(conn, household["id"], event_id, None, score_reason=None)
    conn.commit()

    events = select_lookahead_events(conn, household)

    assert len(events) == 1


def test_verdict_no_excludes_it(conn, household):
    event_id, _ = upsert_raw_event(conn, make_raw_event())
    score_event(conn, household["id"], event_id, 50)
    set_verdict(conn, household["id"], event_id, "no")
    conn.commit()

    assert select_lookahead_events(conn, household) == []


def test_snoozed_event_is_excluded(conn, household):
    event_id, _ = upsert_raw_event(conn, make_raw_event())
    score_event(conn, household["id"], event_id, 50)
    future = (datetime.now(UTC) + timedelta(days=30)).isoformat()
    set_verdict(conn, household["id"], event_id, "interested", snoozed_until=future)
    conn.commit()

    assert select_lookahead_events(conn, household) == []


def test_build_lookahead_html_shows_eyebrow_and_events():
    household = {"id": 1, "label": "Milngavie"}
    events = [
        LookaheadEvent(1, "Near-Miss Gig", "Test Venue", (datetime.now(UTC) + timedelta(days=3)).isoformat(), 20.0, 20.0, "GBP", "https://example.com/gig", 50, "decent fit")
    ]

    out = build_lookahead_html(household, events)

    assert "Heads up" in out
    assert "Near-Miss Gig" in out
    assert "score 50" in out
    assert "id 1" in out


def test_build_lookahead_html_empty_state():
    out = build_lookahead_html({"id": 1, "label": "Milngavie"}, [])

    assert "Nothing in the next fortnight" in out


def test_build_lookahead_plain_includes_url_and_id():
    household = {"id": 1, "label": "Milngavie"}
    events = [
        LookaheadEvent(1, "Near-Miss Gig", "Test Venue", (datetime.now(UTC) + timedelta(days=3)).isoformat(), 20.0, 20.0, "GBP", "https://example.com/gig", 50, "decent fit")
    ]

    out = build_lookahead_plain(household, events)

    assert "Near-Miss Gig @ Test Venue" in out
    assert "id 1" in out
    assert "https://example.com/gig" in out
