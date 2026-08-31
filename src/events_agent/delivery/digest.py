"""Stage 4 — Deliver. The weekly digest: grouped by horizon, built from
household_event_state (score >= threshold, verdict IS NULL, not snoozed),
never from the full event table.
"""

from __future__ import annotations

import html
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from events_agent.annual_anchors import AnnualAnchor
from events_agent.constraints import estimate_drive_minutes, is_far_flung
from events_agent.dedupe import get_suppressed_duplicate_ids
from events_agent.delivery.email_design import (
    ACCENT,
    ACCENT_BG,
    BORDER,
    FARFLUNG,
    FARFLUNG_BG,
    INK,
    MUTED,
    NEW,
    NEW_BG,
    SERIF,
    WARN,
    WARN_BG,
    cta_cell,
    empty_row,
    format_price,
    shell,
)

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
    far_flung: bool = False
    drive_minutes: int | None = None  # only set when far_flung — see build_digest
    is_new: bool = False  # never surfaced in a previous digest — see build_digest


def build_digest(conn: sqlite3.Connection, household: dict[str, Any], today: date | None = None) -> dict[str, list[DigestEvent]]:
    today = today or datetime.now(UTC).date()
    now_iso = datetime.now(UTC).isoformat()

    # score >= digest_threshold is deliberately the only SQL-level bar, even
    # though far-flung events need to clear the stricter far_threshold too —
    # far_threshold is always >= digest_threshold (see config.py), so
    # anything that could pass far_threshold already passes this query;
    # far-flung events that clear digest_threshold but not far_threshold get
    # filtered out below, in Python, where far_flung can actually be
    # computed (it needs venue coordinates, not just a column comparison).
    #
    # A resolved-same duplicate_candidate is a catalog fact (same real event,
    # e.g. Ticketmaster's own "X" / "Venue Premium - X" split), not a
    # household judgment — see dedupe.get_suppressed_duplicate_ids for which
    # side gets hidden and why (not simply "the higher id": confirmed live
    # that Ticketmaster doesn't consistently assign the plain listing the
    # lower id, so an id-order rule can keep the pricier upsell instead of
    # the listing actually worth booking).
    suppressed_ids = get_suppressed_duplicate_ids(conn)

    rows = conn.execute(
        """
        SELECT e.id, e.title, v.name, e.event_date, e.on_sale_date, e.price_min, e.price_max, e.currency,
               e.url, hes.score, hes.audience, hes.score_reason, hes.urgency, v.latitude, v.longitude,
               hes.surfaced_at
        FROM household_event_state hes
        JOIN event e ON e.id = hes.event_id
        LEFT JOIN venue v ON v.id = e.venue_id
        WHERE hes.household_id = ?
          AND hes.score IS NOT NULL AND hes.score >= ?
          AND hes.verdict IS NULL
          AND (hes.snoozed_until IS NULL OR hes.snoozed_until < ?)
          AND e.status NOT IN ('past', 'cancelled')
        ORDER BY hes.score DESC
        """,
        (household["id"], household["digest_threshold"], now_iso),
    ).fetchall()

    horizons: dict[str, list[DigestEvent]] = {h: [] for h in HORIZONS}
    near_days = household["near_days"] or 7
    month_days = household["month_days"] or 31

    for row in rows:
        (event_id, title, venue_name, event_date, on_sale_date, price_min, price_max, currency,
         url, score, audience, reason, urgency, venue_latitude, venue_longitude, surfaced_at) = row

        if event_id in suppressed_ids:
            continue

        far_flung = is_far_flung(
            venue_latitude=venue_latitude,
            venue_longitude=venue_longitude,
            home_latitude=household["home_latitude"],
            home_longitude=household["home_longitude"],
            radius_miles=household["radius_miles"],
        )
        drive_minutes = None
        if far_flung:
            far_threshold = household["far_threshold"]
            if far_threshold is None or score < far_threshold:
                continue  # doesn't clear the stricter "worth the trip" bar
            drive_minutes = estimate_drive_minutes(
                household["home_latitude"], household["home_longitude"], venue_latitude, venue_longitude
            )

        event = DigestEvent(
            event_id, title, venue_name, event_date, on_sale_date, price_min, price_max, currency,
            url, score, audience, reason, urgency, far_flung, drive_minutes, is_new=surfaced_at is None,
        )
        horizon = _classify_horizon(event.event_date, event.on_sale_date, today, near_days, month_days)
        horizons[horizon].append(event)

    # The SQL query above orders by score DESC (so ties and downstream
    # processing are deterministic), but that order leaking straight into
    # the rendered list meant a horizon spanning several months -- most
    # visibly "announced for later" -- showed dates in no order at all,
    # e.g. Oct/Nov/Dec/Mar all interleaved purely by score. Sort each
    # horizon chronologically instead: "on sale soon" by on_sale_date (the
    # actual reason it's urgent), everything else by event_date, undated
    # events last since there's no date to place them by. Score remains the
    # tiebreak for same-date events, not the primary order.
    horizons["on sale soon"].sort(key=lambda e: (e.on_sale_date is None, e.on_sale_date or "", -e.score))
    for horizon in ("this week", "this month", "announced for later"):
        horizons[horizon].sort(key=lambda e: (e.event_date is None, e.event_date or "", -e.score))

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
    # A past event_date should already be excluded upstream (the query's
    # e.status NOT IN ('past', 'cancelled') filter) -- this lower bound is a
    # second, independent guard against the same failure mode, since without
    # it a past event_day is trivially <= today + near_days and silently
    # gets bucketed into "this week" alongside genuinely upcoming events.
    if event_day < today:
        return "announced for later"
    if event_day <= today + _days(near_days):
        return "this week"
    if event_day <= today + _days(month_days):
        return "this month"
    return "announced for later"


