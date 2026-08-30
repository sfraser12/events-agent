"""Shared design tokens + outer shell for every Curtain Up email.

Email-safe: inline styles only (Gmail/Outlook strip <style> blocks and won't
load external fonts), table-based layout for client compatibility, web-safe
font stacks only. 600px is the standard email container width. Same palette
as the events.db browser artifact, so the product reads as one thing across
web and email.
"""

from __future__ import annotations

import html
from urllib.parse import quote_plus

BRAND = "Curtain Up"

BG = "#F4F5F2"
CARD = "#FFFFFF"
INK = "#1A1D1B"
MUTED = "#6B7268"
BORDER = "#E1E4DE"
ACCENT = "#1E5C4F"
ACCENT_BG = "#E4F2ED"
WARN = "#B5730E"
WARN_BG = "#F7ECD9"
URGENT = "#B4423A"
URGENT_BG = "#F6E4E2"
LOOKAHEAD = "#3D5A80"
LOOKAHEAD_BG = "#E6EBF3"
FARFLUNG = "#6B4C8A"
FARFLUNG_BG = "#EEE7F3"
SERIF = "Georgia,'Times New Roman',serif"
SANS = "Helvetica,Arial,sans-serif"


def shell(*, mark_suffix: str, mark_color: str, subtitle: str, body_rows: str, footer: str) -> str:
    """The card every email is built from: a single color-coded brand mark
    ("Curtain Up — Last call" etc, one chip, one style, colored per email type),
    a subtitle line, then caller-supplied <tr> rows, then a footer row.
    Callers pass fully-built <tr> markup for body_rows — this only owns the
    outer shape."""
    return f"""\
<div style="background:{BG}; padding:24px 12px; font-family:{SANS};">
  <table role="presentation" width="600" cellpadding="0" cellspacing="0" \
style="width:600px; max-width:100%; margin:0 auto; background:{CARD}; border-radius:8px; overflow:hidden;">
    <tr>
      <td style="padding:28px 32px 8px;">
        <div style="display:inline-block; background:{mark_color}; color:#FFFFFF; font-family:{SANS}; font-size:13px; \
font-weight:800; letter-spacing:0.08em; text-transform:uppercase; padding:6px 12px; border-radius:3px;">{BRAND} &mdash; {html.escape(mark_suffix)}</div>
        <div style="font-size:13px; color:{MUTED}; margin-top:8px;">{subtitle}</div>
      </td>
    </tr>
    {body_rows}
    <tr>
      <td style="padding:18px 32px 26px; font-size:11px; color:{MUTED}; border-top:1px solid {BORDER};">
        {footer}
      </td>
    </tr>
  </table>
</div>"""


def format_price(price_min: float | None, price_max: float | None, currency: str) -> str:
    if price_min is None and price_max is None:
        return "price TBC"
    if price_min == price_max:
        return f"{currency} {price_min:.2f}"
    return f"{currency} {price_min:.2f}-{price_max:.2f}"


def empty_row(message: str) -> str:
    return f'<tr><td style="padding:24px 32px 32px; font-size:14px; color:{MUTED}; font-family:{SANS};">{html.escape(message)}</td></tr>'


def cta_cell(url: str | None, title: str | None = None, venue_name: str | None = None) -> str:
    """The "Book" button, plus a small fallback search link underneath when
    a title is available -- booking links do go dead (sold out and pulled,
    or just a stale/misfiring page on the source's end; confirmed
    2026-08-29, see mark_delisted_events in db.py for the harvest-side half
    of this fix), and a dead link with no way out is a bad surprise days or
    weeks after the email was sent. The search fallback works regardless of
    *why* the direct link failed, which a source-specific "try again" link
    couldn't."""
    if not url:
        return ""
    search_link = _search_fallback_link(title, venue_name) if title else ""
    return f"""\
        <td style="vertical-align:top; text-align:right; padding-left:12px; width:96px;">
          <a href="{html.escape(url)}" style="display:inline-block; background:{ACCENT}; color:#FFFFFF; \
text-decoration:none; font-size:13px; font-weight:600; padding:8px 14px; border-radius:6px; white-space:nowrap;">\
Book &rarr;</a>
          {search_link}
        </td>"""


def _search_fallback_link(title: str, venue_name: str | None) -> str:
    query = f"{title} {venue_name} tickets" if venue_name else f"{title} tickets"
    search_url = f"https://www.google.com/search?q={quote_plus(query)}"
    return f"""\
<div style="margin-top:6px;"><a href="{search_url}" style="font-size:11px; color:{MUTED}; \
text-decoration:underline;">link not working?</a></div>"""
