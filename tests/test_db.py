import sqlite3
from datetime import UTC, date, datetime

import pytest

from events_agent.db import (
    get_connection,
    get_household_as_dict,
    init_db,
    list_households_as_dicts,
    mark_past_events,
    upsert_household,
    upsert_raw_event,
    upsert_venue,
)
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
        venue_postcode="G1 1PE",
        venue_latitude=55.858409,
        venue_longitude=-4.242684,
        venue_type="bar",
        event_date=datetime(2026, 8, 22, 19, 30, tzinfo=UTC),
        event_date_end=datetime(2026, 8, 22, 21, 0, tzinfo=UTC),
        status="on_sale",
        price_min=17.0,
        price_max=17.0,
        currency="GBP",
        url="https://www.skiddle.com/whats-on/example/",
        blurb="A night of mind reading & trickery",
        raw={"id": "42462366"},
    )
    defaults.update(overrides)
    return RawEvent(**defaults)


def test_upsert_raw_event_inserts_new_event(conn):
    event_id, created = upsert_raw_event(conn, make_raw_event())

    assert created is True
    row = conn.execute("SELECT title, status FROM event WHERE id = ?", (event_id,)).fetchone()
    assert row == ("The Hush Club - Glasgow's Top Secret Magic Experience", "on_sale")


def test_upsert_raw_event_is_idempotent_on_rerun(conn):
    first_id, first_created = upsert_raw_event(conn, make_raw_event())
    second_id, second_created = upsert_raw_event(conn, make_raw_event())

    assert first_created is True
    assert second_created is False
    assert first_id == second_id
    count = conn.execute("SELECT COUNT(*) FROM event").fetchone()[0]
    assert count == 1


def test_upsert_raw_event_updates_changed_status(conn):
    event_id, _ = upsert_raw_event(conn, make_raw_event(status="on_sale"))
    upsert_raw_event(conn, make_raw_event(status="cancelled"))

    status = conn.execute("SELECT status FROM event WHERE id = ?", (event_id,)).fetchone()[0]
    assert status == "cancelled"


def test_upsert_raw_event_reuses_venue_by_normalised_name(conn):
    upsert_raw_event(conn, make_raw_event(source_event_id="1", title="Event One"))
    upsert_raw_event(conn, make_raw_event(source_event_id="2", title="Event Two"))

    count = conn.execute("SELECT COUNT(*) FROM venue").fetchone()[0]
    assert count == 1


def test_upsert_raw_event_records_event_source_row(conn):
    event_id, _ = upsert_raw_event(conn, make_raw_event())

    row = conn.execute(
        "SELECT event_id, source_url, raw_json FROM event_source WHERE source_name = ? AND source_event_id = ?",
        ("skiddle", "42462366"),
    ).fetchone()
    assert row[0] == event_id
    assert row[1] == "https://www.skiddle.com/whats-on/example/"
    assert '"id": "42462366"' in row[2]


def test_different_events_get_different_fingerprints(conn):
    id_a, _ = upsert_raw_event(conn, make_raw_event(source_event_id="1", title="Event A"))
    id_b, _ = upsert_raw_event(conn, make_raw_event(source_event_id="2", title="Event B"))

    assert id_a != id_b
    count = conn.execute("SELECT COUNT(*) FROM event").fetchone()[0]
    assert count == 2


def test_upsert_venue_updates_existing_row_on_rerun(conn):
    first_id = upsert_venue(conn, "Babbity Bowster", "Glasgow", "G1 1PE", 55.858, -4.242)
    second_id = upsert_venue(conn, "Babbity Bowster", "Glasgow", "G1 1PE", 55.859, -4.243)

    assert first_id == second_id
    row = conn.execute("SELECT latitude, longitude FROM venue WHERE id = ?", (first_id,)).fetchone()
    assert row == (55.859, -4.243)


def test_upsert_raw_event_stores_venue_type(conn):
    upsert_raw_event(conn, make_raw_event())

    venue_type = conn.execute("SELECT type FROM venue WHERE name_normalised = 'babbity bowster'").fetchone()[0]
    assert venue_type == "bar"