def _days(n: int) -> timedelta:
    return timedelta(days=n)


def build_digest_html(
    household: dict[str, Any],
    horizons: dict[str, list[DigestEvent]],
    reminders: list[AnnualAnchor] | None = None,
) -> str:
    today_str = datetime.now(UTC).strftime("%A %d %B %Y")
    total = sum(len(events) for events in horizons.values())
    new_count = sum(1 for events in horizons.values() for e in events if e.is_new)
    plural = "" if total == 1 else "s"
    new_note = f" &middot; {new_count} new since last time" if new_count else ""
    subtitle = (
        f"For {html.escape(household['label'])} &middot; {total} thing{plural} worth a look"
        f"{new_note} &middot; {today_str}"
    )

    reminder_rows = "".join(_reminder_row_html(a) for a in (reminders or []))
    sections = reminder_rows + "".join(_horizon_section_html(h, horizons[h]) for h in HORIZONS if horizons[h])
    if not sections:
        sections = empty_row("Nothing new this week.")

    footer = (
        "Scored against your taste profile – not for you? Reply, or run "
        f'<code style="background:{ACCENT_BG}; padding:1px 5px; border-radius:3px;">events-agent verdict &lt;id&gt; no</code>.'
    )
    return shell(
        mark_suffix="Shortlist",
        mark_color=ACCENT,
        strapline="Handpicked for your taste",
        subtitle=subtitle,
        body_rows=sections,
        footer=footer,
    )


def _reminder_row_html(anchor: AnnualAnchor) -> str:
    link = (
        f' &nbsp;&middot;&nbsp; <a href="{html.escape(anchor.watch_url)}" style="color:{WARN};">watch page</a>'
        if anchor.watch_url
        else ""
    )
    return f"""\
    <tr>
      <td style="padding:14px 32px; border-bottom:1px solid {BORDER}; background:{WARN_BG};">
        <span style="background:{WARN_BG}; color:{WARN}; font-size:11px; font-weight:700; \
text-transform:uppercase; letter-spacing:0.04em;">Annual anchor</span>
        <div style="font-size:14px; color:{INK}; margin-top:4px;">{html.escape(anchor.name)}'s programme is usually \
announced this month — worth a look{link}.</div>
      </td>
    </tr>"""


def _horizon_section_html(horizon: str, events: list[DigestEvent]) -> str:
    rows = "".join(_event_card_html(e) for e in events)
    return f"""\
    <tr>
      <td style="padding:20px 32px 4px;">
        <div style="font-family:{SERIF}; font-size:13px; font-weight:700; text-transform:uppercase; \
letter-spacing:0.06em; color:{ACCENT}; border-bottom:2px solid {ACCENT}; padding-bottom:6px;">\
{html.escape(horizon.title())}</div>
      </td>
    </tr>
    {rows}"""


