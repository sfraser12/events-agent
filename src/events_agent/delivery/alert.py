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

ALERT_WINDOW = timedelta(hours=48)


@dataclass
class AlertItem:
    change_id: int
    event_id: int
    title: str
    venue_name: str | None
    url: str | None
    reason: str


def find_alertable_changes(conn: sqlite3.Connection, now: datetime) -> list[AlertItem]:
    now_iso = now.isoformat()
    cutoff = (now + ALERT_WINDOW).isoformat()
    rows = conn.execute(
        """
        SELECT ec.id, e.id, e.title, v.name, e.url, ec.field, ec.new_value
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
    for change_id, event_id, title, venue_name, url, field, new_value in rows:
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
