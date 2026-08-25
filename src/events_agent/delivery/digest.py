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


# Email-safe design: inline styles only (Gmail/Outlook strip <style> blocks
# and won't load external fonts), table-based layout for client compatibility,
# web-safe font stacks only. 600px is the standard email container width.
_BG = "#F4F5F2"
_CARD = "#FFFFFF"
_INK = "#1A1D1B"
_MUTED = "#6B7268"
_BORDER = "#E1E4DE"
_ACCENT = "#1E5C4F"
_ACCENT_BG = "#E4F2ED"
_SERIF = "Georgia,'Times New Roman',serif"
_SANS = "Helvetica,Arial,sans-serif"


def build_digest_html(household: dict[str, Any], horizons: dict[str, list[DigestEvent]]) -> str:
    today_str = datetime.now(UTC).strftime("%A %d %B %Y")
    sections = "".join(_horizon_section_html(h, horizons[h]) for h in HORIZONS if horizons[h])
    if not sections:
        sections = (
            f"<tr><td style=\"padding:24px 32px 32px; font-size:14px; color:{_MUTED}; font-family:{_SANS};\">"
            "Nothing new this week.</td></tr>"
        )

    return f"""\
<div style="background:{_BG}; padding:24px 12px; font-family:{_SANS};">
  <table role="presentation" width="600" cellpadding="0" cellspacing="0" \
style="width:600px; max-width:100%; margin:0 auto; background:{_CARD}; border-radius:8px; overflow:hidden;">
    <tr>
      <td style="padding:28px 32px 8px;">
        <div style="font-family:{_SERIF}; font-size:22px; font-weight:700; color:{_INK};">\
Events digest for {html.escape(household["label"])}</div>
        <div style="font-size:13px; color:{_MUTED}; margin-top:4px;">{today_str}</div>
      </td>
    </tr>
    {sections}
    <tr>
      <td style="padding:18px 32px 26px; font-size:11px; color:{_MUTED}; border-top:1px solid {_BORDER};">
        Scored against your taste profile — not for you? Reply, or run \
<code style="background:{_ACCENT_BG}; padding:1px 5px; border-radius:3px;">events-agent verdict &lt;id&gt; no</code>.
      </td>
    </tr>
  </table>
</div>"""


def _horizon_section_html(horizon: str, events: list[DigestEvent]) -> str:
    rows = "".join(_event_card_html(e) for e in events)
    return f"""\
    <tr>
      <td style="padding:20px 32px 4px;">
        <div style="font-family:{_SERIF}; font-size:13px; font-weight:700; text-transform:uppercase; \
letter-spacing:0.06em; color:{_ACCENT}; border-bottom:2px solid {_ACCENT}; padding-bottom:6px;">\
{html.escape(horizon.title())}</div>
      </td>
    </tr>
    {rows}"""


def _event_card_html(event: DigestEvent) -> str:
    price = _format_price(event.price_min, event.price_max, event.currency)
    date_str = _format_event_date(event.event_date)
    venue = html.escape(event.venue_name) if event.venue_name else "venue TBC"
    title = html.escape(event.title)
    reason = f'<div style="font-size:13px; color:#3E453F; font-style:italic; margin:2px 0 6px;">{html.escape(event.reason)}</div>' if event.reason else ""

    cta = ""
    if event.url:
        cta = f"""\
        <td style="vertical-align:top; text-align:right; padding-left:12px; width:96px;">
          <a href="{html.escape(event.url)}" style="display:inline-block; background:{_ACCENT}; color:#FFFFFF; \
text-decoration:none; font-size:13px; font-weight:600; padding:8px 14px; border-radius:6px; white-space:nowrap;">\
Book &rarr;</a>
        </td>"""

    return f"""\
    <tr>
      <td style="padding:14px 32px; border-bottom:1px solid {_BORDER};">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td style="vertical-align:top;">
              <div style="font-size:12px; color:{_MUTED};">{date_str} &middot; {venue}</div>
              <div style="font-size:16px; font-weight:700; color:{_INK}; margin:2px 0 2px;">{title}</div>
              {reason}
              <div style="font-size:12px; color:{_MUTED};">{price} &nbsp;&middot;&nbsp; \
<span style="background:{_ACCENT_BG}; color:{_ACCENT}; padding:2px 8px; border-radius:10px; font-weight:600;">\
score {event.score}</span></div>
            </td>
{cta}
          </tr>
        </table>
      </td>
    </tr>"""


def _format_event_date(event_date: str | None) -> str:
    if not event_date:
        return "Date TBC"
    return datetime.fromisoformat(event_date).strftime("%a %-d %b")


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
