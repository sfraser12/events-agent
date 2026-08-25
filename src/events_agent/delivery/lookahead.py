"""Stage 4 — Deliver. The fortnight look-ahead: a safety net, separate from
the weekly digest, for events happening soon that scored BELOW the digest
threshold and so never appeared in a digest at all. The digest's score bar
is permanent regardless of how soon the event is — a moderate match six
months out and the same match next week are treated identically — so a
below-threshold event with an imminent date could otherwise expire
completely unseen. This deliberately sits between alert_threshold and
digest_threshold: anything scoring at or above digest_threshold is already
covered by the digest and would just be noise here; anything below
alert_threshold is a genuinely poor fit, not a near-miss worth a second look.
"""

from __future__ import annotations

import html
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from events_agent.delivery.email_design import (
    BORDER,
    INK,
    LOOKAHEAD,
    LOOKAHEAD_BG,
    MUTED,
    cta_cell,
    empty_row,
    format_price,
    shell,
)

LOOKAHEAD_DAYS = 14


@dataclass
class LookaheadEvent:
    event_id: int
    title: str
    venue_name: str | None
    event_date: str
    price_min: float | None
    price_max: float | None
    currency: str
    url: str | None
    score: int | None
    reason: str | None


def select_lookahead_events(
    conn: sqlite3.Connection, household: dict[str, Any], today: date | None = None
) -> list[LookaheadEvent]:
    today = today or datetime.now(UTC).date()
    window_end = today + timedelta(days=LOOKAHEAD_DAYS)
    now_iso = datetime.now(UTC).isoformat()

    rows = conn.execute(
        """
        SELECT e.id, e.title, v.name, e.event_date, e.price_min, e.price_max, e.currency,
               e.url, hes.score, hes.score_reason
        FROM household_event_state hes
        JOIN event e ON e.id = hes.event_id
        LEFT JOIN venue v ON v.id = e.venue_id
        WHERE hes.household_id = ?
          AND hes.verdict IS NULL
          AND (hes.snoozed_until IS NULL OR hes.snoozed_until < ?)
          AND e.event_date IS NOT NULL
          AND (hes.score IS NULL OR (hes.score >= ? AND hes.score < ?))
        """,
        (household["id"], now_iso, household["alert_threshold"], household["digest_threshold"]),
    ).fetchall()

    events = [LookaheadEvent(*row) for row in rows]
    events = [e for e in events if today <= datetime.fromisoformat(e.event_date).date() <= window_end]
    events.sort(key=lambda e: e.event_date)
    return events


def build_lookahead_html(household: dict[str, Any], events: list[LookaheadEvent], today: date | None = None) -> str:
    today = today or datetime.now(UTC).date()
    window_end = today + timedelta(days=LOOKAHEAD_DAYS)
    plural = "" if len(events) == 1 else "s"
    subtitle = (
        f"For {html.escape(household['label'])} &middot; {len(events)} thing{plural} in the next fortnight "
        f"that didn't make the weekly digest &middot; {today.strftime('%-d %b')}&ndash;{window_end.strftime('%-d %b')}"
    )
    rows = "".join(_lookahead_card_html(e) for e in events) if events else empty_row(
        "Nothing in the next fortnight that the weekly digest hasn't already caught."
    )
    footer = (
        "A second look at anything scoring below the weekly digest's bar but happening within 14 days &mdash; "
        "so a moderate match doesn't quietly expire unseen just because its event date crept up."
    )
    return shell(
        eyebrow="Next fortnight",
        eyebrow_color=LOOKAHEAD,
        eyebrow_bg=LOOKAHEAD_BG,
        subtitle=subtitle,
        body_rows=rows,
        footer=footer,
    )


def _lookahead_card_html(event: LookaheadEvent) -> str:
    price = format_price(event.price_min, event.price_max, event.currency)
    date_str = datetime.fromisoformat(event.event_date).strftime("%a %-d %b")
    venue = html.escape(event.venue_name) if event.venue_name else "venue TBC"
    title = html.escape(event.title)
    reason = (
        f'<div style="font-size:13px; color:#3E453F; font-style:italic; margin:2px 0 6px;">{html.escape(event.reason)}</div>'
        if event.reason
        else ""
    )
    score_badge = (
        f'<span style="background:{LOOKAHEAD_BG}; color:{LOOKAHEAD}; padding:2px 8px; border-radius:10px; font-weight:600;">'
        f"score {event.score}</span> &nbsp;&middot;&nbsp; "
        if event.score is not None
        else ""
    )

    return f"""\
    <tr>
      <td style="padding:14px 32px; border-bottom:1px solid {BORDER};">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td style="vertical-align:top;">
              <div style="font-size:12px; color:{MUTED};">{date_str} &middot; {venue}</div>
              <div style="font-size:16px; font-weight:700; color:{INK}; margin:2px 0 2px;">{title}</div>
              {reason}
              <div style="font-size:12px; color:{MUTED};">{price} &nbsp;&middot;&nbsp; \
{score_badge}<code style="color:{MUTED};">id {event.event_id}</code></div>
            </td>
{cta_cell(event.url)}
          </tr>
        </table>
      </td>
    </tr>"""


def build_lookahead_plain(household: dict[str, Any], events: list[LookaheadEvent], today: date | None = None) -> str:
    lines = [f"MARQUEE — next fortnight for {household['label']}", ""]
    if not events:
        lines.append("Nothing in the next fortnight that the weekly digest hasn't already caught.")
        return "\n".join(lines)

    for event in events:
        price = format_price(event.price_min, event.price_max, event.currency)
        date_str = event.event_date[:10]
        score_str = f", score {event.score}" if event.score is not None else ""
        lines.append(f"- {date_str}  {event.title} @ {event.venue_name or '?'}  {price}  (id {event.event_id}{score_str})")
        if event.reason:
            lines.append(f"    {event.reason}")
        if event.url:
            lines.append(f"    {event.url}")
        lines.append("")
    return "\n".join(lines)
