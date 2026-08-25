"""Stage 4 — Deliver. Plain .ics export of shortlisted events (verdict
interested or booked), per CLAUDE.md's Phase 5 spec. A static file rather
than a Google Calendar push — either household member can import it into
whatever calendar app they already use, with no OAuth flow to set up or
maintain in a cron job.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

DEFAULT_DURATION = timedelta(hours=2)
FOLD_WIDTH = 75  # RFC 5545 line length limit, in octets, before continuation


@dataclass
class CalendarEvent:
    event_id: int
    title: str
    venue_name: str | None
    event_date: str
    event_date_end: str | None
    url: str | None
    verdict: str


def select_calendar_events(conn: sqlite3.Connection, household: dict[str, Any]) -> list[CalendarEvent]:
    """Undated events (announced but no event_date yet) can't go on a
    calendar — excluded here rather than left for the caller to filter."""
    rows = conn.execute(
        """
        SELECT e.id, e.title, v.name, e.event_date, e.event_date_end, e.url, hes.verdict
        FROM household_event_state hes
        JOIN event e ON e.id = hes.event_id
        LEFT JOIN venue v ON v.id = e.venue_id
        WHERE hes.household_id = ?
          AND hes.verdict IN ('interested', 'booked')
          AND e.event_date IS NOT NULL
        ORDER BY e.event_date
        """,
        (household["id"],),
    ).fetchall()
    return [CalendarEvent(*row) for row in rows]


def build_ics(household: dict[str, Any], events: list[CalendarEvent]) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Marquee//events-agent//EN",
        "CALSCALE:GREGORIAN",
        f"X-WR-CALNAME:Marquee \u2014 {household['label']}",
    ]
    for event in events:
        lines.extend(_vevent(event))
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


def _vevent(event: CalendarEvent) -> list[str]:
    start = datetime.fromisoformat(event.event_date)
    end = datetime.fromisoformat(event.event_date_end) if event.event_date_end else start + DEFAULT_DURATION
    provisional = event.verdict == "interested"

    lines = [
        "BEGIN:VEVENT",
        f"UID:events-agent-{event.event_id}@marquee.local",
        f"DTSTAMP:{_fmt(datetime.now(UTC))}",
        f"DTSTART:{_fmt(start)}",
        f"DTEND:{_fmt(end)}",
        f"SUMMARY:{_escape(event.title)}",
        f"STATUS:{'TENTATIVE' if provisional else 'CONFIRMED'}",
        f"DESCRIPTION:{_escape('Provisional \u2014 not yet booked.' if provisional else 'Booked.')}",
    ]
    if event.venue_name:
        lines.append(f"LOCATION:{_escape(event.venue_name)}")
    if event.url:
        lines.append(f"URL:{_escape(event.url)}")
    lines.append("END:VEVENT")
    return lines


def _fmt(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> str:
    encoded = line.encode("utf-8")
    if len(encoded) <= FOLD_WIDTH:
        return line

    chunks = []
    start = 0
    limit = FOLD_WIDTH
    while start < len(encoded):
        end = min(start + limit, len(encoded))
        # back off if `end` lands mid-character (on a UTF-8 continuation byte)
        while end < len(encoded) and (encoded[end] & 0xC0) == 0x80:
            end -= 1
        chunks.append(encoded[start:end].decode("utf-8"))
        start = end
        limit = FOLD_WIDTH - 1  # continuation lines carry a leading space
    return "\r\n ".join(chunks)
