"""Shared design tokens + outer shell for every Roundup email.

Email-safe: inline styles only (Gmail/Outlook strip <style> blocks and won't
load external fonts), table-based layout for client compatibility, web-safe
font stacks only. 600px is the standard email container width. Same palette
as the events.db browser artifact, so the product reads as one thing across
web and email.
"""

from __future__ import annotations

import html

BRAND = "Roundup"

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
SERIF = "Georgia,'Times New Roman',serif"
SANS = "Helvetica,Arial,sans-serif"


def shell(*, eyebrow: str | None, eyebrow_color: str, eyebrow_bg: str, subtitle: str, body_rows: str, footer: str) -> str:
    """The card every email is built from: optional colored eyebrow badge,
    the Roundup wordmark, a subtitle line, then caller-supplied <tr> rows,
    then a footer row. Callers pass fully-built <tr> markup for body_rows —
    this only owns the outer shape."""
    eyebrow_html = ""
    if eyebrow:
        eyebrow_html = f"""\
        <div style="display:inline-block; background:{eyebrow_bg}; color:{eyebrow_color}; font-size:11px; \
font-weight:700; text-transform:uppercase; letter-spacing:0.06em; padding:4px 10px; border-radius:999px; \
margin-bottom:10px;">{html.escape(eyebrow)}</div><br>"""

    return f"""\
<div style="background:{BG}; padding:24px 12px; font-family:{SANS};">
  <table role="presentation" width="600" cellpadding="0" cellspacing="0" \
style="width:600px; max-width:100%; margin:0 auto; background:{CARD}; border-radius:8px; overflow:hidden;">
    <tr>
      <td style="padding:28px 32px 8px;">
        {eyebrow_html}
        <div style="border-left:4px solid {ACCENT}; padding-left:12px; font-family:{SERIF}; font-size:20px; font-weight:800; letter-spacing:0.08em; text-transform:uppercase; color:{INK};">{BRAND}</div>
        <div style="font-size:13px; color:{MUTED}; margin-top:4px;">{subtitle}</div>
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


def cta_cell(url: str | None) -> str:
    if not url:
        return ""
    return f"""\
        <td style="vertical-align:top; text-align:right; padding-left:12px; width:96px;">
          <a href="{html.escape(url)}" style="display:inline-block; background:{ACCENT}; color:#FFFFFF; \
text-decoration:none; font-size:13px; font-weight:600; padding:8px 14px; border-radius:6px; white-space:nowrap;">\
Book &rarr;</a>
        </td>"""
