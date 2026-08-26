"""Hard constraints, applied in Python before an event ever reaches the LLM.

Per CLAUDE.md: "Hard constraints (radius, budget ceiling, blackout dates) are
applied in Python before the events ever reach the model. Never rely on the
prompt to enforce a rule you can enforce in code." Radius was previously
"enforced" only by baking it into each source adapter's fetch-time API call —
fine for one household, wrong once a shared, wider-than-any-one-household
catalog is scored per household (see household_event_state). price_ceiling
and blackout_dates were never enforced anywhere until this module.
"""

from __future__ import annotations

import json
from datetime import date, datetime

from events_agent.db import haversine_meters

MILES_PER_METER = 1 / 1609.344


def event_matches_household(
    *,
    venue_latitude: float | None,
    venue_longitude: float | None,
    price_min: float | None,
    price_max: float | None,
    event_date: str | None,
    home_latitude: float,
    home_longitude: float,
    radius_miles: float,
    price_ceiling: float | None,
    blackout_dates_json: str | None,
    far_radius_miles: float | None = None,
    far_min_latitude: float | None = None,
) -> bool:
    """True if the event clears every hard constraint for this household.

    Missing data never excludes an event — an event with no venue
    coordinates or no price has nothing to check against, so it passes
    through to the model rather than being silently dropped by a filter that
    can't evaluate it. Same principle as elsewhere in the codebase: an
    unknown price is treated as unknown, not as free or as over-budget.

    far_radius_miles (optional, "worth a special trip" tier): when set, an
    event beyond radius_miles is no longer a hard reject as long as it's
    within far_radius_miles — it still reaches scoring. The tighter bar for
    those events (far_threshold instead of digest_threshold) is applied at
    digest-selection time, not here — this function only decides whether an
    event is even worth scoring, never how good a score it needs. See
    is_far_flung() and delivery/digest.py.

    far_min_latitude (optional): a plain circle is the wrong shape for "the
    rest of Scotland" — confirmed against real data, a 200mi circle around
    Milngavie is ~70% Blackpool/Manchester/Scarborough/Belfast (the last of
    which isn't even reachable by road), not Highlands/Skye/Argyll & Bute,
    because Scotland's north-south extent puts places like Skye roughly as
    far from Glasgow as North West England is. far_min_latitude is a rough
    "north of about here" floor (Scott's config uses 55.0, just north of
    Carlisle/the England border) applied only within the far-flung branch,
    to keep the wider net pointed at Scotland instead of a literal circle.
    It's an approximation, not a real border lookup — a handful of
    borderline towns either way is an acceptable trade for not needing a
    geocoding/region API.
    """
    if venue_latitude is not None and venue_longitude is not None:
        distance_miles = haversine_meters(home_latitude, home_longitude, venue_latitude, venue_longitude) * (
            MILES_PER_METER
        )
        if distance_miles > radius_miles:
            far_tier_applies = (
                far_radius_miles is not None
                and distance_miles <= far_radius_miles
                and (far_min_latitude is None or venue_latitude >= far_min_latitude)
            )
            if not far_tier_applies:
                return False

    if price_ceiling is not None and price_min is not None and price_min > price_ceiling:
        return False

    if event_date and blackout_dates_json:
        event_day = datetime.fromisoformat(event_date).date()
        for start_iso, end_iso in json.loads(blackout_dates_json):
            if date.fromisoformat(start_iso) <= event_day <= date.fromisoformat(end_iso):
                return False

    return True


def is_far_flung(
    *,
    venue_latitude: float | None,
    venue_longitude: float | None,
    home_latitude: float,
    home_longitude: float,
    radius_miles: float,
) -> bool:
    """True if the event is outside the household's normal radius_miles —
    meaning it only got this far because far_radius_miles let it through,
    and it needs to clear far_threshold rather than digest_threshold before
    it can surface. Missing coordinates are never far-flung: there's no
    distance to compare, and event_matches_household already let them
    through unconditionally for the same reason."""
    if venue_latitude is None or venue_longitude is None:
        return False
    distance_miles = haversine_meters(home_latitude, home_longitude, venue_latitude, venue_longitude) * (
        MILES_PER_METER
    )
    return distance_miles > radius_miles


def estimate_drive_minutes(
    home_latitude: float, home_longitude: float, venue_latitude: float, venue_longitude: float
) -> int:
    """Straight-line-distance approximation, not a real routing estimate —
    a placeholder until/unless a routing API is worth adding. ~40mph average
    to account for the mix of motorway and town-center driving in this area."""
    distance_miles = haversine_meters(home_latitude, home_longitude, venue_latitude, venue_longitude) * (
        MILES_PER_METER
    )
    average_mph = 40
    return round(distance_miles / average_mph * 60)
