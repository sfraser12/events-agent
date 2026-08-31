from datetime import UTC, date, datetime, timedelta

import pytest

from events_agent.db import (
    get_connection,
    get_household_as_dict,
    init_db,
    mark_surfaced,
    set_verdict,
    upsert_household,
    upsert_raw_event,
    upsert_score,
)
from events_agent.annual_anchors import AnnualAnchor
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


def test_never_surfaced_event_is_flagged_new(conn, household):
    event_id, _ = upsert_raw_event(conn, make_raw_event())
    score_event(conn, household["id"], event_id, 80)
    conn.commit()

    horizons = build_digest(conn, household)

    all_events = [e for events in horizons.values() for e in events]
    assert all_events[0].is_new is True


def test_previously_surfaced_event_is_not_flagged_new(conn, household):
    event_id, _ = upsert_raw_event(conn, make_raw_event())
    score_event(conn, household["id"], event_id, 80)
    mark_surfaced(conn, household["id"], [event_id], datetime.now(UTC).isoformat())
    conn.commit()

    horizons = build_digest(conn, household)

    all_events = [e for events in horizons.values() for e in events]
    assert all_events[0].is_new is False


def test_resolved_duplicate_only_surfaces_the_plain_listing(conn, household):
    # The premium/VIP variant is suppressed regardless of which side got the
    # lower event_id -- see dedupe.get_suppressed_duplicate_ids.
    lower_id, _ = upsert_raw_event(conn, make_raw_event(source_event_id="1", title="Duran Duran"))
    higher_id, _ = upsert_raw_event(
        conn, make_raw_event(source_event_id="2", title="Venue Premium - Duran Duran")
    )
    assert lower_id < higher_id
    score_event(conn, household["id"], lower_id, 80)
    score_event(conn, household["id"], higher_id, 80)
    conn.execute(
        "INSERT INTO duplicate_candidate (event_id_a, event_id_b, similarity, resolution) VALUES (?, ?, 1.0, 'same')",
        (lower_id, higher_id),
    )
    conn.commit()

    horizons = build_digest(conn, household)

    all_ids = {e.event_id for events in horizons.values() for e in events}
    assert all_ids == {lower_id}


def test_resolved_duplicate_suppresses_premium_even_with_the_lower_id(conn, household):
    # Real production case (2026-08-31): Ticketmaster gave "Venue Premium -
    # Fontaines D.C." a lower id than the plain "Fontaines D.C." listing --
    # an id-order suppression rule kept the pricier one and hid the plain
    # one. Must keep the plain listing regardless of id order.
    premium_id, _ = upsert_raw_event(
        conn, make_raw_event(source_event_id="1", title="Venue Premium - Duran Duran")
    )
    plain_id, _ = upsert_raw_event(conn, make_raw_event(source_event_id="2", title="Duran Duran"))
    assert premium_id < plain_id
    score_event(conn, household["id"], premium_id, 80)
    score_event(conn, household["id"], plain_id, 80)
    conn.execute(
        "INSERT INTO duplicate_candidate (event_id_a, event_id_b, similarity, resolution) VALUES (?, ?, 1.0, 'same')",
        (premium_id, plain_id),
    )
    conn.commit()

    horizons = build_digest(conn, household)

    all_ids = {e.event_id for events in horizons.values() for e in events}
    assert all_ids == {plain_id}


def test_unresolved_duplicate_candidate_still_shows_both(conn, household):
    lower_id, _ = upsert_raw_event(conn, make_raw_event(source_event_id="1", title="Duran Duran"))
    higher_id, _ = upsert_raw_event(
        conn, make_raw_event(source_event_id="2", title="Venue Premium - Duran Duran")
    )
    score_event(conn, household["id"], lower_id, 80)
    score_event(conn, household["id"], higher_id, 80)
    conn.execute(
        "INSERT INTO duplicate_candidate (event_id_a, event_id_b, similarity, resolution) VALUES (?, ?, 1.0, NULL)",
        (lower_id, higher_id),
    )
    conn.commit()

    horizons = build_digest(conn, household)

    all_ids = {e.event_id for events in horizons.values() for e in events}
    assert all_ids == {lower_id, higher_id}


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


def test_past_event_is_excluded_even_if_still_scored_high(conn, household):
    # Regression: an event scored while upcoming, then left with no verdict,
    # used to keep reappearing in every digest forever after its date
    # passed -- misclassified as "this week" because _classify_horizon had
    # no lower bound (any past event_date is trivially <= today + near_days).
    today = datetime.now(UTC)
    event_id, _ = upsert_raw_event(
        conn,
        make_raw_event(
            title="Last week's gig",
            event_date=today - timedelta(days=5),
            status="past",
        ),
    )
    score_event(conn, household["id"], event_id, 90)
    conn.commit()

    horizons = build_digest(conn, household, today=today.date())

    assert sum(len(e) for e in horizons.values()) == 0
    assert horizons["this week"] == []


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
    assert "score 80" in html
    assert "https://example.com/karine" in html  # the booking link