def test_upsert_venue_resolves_by_proximity_when_names_differ(conn):
    # Real drift observed between Skiddle and Ticketmaster's pins for the same
    # SWG3 building: ~140m apart, different spelling ("Galvanizers, SWG3" vs
    # "Galvanizers SWG3" — this test uses names that don't normalise the same,
    # unlike that real pair, to isolate the proximity fallback specifically).
    first_id = upsert_venue(conn, "Galvanizers SWG3", "Glasgow", "G3 8QG", 55.86371100, -4.29821400)
    second_id = upsert_venue(conn, "Galvanizers - SWG3 Glasgow", "Glasgow", "G3 8QG", 55.8645723, -4.2995517)

    assert first_id == second_id
    count = conn.execute("SELECT COUNT(*) FROM venue").fetchone()[0]
    assert count == 1


def test_upsert_venue_proximity_match_does_not_rename_the_venue(conn):
    first_id = upsert_venue(conn, "Galvanizers SWG3", "Glasgow", "G3 8QG", 55.86371100, -4.29821400)
    upsert_venue(conn, "Galvanizers - SWG3 Glasgow", "Glasgow", "G3 8QG", 55.8645723, -4.2995517)

    name = conn.execute("SELECT name FROM venue WHERE id = ?", (first_id,)).fetchone()[0]
    assert name == "Galvanizers SWG3"  # the first-seen spelling wins, not the second source's


def test_upsert_venue_beyond_proximity_threshold_creates_a_new_venue(conn):
    upsert_venue(conn, "OVO Hydro", "Glasgow", "G3 8YW", 55.859881, -4.285367)
    # Real Barrowland coordinates — ~2.5km from the Hydro, well outside the 150m fallback.
    upsert_venue(conn, "Barrowland Ballroom Extra", "Glasgow", "G4 0TT", 55.8550553, -4.2369184)

    count = conn.execute("SELECT COUNT(*) FROM venue").fetchone()[0]
    assert count == 2


def test_upsert_venue_type_is_not_erased_by_a_source_that_lacks_it(conn):
    # Skiddle sets a real type first...
    venue_id = upsert_venue(conn, "OVO Hydro", "Glasgow", "G3 8YW", 55.859881, -4.285367, venue_type="arena")
    # ...then Ticketmaster harvests the same venue by exact name, but it has
    # no concept of venue "type" and always passes None — that must not wipe
    # out the type Skiddle already recorded.
    upsert_venue(conn, "OVO Hydro", "Glasgow", "G3 8YW", 55.859881, -4.285367, venue_type=None)

    venue_type = conn.execute("SELECT type FROM venue WHERE id = ?", (venue_id,)).fetchone()[0]
    assert venue_type == "arena"


def test_upsert_raw_event_logs_no_change_on_first_insert(conn):
    upsert_raw_event(conn, make_raw_event())

    count = conn.execute("SELECT COUNT(*) FROM event_change").fetchone()[0]
    assert count == 0


def test_upsert_raw_event_logs_status_change(conn):
    event_id, _ = upsert_raw_event(conn, make_raw_event(status="on_sale"))
    upsert_raw_event(conn, make_raw_event(status="low_availability"))

    row = conn.execute(
        "SELECT field, old_value, new_value, notified_at FROM event_change WHERE event_id = ? AND field = 'status'",
        (event_id,),
    ).fetchone()
    assert row == ("status", "on_sale", "low_availability", None)


def test_upsert_raw_event_does_not_log_unchanged_status_on_rerun(conn):
    event_id, _ = upsert_raw_event(conn, make_raw_event(status="on_sale"))
    upsert_raw_event(conn, make_raw_event(status="on_sale"))

    count = conn.execute(
        "SELECT COUNT(*) FROM event_change WHERE event_id = ? AND field = 'status'", (event_id,)
    ).fetchone()[0]
    assert count == 0


def test_upsert_raw_event_logs_price_range_change(conn):
    event_id, _ = upsert_raw_event(conn, make_raw_event(price_min=17.0, price_max=17.0))
    upsert_raw_event(conn, make_raw_event(price_min=20.0, price_max=25.0))

    row = conn.execute(
        "SELECT old_value, new_value FROM event_change WHERE event_id = ? AND field = 'price_range'",
        (event_id,),
    ).fetchone()
    assert row == ("17.0-17.0", "20.0-25.0")


