from datetime import UTC, datetime

import pytest

from events_agent.db import get_connection, init_db, upsert_raw_event, upsert_venue
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