def test_build_digest_html_shows_event_id_for_marking_verdicts(conn, household):
    # The digest is the only place you'd learn an event's id — without it
    # shown here, `events-agent verdict <id> ...` has nothing to work from.
    event_id, _ = upsert_raw_event(conn, make_raw_event())
    score_event(conn, household["id"], event_id, 80)
    conn.commit()

    horizons = build_digest(conn, household)
    html = build_digest_html(household, horizons)
    plain = build_digest_plain(household, horizons)

    assert f"id {event_id}" in html
    assert f"id {event_id}" in plain


def test_build_digest_html_escapes_special_characters_in_title(conn, household):
    event_id, _ = upsert_raw_event(conn, make_raw_event(title="Rock & Roll <Live>"))
    score_event(conn, household["id"], event_id, 80)
    conn.commit()

    horizons = build_digest(conn, household)
    html = build_digest_html(household, horizons)

    assert "Rock &amp; Roll &lt;Live&gt;" in html
    assert "<Live>" not in html


def test_build_digest_html_omits_book_link_when_no_url(conn, household):
    event_id, _ = upsert_raw_event(conn, make_raw_event(url=None))
    score_event(conn, household["id"], event_id, 80)
    conn.commit()

    horizons = build_digest(conn, household)
    html = build_digest_html(household, horizons)

    assert "Book" not in html


def test_build_digest_plain_says_nothing_new_when_empty(household):
    empty_horizons = {"on sale soon": [], "this week": [], "this month": [], "announced for later": []}
    plain = build_digest_plain(household, empty_horizons)

    assert "Nothing new this week." in plain


def test_annual_anchor_reminder_appears_in_html_and_plain(household):
    empty_horizons = {"on sale soon": [], "this week": [], "this month": [], "announced for later": []}
    anchor = AnnualAnchor(name="Celtic Connections", typical_month="january", programme_announced="october")

    html_out = build_digest_html(household, empty_horizons, reminders=[anchor])
    plain_out = build_digest_plain(household, empty_horizons, reminders=[anchor])

    assert "Celtic Connections" in html_out
    assert "Celtic Connections" in plain_out


def test_no_reminders_means_no_reminder_text(household):
    empty_horizons = {"on sale soon": [], "this week": [], "this month": [], "announced for later": []}

    html_out = build_digest_html(household, empty_horizons, reminders=[])
    plain_out = build_digest_plain(household, empty_horizons, reminders=[])

    assert "Annual anchor" not in html_out
    assert "ANNUAL ANCHOR" not in plain_out


SKYE_LAT, SKYE_LON = 57.4126, -6.1953  # Portree, ~170 miles from Milngavie


@pytest.fixture
def household_with_far_tier(conn, tmp_path):
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
        far_radius_miles=200,
        far_threshold=85,
    )
    conn.commit()
    return get_household_as_dict(conn, 1)


def test_far_flung_event_above_far_threshold_appears_tagged(conn, household_with_far_tier):
    event_id, _ = upsert_raw_event(
        conn, make_raw_event(venue_name="An Lanntair", venue_latitude=SKYE_LAT, venue_longitude=SKYE_LON)
    )
    score_event(conn, household_with_far_tier["id"], event_id, 90)  # clears far_threshold=85
    conn.commit()

    horizons = build_digest(conn, household_with_far_tier)
    all_events = [e for events in horizons.values() for e in events]

    assert len(all_events) == 1
    assert all_events[0].far_flung is True
    assert all_events[0].drive_minutes is not None and all_events[0].drive_minutes > 0


def test_far_flung_event_between_digest_and_far_threshold_is_excluded(conn, household_with_far_tier):
    # Clears digest_threshold (60) but not the stricter far_threshold (85) --
    # this is the whole point of the tier: routine-good isn't enough when
    # it's three hours away.
    event_id, _ = upsert_raw_event(
        conn, make_raw_event(venue_name="An Lanntair", venue_latitude=SKYE_LAT, venue_longitude=SKYE_LON)
    )
    score_event(conn, household_with_far_tier["id"], event_id, 70)
    conn.commit()

    horizons = build_digest(conn, household_with_far_tier)

    assert sum(len(e) for e in horizons.values()) == 0


def test_local_event_unaffected_by_far_tier_configuration(conn, household_with_far_tier):
    # A household with the far tier configured should still show ordinary
    # local events exactly as before -- far_flung=False, digest_threshold
    # still the only bar that applies.
    event_id, _ = upsert_raw_event(conn, make_raw_event())  # Oran Mor, well within radius_miles=25
    score_event(conn, household_with_far_tier["id"], event_id, 70)
    conn.commit()

    horizons = build_digest(conn, household_with_far_tier)
    all_events = [e for events in horizons.values() for e in events]

    assert len(all_events) == 1
    assert all_events[0].far_flung is False
    assert all_events[0].drive_minutes is None