def test_upsert_raw_event_does_not_log_price_change_for_int_vs_float_same_value(conn):
    # SQLite round-trips a REAL column as float (15.0); fresh JSON can hand
    # back a whole-number price as int (15). Same price, must not diff.
    event_id, _ = upsert_raw_event(conn, make_raw_event(price_min=15.0, price_max=15.0))
    upsert_raw_event(conn, make_raw_event(price_min=15, price_max=15))

    count = conn.execute(
        "SELECT COUNT(*) FROM event_change WHERE event_id = ? AND field = 'price_range'", (event_id,)
    ).fetchone()[0]
    assert count == 0


def test_upsert_raw_event_logs_on_sale_date_change(conn):
    event_id, _ = upsert_raw_event(conn, make_raw_event(on_sale_date=None))
    on_sale = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
    upsert_raw_event(conn, make_raw_event(on_sale_date=on_sale))

    row = conn.execute(
        "SELECT old_value, new_value FROM event_change WHERE event_id = ? AND field = 'on_sale_date'",
        (event_id,),
    ).fetchone()
    assert row == (None, on_sale.isoformat())


def test_mark_past_events_flips_status_for_elapsed_dates(conn):
    event_id, _ = upsert_raw_event(
        conn, make_raw_event(event_date=datetime(2026, 8, 22, 19, 30, tzinfo=UTC), status="on_sale")
    )

    marked = mark_past_events(conn, today=date(2026, 8, 24))

    assert marked == 1
    status = conn.execute("SELECT status FROM event WHERE id = ?", (event_id,)).fetchone()[0]
    assert status == "past"


def test_mark_past_events_logs_an_event_change(conn):
    event_id, _ = upsert_raw_event(
        conn, make_raw_event(event_date=datetime(2026, 8, 22, 19, 30, tzinfo=UTC), status="on_sale")
    )

    mark_past_events(conn, today=date(2026, 8, 24))

    row = conn.execute(
        "SELECT old_value, new_value FROM event_change WHERE event_id = ? AND field = 'status'", (event_id,)
    ).fetchone()
    assert row == ("on_sale", "past")


def test_mark_past_events_ignores_future_events(conn):
    upsert_raw_event(conn, make_raw_event(event_date=datetime(2026, 9, 1, 19, 30, tzinfo=UTC), status="on_sale"))

    marked = mark_past_events(conn, today=date(2026, 8, 24))

    assert marked == 0
    status = conn.execute("SELECT status FROM event").fetchone()[0]
    assert status == "on_sale"


def test_mark_past_events_does_not_overwrite_cancelled(conn):
    upsert_raw_event(
        conn, make_raw_event(event_date=datetime(2026, 8, 22, 19, 30, tzinfo=UTC), status="cancelled")
    )

    marked = mark_past_events(conn, today=date(2026, 8, 24))

    assert marked == 0
    status = conn.execute("SELECT status FROM event").fetchone()[0]
    assert status == "cancelled"


def test_mark_past_events_ignores_undated_events(conn):
    upsert_raw_event(conn, make_raw_event(event_date=None, status="on_sale"))

    marked = mark_past_events(conn, today=date(2026, 8, 24))

    assert marked == 0


def test_upsert_household_seeds_a_new_row(conn, tmp_path):
    upsert_household(
        conn,
        household_id=1,
        label="Milngavie",
        home_latitude=55.9410,
        home_longitude=-4.3170,
        radius_miles=25,
        near_days=7,
        month_days=31,
        max_drive_minutes=90,
        price_ceiling=200,
        blackout_dates=[("2026-10-12", "2026-10-26")],
        taste_profile_path=str(tmp_path / "taste-profile.md"),
        digest_threshold=60,
        alert_threshold=45,
        email_to="test@example.com",
    )
    conn.commit()

    household = get_household_as_dict(conn, 1)
    assert household["label"] == "Milngavie"
    assert household["radius_miles"] == 25
    assert household["blackout_dates"] == '[["2026-10-12", "2026-10-26"]]'


