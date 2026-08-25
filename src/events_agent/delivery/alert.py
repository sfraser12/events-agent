"""The daily urgent alert (Stage 4, "Deliver").

Fires only for the two things CLAUDE.md calls the highest-value output in the
whole system: an on-sale date landing in the next 48 hours, or a status flip
to low_availability. Both are read off event_change rows, never off the full
event table, and each change is only ever surfaced once (see notified_at).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from events_agent.constraints import event_matches_household

ALERT_WINDOW = timedelta(hours=48)


@dataclass
class AlertItem:
    change_id: int
    event_id: int
    title: str
    venue_name: str | None
    url: str | None
    reason: str


def find_alertable_changes(conn: sqlite3.Connection, now: datetime, household: dict[str, Any]) -> list[AlertItem]:
    """Household-scoped: a change outside this household's radius/price
    ceiling/blackout dates isn't urgent *to them*, regardless of how urgent
    it is in the abstract — otherwise every household would get alerted
    about every other household's events once more than one exists.

    Note: notified_at lives on event_change itself, not per-household — with
    one household that's exactly right; the day a second one exists, two
    households filtering the same still-unnotified change would each see it
    once, which is still correct, but only one of them marking it notified
    would (harmlessly) suppress it for the other too. Fine for now, flagged
    here rather than silently assumed correct.
    """
    now_iso = now.isoformat()
    cutoff = (now + ALERT_WINDOW).isoformat()
    rows = conn.execute(
        """
        SELECT ec.id, e.id, e.title, v.name, e.url, ec.field, ec.new_value,
               v.latitude, v.longitude, e.price_min, e.price_max, e.event_date
        FROM event_change ec
        JOIN event e ON e.id = ec.event_id
        LEFT JOIN venue v ON v.id = e.venue_id
        WHERE ec.notified_at IS NULL
        AND (
            (ec.field = 'status' AND ec.new_value = 'low_availability')
            OR (ec.field = 'on_sale_date' AND ec.new_value IS NOT NULL AND ec.new_value >= ? AND ec.new_value <= ?)
        )
        ORDER BY ec.detected_at
        """,
        (now_iso, cutoff),
    ).fetchall()

    items = []
    for change_id, event_id, title, venue_name, url, field, new_value, venue_lat, venue_lon, price_min, price_max, event_date in rows:
        if not event_matches_household(
            venue_latitude=venue_lat,
            venue_longitude=venue_lon,
            price_min=price_min,
            price_max=price_max,
            event_date=event_date,
            home_latitude=household["home_latitude"],
            home_longitude=household["home_longitude"],
            radius_miles=household["radius_miles"],
            price_ceiling=household["price_ceiling"],
            blackout_dates_json=household["blackout_dates"],
        ):
            continue
        reason = _low_availability_reason() if field == "status" else _on_sale_reason(new_value)
        items.append(AlertItem(change_id, event_id, title, venue_name, url, reason))
    return items


def mark_notified(conn: sqlite3.Connection, change_ids: list[int], now: datetime) -> None:
    conn.executemany(
        "UPDATE event_change SET notified_at = ? WHERE id = ?",
        [(now.isoformat(), change_id) for change_id in change_ids],
    )


def _low_availability_reason() -> str:
    return "selling fast — low availability"


def _on_sale_reason(on_sale_date: str) -> str:
    return f"on sale {datetime.fromisoformat(on_sale_date).strftime('%Y-%m-%d %H:%M')} (within 48h)"
