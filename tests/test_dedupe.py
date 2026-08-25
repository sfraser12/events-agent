from datetime import UTC, datetime

import pytest

from events_agent.db import get_connection, init_db, upsert_raw_event
from events_agent.dedupe import find_and_flag_candidates
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
        source_name="ticketmaster",
        source_event_id="tm-1",
        title="AEW: Dynamite & Collision",
        category="other",
        venue_name="OVO Hydro",
        venue_city="Glasgow",
        venue_latitude=55.859881,
        venue_longitude=-4.285367,
        event_date=datetime(2027, 5, 15, 19, 0, tzinfo=UTC),
        status="on_sale",
    )
    defaults.update(overrides)
    return RawEvent(**defaults)


def test_near_miss_same_venue_date_similar_title_flags_a_candidate(conn):
    id_a, _ = upsert_raw_event(conn, make_raw_event(source_event_id="1", title="AEW: Dynamite & Collision"))
    id_b, _ = upsert_raw_event(
        conn, make_raw_event(source_event_id="2", title="Venue Premium - AEW: Dynamite & Collision")
    )

    row = conn.execute(
        "SELECT event_id_a, event_id_b, resolution FROM duplicate_candidate"
    ).fetchone()
    assert row == (min(id_a, id_b), max(id_a, id_b), None)


def test_dissimilar_titles_same_venue_date_are_not_flagged(conn):
    upsert_raw_event(conn, make_raw_event(source_event_id="1", title="AEW: Dynamite & Collision"))
    upsert_raw_event(conn, make_raw_event(source_event_id="2", title="Scottish Ballet: Swan Lake"))

    count = conn.execute("SELECT COUNT(*) FROM duplicate_candidate").fetchone()[0]
    assert count == 0


def test_exact_fingerprint_match_is_not_flagged_as_a_near_miss(conn):
    # Same title/venue/date across two sources merges via the exact fingerprint
    # path (Phase 1 behavior) — never even reaches the near-miss comparison.
    upsert_raw_event(conn, make_raw_event(source_name="skiddle", source_event_id="1"))
    upsert_raw_event(conn, make_raw_event(source_name="ticketmaster", source_event_id="2"))

    event_count = conn.execute("SELECT COUNT(*) FROM event").fetchone()[0]
    candidate_count = conn.execute("SELECT COUNT(*) FROM duplicate_candidate").fetchone()[0]
    assert event_count == 1
    assert candidate_count == 0


def test_different_venue_does_not_flag_even_with_identical_title_and_date(conn):
    # Genuinely distant coordinates (real Barrowland pin, ~2.5km from the
    # Hydro) — otherwise db.py's venue proximity fallback (Phase 2's venue
    # resolution feature) would correctly merge these into one venue row,
    # which is a different code path than what this test is checking.
    upsert_raw_event(conn, make_raw_event(source_event_id="1", venue_name="OVO Hydro"))
    upsert_raw_event(
        conn,
        make_raw_event(
            source_event_id="2",
            venue_name="Barrowland Ballroom",
            venue_latitude=55.8550553,
            venue_longitude=-4.2369184,
        ),
    )

    count = conn.execute("SELECT COUNT(*) FROM duplicate_candidate").fetchone()[0]
    assert count == 0


def test_reflagging_the_same_pair_does_not_duplicate_the_row(conn):
    # upsert_raw_event only calls find_and_flag_candidates once, on an event's
    # creation — this directly exercises _flag_pair's own existence check,
    # which is the actual idempotency guard, in case anything ever re-triggers
    # a comparison for an event that's already been flagged once.
    id_a, _ = upsert_raw_event(conn, make_raw_event(source_event_id="1", title="AEW: Dynamite & Collision"))
    id_b, _ = upsert_raw_event(
        conn, make_raw_event(source_event_id="2", title="Venue Premium - AEW: Dynamite & Collision")
    )

    row_before = conn.execute("SELECT id FROM duplicate_candidate").fetchone()

    find_and_flag_candidates(conn, id_b)

    rows_after = conn.execute("SELECT id FROM duplicate_candidate").fetchall()
    assert rows_after == [row_before]
