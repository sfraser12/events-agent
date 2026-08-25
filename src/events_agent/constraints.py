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
) -> bool:
    """True if the event clears every hard constraint for this household.

    Missing data never excludes an event — an event with no venue
    coordinates or no price has nothing to check against, so it passes
    through to the model rather than being silently dropped by a filter that
    can't evaluate it. Same principle as elsewhere in the codebase: an
    unknown price is treated as unknown, not as free or as over-budget.
    """
    if venue_latitude is not None and venue_longitude is not None:
        distance_miles = haversine_meters(home_latitude, home_longitude, venue_latitude, venue_longitude) * (
            MILES_PER_METER
        )
        if distance_miles > radius_miles:
            return False

    if price_ceiling is not None and price_min is not None and price_min > price_ceiling:
        return False

    if event_date and blackout_dates_json:
        event_day = datetime.fromisoformat(event_date).date()
        for start_iso, end_iso in json.loads(blackout_dates_json):
            if date.fromisoformat(start_iso) <= event_day <= date.fromisoformat(end_iso):
                return False

    return True


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
