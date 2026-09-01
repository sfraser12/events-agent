"""Annual anchors: hand-maintained recurring fixtures worth watching for a
programme announcement (see CLAUDE.md "Configuration" and
annual-anchors.example.yaml). Not a fetcher — no adapter, no API call, just
a periodic digest reminder read from a small YAML file the household edits
by hand once a year.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel

_MONTH_NUMBERS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}


class AnnualAnchor(BaseModel):
    name: str
    typical_month: str | None = None
    programme_announced: str | None = None
    # Alternative to typical_month/programme_announced, for a fixture with a
    # genuine fixed recurring date (Hogmanay fire festivals, Up Helly Aa)
    # rather than a "programme announced" lead time — "MM-DD".
    fixed_date: str | None = None
    remind_days_before: int = 21
    watch_url: str = ""

    # Populated by due_reminders() for a fixed_date anchor, not read from
    # YAML — the concrete date of the next occurrence, for display.
    next_occurrence: date | None = None


def load_annual_anchors(path: Path) -> list[AnnualAnchor]:
    """A missing file is not an error — this is optional, hand-maintained
    infrastructure (see CLAUDE.md Phase 7), not a hard dependency of the
    digest. An empty list just means no reminders this run."""
    if not path.exists():
        return []
    with path.open() as f:
        raw = yaml.safe_load(f) or []
    return [AnnualAnchor.model_validate(item) for item in raw]


def due_reminders(anchors: list[AnnualAnchor], today: date) -> list[AnnualAnchor]:
    """Two anchor shapes, checked separately:

    - programme_announced (Celtic Connections, Fringe, panto, ...): due in
      the single calendar month immediately before that month — e.g.
      programme_announced: october fires reminders through all of
      September, so there's still time to go looking before the
      announcement actually lands.
    - fixed_date (Hogmanay fire festivals, Up Helly Aa, ...): due for the
      remind_days_before window immediately before the next occurrence of
      that MM-DD — these are free public fixtures with a real date, not an
      on-sale window to watch for, so the reminder is "this is coming up"
      rather than "go check for an announcement."

    Unrecognised month names / malformed fixed_date values are skipped
    rather than raising, consistent with "missing/bad data never excludes"
    elsewhere — a typo in a hand-edited YAML file shouldn't break the whole
    digest send."""
    due = []
    for anchor in anchors:
        if anchor.fixed_date:
            occurrence = _next_occurrence(anchor.fixed_date, today)
            if occurrence is None:
                continue
            days_until = (occurrence - today).days
            if 0 <= days_until <= anchor.remind_days_before:
                due.append(anchor.model_copy(update={"next_occurrence": occurrence}))
            continue

        if not anchor.programme_announced:
            continue
        announced_month = _MONTH_NUMBERS.get(anchor.programme_announced.strip().lower())
        if announced_month is None:
            continue
        reminder_month = announced_month - 1 or 12
        if today.month == reminder_month:
            due.append(anchor)
    return due


def _next_occurrence(fixed_date: str, today: date) -> date | None:
    try:
        month_str, day_str = fixed_date.strip().split("-")
        month, day = int(month_str), int(day_str)
        occurrence = date(today.year, month, day)
    except (ValueError, TypeError):
        return None
    if occurrence < today:
        occurrence = date(today.year + 1, month, day)
    return occurrence
