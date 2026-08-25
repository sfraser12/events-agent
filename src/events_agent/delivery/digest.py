"""Stage 4 — Deliver. The weekly digest: grouped by horizon, built from
household_event_state (score >= threshold, verdict IS NULL, not snoozed),
never from the full event table.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

HORIZONS = ("on sale soon", "this week", "this month", "announced for later")


@dataclass
class DigestEvent:
    event_id: int
    title: str
    venue_name: str | None
    event_date: str | None
    on_sale_date: str | None
    price_min: float | None
    price_max: float | None
    currency: str
    url: str | None
    score: int
    audience: str | None
    reason: str | None
    urgency: str | None


def build_digest(conn: sqlite3.Connection, household: dict[str, Any], today: date | None = None) -> dict[str, list[DigestEvent]]:
    today = today or datetime.now(UTC).date()
    now_iso = datetime.now(UTC).isoformat()

    rows = conn.execute(
        """
        SELECT e.id, e.title, v.name, e.event_date, e.on_sale_date, e.price_min, e.price_max, e.currency,
               e.url, hes.score, hes.audience, hes.score_reason, hes.urgency
        FROM household_event_state hes
        JOIN event e ON e.id = hes.event_id
        LEFT JOIN venue v ON v.id = e.venue_id
        WHERE hes.household_id = ?
          AND hes.score IS NOT NULL AND hes.score >= ?
          AND hes.verdict IS NULL
          AND (hes.snoozed_until IS NULL OR hes.snoozed_until < ?)
        ORDER BY hes.score DESC
        """,
        (household["id"], household["digest_threshold"], now_iso),
    ).fetchall()

    horizons: dict[str, list[DigestEvent]] = {h: [] for h in HORIZONS}
    near_days = household["near_days"] or 7
    month_days = household["month_days"] or 31

    for row in rows:
        event = DigestEvent(*row)
        horizon = _classify_horizon(event.event_date, event.on_sale_date, today, near_days, month_days)
        horizons[horizon].append(event)

    return horizons


def _classify_horizon(event_date: str | None, on_sale_date: str | None, today: date, near_days: int, month_days: int) -> str:
    # An imminent on-sale date is the actionable thing regardless of how far
    # off the event itself is — surfacing it under "this month" alongside
    # unrelated gigs would bury the thing that actually needs a decision now.
    if on_sale_date:
        on_sale_day = datetime.fromisoformat(on_sale_date).date()
        if today <= on_sale_day <= today + _days(month_days):
            return "on sale soon"

    if not event_date:
        return "announced for later"

    event_day = datetime.fromisoformat(event_date).date()
    if event_day <= today + _days(near_days):
        return "this week"
    if event_day <= today + _days(month_days):
        return "this month"
    return "announced for later"


def _days(n: int) -> timedelta:
    return timedelta(days=n)


def build_digest_html(household: dict[str, Any], horizons: dict[str, list[DigestEvent]]) -> str:
    sections = []
    for horizon in HORIZONS:
        events = horizons[horizon]
        if not events:
            continue
        rows_html = "\n".join(_event_row_html(e) for e in events)
        sections.append(f"<h2>{horizon.title()}</h2>\n<table cellpadding='6'>\n{rows_html}\n</table>")

    body = "\n".join(sections) if sections else "<p>Nothing new this week.</p>"
    return f"<html><body><h1>Events digest for {household['label']}</h1>\n{body}</body></html>"


def _event_row_html(event: DigestEvent) -> str:
    price = _format_price(event.price_min, event.price_max, event.currency)
    date_str = event.event_date[:10] if event.event_date else "TBC"
    link = f"<a href='{event.url}'>Book</a>" if event.url else ""
    return (
        "<tr>"
        f"<td>{date_str}</td><td>{event.title}</td><td>{event.venue_name or ''}</td>"
        f"<td>{price}</td><td>{event.score}</td><td>{event.reason or ''}</td><td>{link}</td>"
        "</tr>"
    )


def build_digest_plain(household: dict[str, Any], horizons: dict[str, list[DigestEvent]]) -> str:
    lines = [f"Events digest for {household['label']}", ""]
    any_events = False
    for horizon in HORIZONS:
        events = horizons[horizon]
        if not events:
            continue
        any_events = True
        lines.append(horizon.upper())
        for event in events:
            price = _format_price(event.price_min, event.price_max, event.currency)
            date_str = event.event_date[:10] if event.event_date else "TBC"
            lines.append(f"- {date_str}  {event.title} @ {event.venue_name or '?'}  {price}  (score {event.score})")
            if event.reason:
                lines.append(f"    {event.reason}")
            if event.url:
                lines.append(f"    {event.url}")
        lines.append("")
    if not any_events:
        lines.append("Nothing new this week.")
    return "\n".join(lines)


def _format_price(price_min: float | None, price_max: float | None, currency: str) -> str:
    if price_min is None and price_max is None:
        return "price TBC"
    if price_min == price_max:
        return f"{currency} {price_min:.2f}"
    return f"{currency} {price_min:.2f}-{price_max:.2f}"
