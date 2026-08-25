import json

from events_agent.constraints import estimate_drive_minutes, event_matches_household

HOME_LAT, HOME_LON = 55.9410, -4.3170  # Milngavie
HYDRO_LAT, HYDRO_LON = 55.859881, -4.285367  # ~5 miles from home
BARROWLAND_LAT, BARROWLAND_LON = 55.8550553, -4.2369184  # further out


def make_household(**overrides) -> dict:
    defaults = dict(
        home_latitude=HOME_LAT,
        home_longitude=HOME_LON,
        radius_miles=25,
        price_ceiling=200,
        blackout_dates_json=None,
    )
    defaults.update(overrides)
    return defaults


def test_event_within_radius_and_price_passes():
    assert event_matches_household(
        venue_latitude=HYDRO_LAT,
        venue_longitude=HYDRO_LON,
        price_min=50,
        price_max=80,
        event_date="2026-09-01T19:00:00+00:00",
        **make_household(),
    )


def test_event_outside_radius_is_excluded():
    assert not event_matches_household(
        venue_latitude=HYDRO_LAT,
        venue_longitude=HYDRO_LON,
        price_min=50,
        price_max=80,
        event_date="2026-09-01T19:00:00+00:00",
        **make_household(radius_miles=1),
    )


def test_event_above_price_ceiling_is_excluded():
    assert not event_matches_household(
        venue_latitude=HYDRO_LAT,
        venue_longitude=HYDRO_LON,
        price_min=250,
        price_max=300,
        event_date="2026-09-01T19:00:00+00:00",
        **make_household(price_ceiling=200),
    )


def test_event_with_unknown_price_is_not_excluded():
    # Missing data never excludes — an unknown price isn't "over budget",
    # it's unknown, same principle as elsewhere in the pipeline.
    assert event_matches_household(
        venue_latitude=HYDRO_LAT,
        venue_longitude=HYDRO_LON,
        price_min=None,
        price_max=None,
        event_date="2026-09-01T19:00:00+00:00",
        **make_household(price_ceiling=50),
    )


def test_event_with_no_venue_coordinates_is_not_excluded_by_radius():
    assert event_matches_household(
        venue_latitude=None,
        venue_longitude=None,
        price_min=50,
        price_max=50,
        event_date="2026-09-01T19:00:00+00:00",
        **make_household(radius_miles=1),
    )


def test_event_inside_blackout_range_is_excluded():
    blackout = json.dumps([["2026-10-12", "2026-10-26"]])
    assert not event_matches_household(
        venue_latitude=HYDRO_LAT,
        venue_longitude=HYDRO_LON,
        price_min=50,
        price_max=50,
        event_date="2026-10-20T19:00:00+00:00",
        **make_household(blackout_dates_json=blackout),
    )


def test_event_outside_blackout_range_is_not_excluded():
    blackout = json.dumps([["2026-10-12", "2026-10-26"]])
    assert event_matches_household(
        venue_latitude=HYDRO_LAT,
        venue_longitude=HYDRO_LON,
        price_min=50,
        price_max=50,
        event_date="2026-11-01T19:00:00+00:00",
        **make_household(blackout_dates_json=blackout),
    )


def test_undated_event_is_not_excluded_by_blackout():
    blackout = json.dumps([["2026-10-12", "2026-10-26"]])
    assert event_matches_household(
        venue_latitude=HYDRO_LAT,
        venue_longitude=HYDRO_LON,
        price_min=50,
        price_max=50,
        event_date=None,
        **make_household(blackout_dates_json=blackout),
    )


def test_estimate_drive_minutes_is_a_positive_int_for_a_real_distance():
    minutes = estimate_drive_minutes(HOME_LAT, HOME_LON, BARROWLAND_LAT, BARROWLAND_LON)
    assert isinstance(minutes, int)
    assert 0 < minutes < 60  # ~2.5 miles at an approximated 40mph, sanity bound