def test_upsert_household_is_idempotent_on_rerun(conn, tmp_path):
    kwargs = dict(
        household_id=1,
        home_latitude=55.9410,
        home_longitude=-4.3170,
        radius_miles=25,
        near_days=7,
        month_days=31,
        max_drive_minutes=90,
        price_ceiling=200,
        blackout_dates=[],
        taste_profile_path=str(tmp_path / "taste-profile.md"),
        digest_threshold=60,
        alert_threshold=45,
        email_to="test@example.com",
    )
    upsert_household(conn, label="Milngavie", **kwargs)
    upsert_household(conn, label="Milngavie (renamed)", **kwargs)
    conn.commit()

    assert len(list_households_as_dicts(conn)) == 1
    assert get_household_as_dict(conn, 1)["label"] == "Milngavie (renamed)"


def test_init_db_migrates_a_pre_phase_4_database(tmp_path):
    """Simulates a database created before household_event_state existed —
    score/verdict/etc. still live directly on `event`, all NULL (as they
    always were pre-Phase-4, since scoring never ran), plus venue.drive_minutes.
    init_db must drop them and add the new tables without error.
    """
    db_path = tmp_path / "old.db"
    old_conn = sqlite3.connect(db_path)
    old_conn.executescript(
        """
        CREATE TABLE venue (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, name_normalised TEXT NOT NULL UNIQUE,
            city TEXT, postcode TEXT, latitude REAL, longitude REAL, drive_minutes INTEGER, type TEXT
        );
        CREATE TABLE event (
            id INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL, title TEXT NOT NULL,
            title_normalised TEXT NOT NULL, category TEXT, venue_id INTEGER REFERENCES venue(id),
            event_date TEXT, event_date_end TEXT, announced_date TEXT, on_sale_date TEXT,
            status TEXT, price_min REAL, price_max REAL, currency TEXT DEFAULT 'GBP',
            url TEXT, blurb TEXT, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
            score INTEGER, audience TEXT, score_reason TEXT, urgency TEXT, scored_at TEXT,
            verdict TEXT, verdict_at TEXT, snoozed_until TEXT, surfaced_at TEXT
        );
        CREATE INDEX idx_event_verdict ON event(verdict);
        CREATE TABLE event_change (
            id INTEGER PRIMARY KEY, event_id INTEGER REFERENCES event(id), field TEXT NOT NULL,
            old_value TEXT, new_value TEXT, detected_at TEXT NOT NULL
        );
        """
    )
    old_conn.execute(
        "INSERT INTO venue (name, name_normalised) VALUES ('Test Venue', 'test venue')"
    )
    old_conn.execute(
        "INSERT INTO event (fingerprint, title, title_normalised, first_seen, last_seen) "
        "VALUES ('fp1', 'Test Event', 'test event', '2026-01-01', '2026-01-01')"
    )
    old_conn.commit()
    old_conn.close()

    init_db(db_path)  # must not raise

    conn = get_connection(db_path)
    try:
        event_columns = {row[1] for row in conn.execute("PRAGMA table_info(event)")}
        venue_columns = {row[1] for row in conn.execute("PRAGMA table_info(venue)")}
        assert "score" not in event_columns
        assert "verdict" not in event_columns
        assert "drive_minutes" not in venue_columns

        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "household" in tables
        assert "household_event_state" in tables

        # Pre-existing data survived the column drops.
        title = conn.execute("SELECT title FROM event WHERE fingerprint = 'fp1'").fetchone()[0]
        assert title == "Test Event"
    finally:
        conn.close()


def test_init_db_drops_unused_digest_day_and_hour_columns(tmp_path):
    """digest_day/digest_hour were added with household in Phase 4, then
    found to be dead (nothing ever read them — cadence belongs to the
    launchd plist, not app config). A database created between that commit
    and this cleanup would still have them; init_db must drop them cleanly.
    """
    db_path = tmp_path / "old.db"
    init_db(db_path)  # current schema — no digest_day/digest_hour to begin with
    conn = sqlite3.connect(db_path)
    conn.execute("ALTER TABLE household ADD COLUMN digest_day TEXT")
    conn.execute("ALTER TABLE household ADD COLUMN digest_hour INTEGER")
    conn.commit()
    conn.close()

    init_db(db_path)  # must not raise, must drop them

    conn = get_connection(db_path)
    try:
        household_columns = {row[1] for row in conn.execute("PRAGMA table_info(household)")}
        assert "digest_day" not in household_columns
        assert "digest_hour" not in household_columns
    finally:
        conn.close()
