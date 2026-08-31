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
    typical_month: str
    programme_announced: str
    watch_url: str = ""


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
    """An anchor is due in the single calendar month immediately before its
    programme_announced month — e.g. programme_announced: october fires
    reminders through all of September, so there's still time to go looking
    before the announcement actually lands. Unrecognised month names are
    skipped rather than raising, consistent with "missing/bad data never
    excludes" elsewhere — a typo in a hand-edited YAML file shouldn't break
    the whole digest send."""
    due = []
    for anchor in anchors:
        announced_month = _MONTH_NUMBERS.get(anchor.programme_announced.strip().lower())
        if announced_month is None:
            continue
        reminder_month = announced_month - 1 or 12
        if today.month == reminder_month:
            due.append(anchor)
    return due
