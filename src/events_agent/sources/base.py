"""SourceAdapter protocol shared by every harvester."""

from __future__ import annotations

import time
from collections.abc import Iterable
from datetime import datetime
from typing import Protocol

from events_agent.models import RawEvent


class SourceAdapter(Protocol):
    name: str

    def fetch(self, since: datetime | None) -> Iterable[RawEvent]: ...


class RateLimiter:
    """Spaces out calls to at least `min_interval_seconds` apart.

    Not a true token bucket — adapters here call sequentially from a single
    thread, so simple spacing gets the same effect with far less code.
    """

    def __init__(self, min_interval_seconds: float):
        self.min_interval = min_interval_seconds
        self._last_call: float | None = None

    def wait(self) -> None:
        if self._last_call is not None:
            elapsed = time.monotonic() - self._last_call
            remaining = self.min_interval - elapsed
            if remaining > 0:
                time.sleep(remaining)
        self._last_call = time.monotonic()
