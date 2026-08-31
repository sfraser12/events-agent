"""Near-miss duplicate detection (Stage 2 of the pipeline).

Exact fingerprint matches already merge via upsert_raw_event's UPDATE path —
this module only handles near-misses: a different fingerprint, but the same
venue and calendar day, with a similar-enough title. Those get flagged in
duplicate_candidate for the LLM to adjudicate later (Stage 3). Never silently
merged — per CLAUDE.md, a false merge loses an event permanently.
"""

from __future__ import annotations

import sqlite3

from rapidfuzz import fuzz

# token_set_ratio, not token_sort_ratio: Ticketmaster's own "Venue Premium - X"
# / "VIP Package - X" duplicates (real, observed live) add whole extra words
# rather than reordering existing ones, so token_sort_ratio only scores them
# ~76-79 (parity-scores as "no match" against any reasonable threshold) while
# token_set_ratio — which scores by token-set overlap, ignoring extra tokens
# in the longer title — scores the true "X" vs "Venue Premium - X" pair 100.
# Empirically checked against otherwise-unrelated Skiddle titles (Scottish
# Ballet vs AEW wrestling) — stays down at ~38, comfortably below threshold.
SIMILARITY_THRESHOLD = 90


def find_and_flag_candidates(conn: sqlite3.Connection, event_id: int) -> int:
    """Compare a newly-created event against others at the same venue/date.

    Only meaningful for brand-new events — an event that matched an existing
    fingerprint was already merged on the update path, so it's not a
    near-miss candidate against anything.
    """
    row = conn.execute(
        "SELECT title_normalised, venue_id, event_date FROM event WHERE id = ?", (event_id,)
    ).fetchone()
    if row is None:
        return 0
    title_normalised, venue_id, event_date = row
    if venue_id is None or event_date is None:
        return 0

    candidates = conn.execute(
        "SELECT id, title_normalised FROM event WHERE venue_id = ? AND date(event_date) = date(?) AND id != ?",
        (venue_id, event_date, event_id),
    ).fetchall()

    flagged = 0
    for other_id, other_title_normalised in candidates:
        score = fuzz.token_set_ratio(title_normalised, other_title_normalised)
        if score >= SIMILARITY_THRESHOLD and _flag_pair(conn, event_id, other_id, score / 100):
            flagged += 1
    return flagged


def _flag_pair(conn: sqlite3.Connection, event_id_a: int, event_id_b: int, similarity: float) -> bool:
    lo, hi = sorted((event_id_a, event_id_b))
    existing = conn.execute(
        "SELECT id FROM duplicate_candidate WHERE event_id_a = ? AND event_id_b = ?", (lo, hi)
    ).fetchone()
    if existing:
        return False
    conn.execute(
        "INSERT INTO duplicate_candidate (event_id_a, event_id_b, similarity) VALUES (?, ?, ?)",
        (lo, hi, similarity),
    )
    return True


# The exact upsell-listing pattern this module's SIMILARITY_THRESHOLD comment
# already names as the real, observed case: Ticketmaster splitting one show
# into a plain listing and a pricier "Venue Premium" / "VIP Package" one.
_PREMIUM_MARKERS = ("venue premium", "vip package", "vip experience", "hospitality package")


def get_suppressed_duplicate_ids(conn: sqlite3.Connection) -> set[int]:
    """Which side of each resolved-same duplicate pair to hide from
    delivery (digest/lookahead).

    event_id_a/event_id_b are stored lo/hi (see _flag_pair) — that's an
    artifact of insertion order, not a signal of which listing is actually
    worth booking. Confirmed live (2026-08-31): Ticketmaster gave a "Venue
    Premium" listing a *lower* id than its own plain counterpart, so always
    suppressing event_id_b kept the pricier upsell and hid the plain
    listing — backwards. Prefer keeping whichever side's title doesn't look
    like a premium/VIP variant; only fall back to the arbitrary lo/hi order
    when neither or both titles match (still deterministic, just no better
    signal available).
    """
    rows = conn.execute(
        """
        SELECT dc.event_id_a, dc.event_id_b, a.title, b.title
        FROM duplicate_candidate dc
        JOIN event a ON a.id = dc.event_id_a
        JOIN event b ON b.id = dc.event_id_b
        WHERE dc.resolution = 'same'
        """
    ).fetchall()
    suppressed: set[int] = set()
    for id_a, id_b, title_a, title_b in rows:
        a_is_premium = _looks_premium(title_a)
        b_is_premium = _looks_premium(title_b)
        if a_is_premium and not b_is_premium:
            suppressed.add(id_a)
        else:
            suppressed.add(id_b)
    return suppressed


def _looks_premium(title: str) -> bool:
    lowered = title.lower()
    return any(marker in lowered for marker in _PREMIUM_MARKERS)
