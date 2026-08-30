"""The daily urgent alert (Stage 4, "Deliver").

Fires only for the two things CLAUDE.md calls the highest-value output in the
whole system: an on-sale date landing in the next 48 hours, or a status flip
to low_availability. Both are read off event_change rows, never off the full
event table, and each change is only ever surfaced once (see notified_at).
"""

from __future__ import annotations

import html
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from events_agent.constraints import event_matches_household
from events_agent.delivery.email_design import BORDER, INK, MUTED, URGENT, URGENT_BG, WARN, WARN_BG, cta_cell, empty_row, shell

ALERT_WINDOW = timedelta(hours=48)


@dataclass
class AlertItem:
    change_id: int
    event_id: int
    title: str
    venue_name: str | None
    url: str | None
    kind: str  # "low_availability" | "on_sale_soon"
    reason: str  # pre-formatted, terminal-friendly sentence
    on_sale_date: str | None = None  # raw ISO — only set when kind == "on_sale_soon"


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
        if field == "status":
            items.append(
                AlertItem(change_id, event_id, title, venue_name, url, "low_availability", _low_availability_reason())
            )
        else:
            items.append(
                AlertItem(
                    change_id, event_id, title, venue_name, url, "on_sale_soon", _on_sale_reason(new_value), new_value
                )
            )
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


def _human_countdown(target: datetime, now: datetime) -> str:
    seconds = (target - now).total_seconds()
    if seconds <= 0:
        return "any moment now"
    hours = seconds / 3600
    if hours < 1:
        minutes = round(seconds / 60)
        return f"in {minutes} minute{'s' if minutes != 1 else ''}"
    if hours < 24:
        h = round(hours)
        return f"in {h} hour{'s' if h != 1 else ''}"
    days = int(hours // 24)
    rem_hours = round(hours - days * 24)
    if rem_hours == 0:
        return f"in {days} day{'s' if days != 1 else ''}"
    return f"in {days} day{'s' if days != 1 else ''} {rem_hours} hour{'s' if rem_hours != 1 else ''}"


def build_alert_html(household: dict[str, Any], items: list[AlertItem], now: datetime) -> str:
    plural = "" if len(items) == 1 else "s"
    subtitle = f"For {html.escape(household['label'])} &middot; {len(items)} thing{plural} need a decision today"
    rows = "".join(_alert_card_html(item, now) for item in items) if items else empty_row("No urgent alerts.")
    footer = "Fires only on a status flip to low availability, or an on-sale date landing within 48 hours — nothing else."
    return shell(
        mark_suffix="Last call",
        mark_color=URGENT,
        strapline="On sale soon, selling fast — act today",
        subtitle=subtitle,
        body_rows=rows,
        footer=footer,
    )


def _alert_card_html(item: AlertItem, now: datetime) -> str:
    venue = html.escape(item.venue_name) if item.venue_name else "venue TBC"
    title = html.escape(item.title)

    if item.kind == "low_availability":
        badge_color, badge_bg, badge_text = URGENT, URGENT_BG, "Selling fast"
        detail = "Availability is dropping — don't wait on this one."
    else:
        badge_color, badge_bg, badge_text = WARN, WARN_BG, "On sale soon"
        on_sale_dt = datetime.fromisoformat(item.on_sale_date)
        detail = f"On sale {on_sale_dt.strftime('%a %d %b, %H:%M')} &middot; {_human_countdown(on_sale_dt, now)}"

    return f"""\
    <tr>
      <td style="padding:14px 32px; border-bottom:1px solid {BORDER};">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td style="vertical-align:top;">
              <span style="background:{badge_bg}; color:{badge_color}; font-size:11px; font-weight:700; \
text-transform:uppercase; letter-spacing:0.04em; padding:2px 8px; border-radius:10px;">{badge_text}</span>
              <div style="font-size:16px; font-weight:700; color:{INK}; margin:6px 0 2px;">{title}</div>
              <div style="font-size:12px; color:{MUTED};">{venue}</div>
              <div style="font-size:13px; color:{badge_color}; font-weight:600; margin-top:4px;">{detail}</div>
            </td>
{cta_cell(item.url, item.title, item.venue_name)}
          </tr>
        </table>
      </td>
    </tr>"""


def build_alert_plain(household: dict[str, Any], items: list[AlertItem], now: datetime) -> str:
    lines = [
        f"CURTAIN UP — LAST CALL — for {household['label']}",
        "On sale soon, selling fast — act today",
        "",
    ]
    if not items:
        lines.append("No urgent alerts.")
        return "\n".join(lines)

    for item in items:
        venue = item.venue_name or "venue TBC"
        if item.kind == "low_availability":
            detail = "Selling fast — availability is dropping. Don't wait on this one."
        else:
            on_sale_dt = datetime.fromisoformat(item.on_sale_date)
            detail = f"On sale {on_sale_dt.strftime('%a %d %b, %H:%M')} ({_human_countdown(on_sale_dt, now)})"
        lines.append(f"- {item.title} @ {venue}")
        lines.append(f"    {detail}")
        if item.url:
            lines.append(f"    {item.url}")
        lines.append("")
    return "\n".join(lines)
