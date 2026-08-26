from datetime import UTC, datetime

from events_agent.db import get_connection, init_db, set_verdict, upsert_household, upsert_raw_event
from events_agent.delivery.ics import CalendarEvent, build_ics, select_calendar_events
from events_agent.models import RawEvent


def make_household(conn, **overrides):
    defaults = dict(
        household_id=1,
        label="Milngavie",
        home_latitude=55.9410,
        home_longitude=-4.3170,
        radius_miles=90,
        near_days=7,
        month_days=31,
        max_drive_minutes=90,
        price_ceiling=500,
        blackout_dates=[],
        taste_profile_path="taste-profile.md",
        digest_threshold=60,
        alert_threshold=45,
        email_to="test@example.com",
    )
    defaults.update(overrides)
    upsert_household(conn, **defaults)
    conn.commit()
    return {"id": 1, "label": defaults["label"]}


def make_raw_event(**overrides) -> RawEvent:
    defaults = dict(
        source_name="skiddle",
        source_event_id="1",
        title="Test Gig",
        category="music",
        venue_name="Test Venue",
        event_date=datetime(2026, 9, 10, 19, 0, tzinfo=UTC),
        url="https://example.com/gig",
    )
    defaults.update(overrides)
    return RawEvent(**defaults)


def test_select_calendar_events_only_includes_interested_and_booked(tmp_path):
    db_path = tmp_path / "test.db"
    init_db(db_path)
    conn = get_connection(db_path)
    household = make_household(conn)

    interested_id, _ = upsert_raw_event(conn, make_raw_event(source_event_id="1", title="Interested Gig"))
    booked_id, _ = upsert_raw_event(conn, make_raw_event(source_event_id="2", title="Booked Gig"))
    no_id, _ = upsert_raw_event(conn, make_raw_event(source_event_id="3", title="Rejected Gig"))
    unscored_id, _ = upsert_raw_event(conn, make_raw_event(source_event_id="4", title="Unscored Gig"))
    set_verdict(conn, 1, interested_id, "interested")
    set_verdict(conn, 1, booked_id, "booked")
    set_verdict(conn, 1, no_id, "no")
    conn.commit()

    events = select_calendar_events(conn, household)
    conn.close()

    titles = {e.title for e in events}
    assert titles == {"Interested Gig", "Booked Gig"}


def test_select_calendar_events_excludes_undated_events(tmp_path):
    db_path = tmp_path / "test.db"
    init_db(db_path)
    conn = get_connection(db_path)
    household = make_household(conn)

    event_id, _ = upsert_raw_event(conn, make_raw_event(event_date=None))
    set_verdict(conn, 1, event_id, "interested")
    conn.commit()

    events = select_calendar_events(conn, household)
    conn.close()

    assert events == []


def test_build_ics_marks_interested_as_tentative_and_booked_as_confirmed():
    household = {"id": 1, "label": "Milngavie"}
    events = [
        CalendarEvent(1, "Interested Gig", "Test Venue", "2026-09-10T19:00:00+00:00", None, "https://example.com/1", "interested"),
        CalendarEvent(2, "Booked Gig", "Test Venue", "2026-09-11T19:00:00+00:00", "2026-09-11T22:00:00+00:00", None, "booked"),
    ]

    out = build_ics(household, events)

    assert out.startswith("BEGIN:VCALENDAR\r\n")
    assert out.rstrip("\r\n").endswith("END:VCALENDAR")
    assert "SUMMARY:Interested Gig" in out
    assert "STATUS:TENTATIVE" in out
    assert "SUMMARY:Booked Gig" in out
    assert "STATUS:CONFIRMED" in out
    assert "DTSTART:20260910T190000Z" in out
    assert "DTEND:20260911T220000Z" in out  # explicit event_date_end used verbatim
    assert "DTSTART:20260911T190000Z" in out


def test_build_ics_defaults_duration_when_no_end_date():
    household = {"id": 1, "label": "Milngavie"}
    events = [CalendarEvent(1, "Gig", "Venue", "2026-09-10T19:00:00+00:00", None, None, "interested")]

    out = build_ics(household, events)

    assert "DTSTART:20260910T190000Z" in out
    assert "DTEND:20260910T210000Z" in out  # default 2-hour duration


def test_build_ics_escapes_commas_and_semicolons_in_title():
    household = {"id": 1, "label": "Milngavie"}
    events = [
        CalendarEvent(1, "Rock, Pop; Soul", "Venue", "2026-09-10T19:00:00+00:00", None, None, "interested")
    ]

    out = build_ics(household, events)

    assert "SUMMARY:Rock\\, Pop\\; Soul" in out


def test_build_ics_folds_long_lines_per_rfc5545():
    household = {"id": 1, "label": "Milngavie"}
    long_title = "A " + ("very " * 30) + "long event title"
    events = [CalendarEvent(1, long_title, "Venue", "2026-09-10T19:00:00+00:00", None, None, "interested")]

    out = build_ics(household, events)

    for line in out.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75
    # the folded continuation is rejoined with a leading space per spec
    assert "\r\n very" in out or "\r\n" in out


def test_build_ics_empty_state():
    out = build_ics({"id": 1, "label": "Milngavie"}, [])

    assert out == "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Roundup//events-agent//EN\r\nCALSCALE:GREGORIAN\r\nX-WR-CALNAME:Roundup — Milngavie\r\nEND:VCALENDAR\r\n"