def _event_card_html(event: DigestEvent) -> str:
    price = format_price(event.price_min, event.price_max, event.currency)
    date_str = _format_event_date(event.event_date)
    venue = html.escape(event.venue_name) if event.venue_name else "venue TBC"
    title = html.escape(event.title)
    reason = f'<div style="font-size:13px; color:#3E453F; font-style:italic; margin:2px 0 6px;">{html.escape(event.reason)}</div>' if event.reason else ""
    far_flung_badge = (
        f'<span style="background:{FARFLUNG_BG}; color:{FARFLUNG}; padding:2px 8px; border-radius:10px; \
font-weight:600;">worth the trip &middot; {_format_drive_minutes(event.drive_minutes)}</span> &nbsp;&middot;&nbsp; '
        if event.far_flung
        else ""
    )
    new_badge = (
        f'<span style="background:{NEW_BG}; color:{NEW}; padding:2px 8px; border-radius:10px; \
font-weight:600;">new</span> &nbsp; '
        if event.is_new
        else ""
    )
    left_border = f"border-left:3px solid {NEW};" if event.is_new else ""

    return f"""\
    <tr>
      <td style="padding:14px 32px 14px 29px; border-bottom:1px solid {BORDER}; {left_border}">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td style="vertical-align:top;">
              <div style="font-size:12px; color:{MUTED};">{new_badge}{date_str} &middot; {venue}</div>
              <div style="font-size:16px; font-weight:700; color:{INK}; margin:2px 0 2px;">{title}</div>
              {reason}
              <div style="font-size:12px; color:{MUTED};">{far_flung_badge}{price} &nbsp;&middot;&nbsp; \
<span style="background:{ACCENT_BG}; color:{ACCENT}; padding:2px 8px; border-radius:10px; font-weight:600;">\
score {event.score}</span> &nbsp;&middot;&nbsp; <code style="color:{MUTED};">id {event.event_id}</code></div>
            </td>
{cta_cell(event.url, event.title, event.venue_name)}
          </tr>
        </table>
      </td>
    </tr>"""


def _format_drive_minutes(drive_minutes: int | None) -> str:
    if drive_minutes is None:
        return "far"
    hours, minutes = divmod(drive_minutes, 60)
    if hours == 0:
        return f"~{minutes}min drive"
    return f"~{hours}h{minutes:02d}m drive" if minutes else f"~{hours}h drive"


def _format_event_date(event_date: str | None) -> str:
    if not event_date:
        return "Date TBC"
    return datetime.fromisoformat(event_date).strftime("%a %-d %b %y")


def build_digest_plain(
    household: dict[str, Any],
    horizons: dict[str, list[DigestEvent]],
    reminders: list[AnnualAnchor] | None = None,
) -> str:
    new_count = sum(1 for events in horizons.values() for e in events if e.is_new)
    new_note = f" ({new_count} new since last time)" if new_count else ""
    lines = [
        f"CURTAIN UP – SHORTLIST – for {household['label']}{new_note}",
        "Handpicked for your taste",
        "",
    ]
    for anchor in reminders or []:
        watch = f" — {anchor.watch_url}" if anchor.watch_url else ""
        lines.append(f"ANNUAL ANCHOR: {anchor.name}'s programme is usually announced this month{watch}")
    if reminders:
        lines.append("")
    any_events = False
    for horizon in HORIZONS:
        events = horizons[horizon]
        if not events:
            continue
        any_events = True
        lines.append(horizon.upper())
        for event in events:
            price = format_price(event.price_min, event.price_max, event.currency)
            date_str = event.event_date[:10] if event.event_date else "TBC"
            far_flung_tag = f" [WORTH THE TRIP, {_format_drive_minutes(event.drive_minutes)}]" if event.far_flung else ""
            new_tag = " [NEW]" if event.is_new else ""
            lines.append(
                f"-{new_tag} {date_str}  {event.title} @ {event.venue_name or '?'}  {price}  "
                f"(score {event.score}, id {event.event_id}){far_flung_tag}"
            )
            if event.reason:
                lines.append(f"    {event.reason}")
            if event.url:
                lines.append(f"    {event.url}")
        lines.append("")
    if not any_events:
        lines.append("Nothing new this week.")
    return "\n".join(lines)

