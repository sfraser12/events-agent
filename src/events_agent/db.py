"""SQLite schema, connection helper, and upsert logic. Plain sqlite3 — no ORM."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime
from math import asin, cos, radians, sin, sqrt
from pathlib import Path

from events_agent.dedupe import find_and_flag_candidates
from events_agent.models import RawEvent
from events_agent.normalise import fingerprint, normalise_text

# Fallback venue-matching distance when two sources spell/geocode the same
# building differently (confirmed real: Skiddle vs Ticketmaster's pins for the
# same SWG3 space sit ~140m apart). Small enough to stay low-risk in a dense
# city center, comfortably above that observed drift.
VENUE_PROXIMITY_METERS = 150

SCHEMA = """
CREATE TABLE IF NOT EXISTS venue (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    name_normalised TEXT NOT NULL UNIQUE,
    city            TEXT,
    postcode        TEXT,
    latitude        REAL,
    longitude       REAL,
    type            TEXT              -- source-provided venue category, e.g. "bar", "theatre"
);

CREATE TABLE IF NOT EXISTS event (
    id              INTEGER PRIMARY KEY,
    fingerprint     TEXT NOT NULL,
    title           TEXT NOT NULL,
    title_normalised TEXT NOT NULL,
    category        TEXT,
    venue_id        INTEGER REFERENCES venue(id),

    event_date      TEXT,
    event_date_end  TEXT,
    announced_date  TEXT,
    on_sale_date    TEXT,

    status          TEXT,
    price_min       REAL,
    price_max       REAL,
    currency        TEXT DEFAULT 'GBP',
    url             TEXT,
    blurb           TEXT,

    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_event_fingerprint ON event(fingerprint);
CREATE INDEX IF NOT EXISTS idx_event_date        ON event(event_date);
CREATE INDEX IF NOT EXISTS idx_event_onsale      ON event(on_sale_date);

-- One row per household (today: just the one). Scoring/delivery settings
-- mirror config.yaml + taste-profile.md — config.yaml stays the human-edited
-- source of truth, this row is a queryable materialization of it, re-synced
-- on every `events-agent init`.
CREATE TABLE IF NOT EXISTS household (
    id                  INTEGER PRIMARY KEY,
    label               TEXT NOT NULL,
    home_latitude       REAL NOT NULL,
    home_longitude      REAL NOT NULL,
    radius_miles        REAL NOT NULL,
    near_days           INTEGER,           -- digest horizon: "this week" cutoff
    month_days          INTEGER,           -- digest horizon: "this month" cutoff
    max_drive_minutes   INTEGER,
    price_ceiling       REAL,
    blackout_dates      TEXT,              -- JSON array of [start_iso, end_iso] pairs
    taste_profile_path  TEXT NOT NULL,
    digest_threshold    INTEGER,
    alert_threshold     INTEGER,
    email_to            TEXT,
    created_at          TEXT NOT NULL
);

-- Per-household view of an event: score, verdict, snooze. Split out from
-- `event` because those are judgments about taste, not facts about the
-- event — two households can (and will) score the same gig differently.
CREATE TABLE IF NOT EXISTS household_event_state (
    household_id    INTEGER NOT NULL REFERENCES household(id),
    event_id        INTEGER NOT NULL REFERENCES event(id),

    score           INTEGER,
    audience        TEXT,
    score_reason    TEXT,
    urgency         TEXT,
    scored_at       TEXT,

    verdict         TEXT,
    verdict_at      TEXT,
    snoozed_until   TEXT,
    surfaced_at     TEXT,

    PRIMARY KEY (household_id, event_id)
);

CREATE INDEX IF NOT EXISTS idx_hes_verdict ON household_event_state(verdict);
CREATE INDEX IF NOT EXISTS idx_hes_score   ON household_event_state(score);

CREATE TABLE IF NOT EXISTS event_source (
    event_id        INTEGER REFERENCES event(id),
    source_name     TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    source_url      TEXT,
    raw_json        TEXT,
    PRIMARY KEY (source_name, source_event_id)
);

CREATE TABLE IF NOT EXISTS event_change (
    id              INTEGER PRIMARY KEY,
    event_id        INTEGER REFERENCES event(id),
    field           TEXT NOT NULL,
    old_value       TEXT,
    new_value       TEXT,
    detected_at     TEXT NOT NULL,
    notified_at     TEXT              -- set once the daily alert has surfaced this change
);

CREATE TABLE IF NOT EXISTS duplicate_candidate (
    id              INTEGER PRIMARY KEY,
    event_id_a      INTEGER REFERENCES event(id),
    event_id_b      INTEGER REFERENCES event(id),
    similarity      REAL,
    resolution      TEXT,
    resolved_at     TEXT
);

CREATE TABLE IF NOT EXISTS source_run (
    id              INTEGER PRIMARY KEY,
    source_name     TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    status          TEXT,
    rows_fetched    INTEGER,
    error           TEXT
);
"""


def get_connection(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: Path) -> None:
    conn = get_connection(db_path)
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Migrations for databases created before a schema change.

    CREATE TABLE IF NOT EXISTS leaves existing tables untouched, so a changed
    column set needs an explicit ALTER TABLE here too.
    """
    venue_columns = {row[1] for row in conn.execute("PRAGMA table_info(venue)")}
    if "type" not in venue_columns:
        conn.execute("ALTER TABLE venue ADD COLUMN type TEXT")
    if "drive_minutes" in venue_columns:
        # Was a venue-level cache of "minutes from home" — but "home" is
        # per-household now (Phase 4), so a single cached value on the venue
        # row is the wrong shape. Never populated by any code either way.
        conn.execute("ALTER TABLE venue DROP COLUMN drive_minutes")

    event_change_columns = {row[1] for row in conn.execute("PRAGMA table_info(event_change)")}
    if "notified_at" not in event_change_columns:
        conn.execute("ALTER TABLE event_change ADD COLUMN notified_at TEXT")

    # Phase 4: score/audience/verdict/etc. moved off `event` onto the new
    # household_event_state table (per-household judgments, not facts about
    # the event). Safe as a hard drop, not a data-preserving migration: these
    # columns are NULL for every event in every database that predates
    # scoring ever running.
    event_columns = {row[1] for row in conn.execute("PRAGMA table_info(event)")}
    if "score" in event_columns:
        conn.execute("DROP INDEX IF EXISTS idx_event_verdict")
        for column in (
            "score",
            "audience",
            "score_reason",
            "urgency",
            "scored_at",
            "verdict",
            "verdict_at",
            "snoozed_until",
            "surfaced_at",
        ):
            conn.execute(f"ALTER TABLE event DROP COLUMN {column}")

    # digest_day/digest_hour were never read anywhere — cadence belongs to
    # the launchd plist, not app config that can drift out of sync with it.
    household_columns = {row[1] for row in conn.execute("PRAGMA table_info(household)")}
    if "digest_day" in household_columns:
        conn.execute("ALTER TABLE household DROP COLUMN digest_day")
    if "digest_hour" in household_columns:
        conn.execute("ALTER TABLE household DROP COLUMN digest_hour")


def upsert_venue(
    conn: sqlite3.Connection,
    name: str,
    city: str | None,
    postcode: str | None,
    latitude: float | None,
    longitude: float | None,
    venue_type: str | None = None,
) -> int:
    name_normalised = normalise_text(name)
    row = conn.execute(
        "SELECT id FROM venue WHERE name_normalised = ?", (name_normalised,)
    ).fetchone()
    if row:
        venue_id = row[0]
        # COALESCE: a source that doesn't expose a venue "type" (e.g.
        # Ticketmaster) passes None here — that must never overwrite a type
        # a different source already set for this same venue.
        conn.execute(
            "UPDATE venue SET name = ?, city = ?, postcode = ?, latitude = ?, longitude = ?, "
            "type = COALESCE(?, type) WHERE id = ?",
            (name, city, postcode, latitude, longitude, venue_type, venue_id),
        )
        return venue_id

    if latitude is not None and longitude is not None:
        nearby_id = _find_nearby_venue(conn, latitude, longitude)
        if nearby_id is not None:
            # Proximity-only match: two sources spell/geocode the same venue
            # differently. Reuse the row but don't touch its stored fields —
            # only an exact-name match updates those, so the canonical name
            # doesn't flap between sources' differing spellings each harvest.
            return nearby_id

    cur = conn.execute(
        "INSERT INTO venue (name, name_normalised, city, postcode, latitude, longitude, type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (name, name_normalised, city, postcode, latitude, longitude, venue_type),
    )
    return cur.lastrowid


def _find_nearby_venue(conn: sqlite3.Connection, latitude: float, longitude: float) -> int | None:
    rows = conn.execute(
        "SELECT id, latitude, longitude FROM venue WHERE latitude IS NOT NULL AND longitude IS NOT NULL"
    ).fetchall()
    for venue_id, v_lat, v_lon in rows:
        if haversine_meters(latitude, longitude, v_lat, v_lon) <= VENUE_PROXIMITY_METERS:
            return venue_id
    return None


def haversine_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    earth_radius_m = 6_371_000
    phi1, phi2 = radians(lat1), radians(lat2)
    d_phi = radians(lat2 - lat1)
    d_lambda = radians(lon2 - lon1)
    a = sin(d_phi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(d_lambda / 2) ** 2
    return 2 * earth_radius_m * asin(sqrt(a))


def upsert_raw_event(conn: sqlite3.Connection, raw: RawEvent) -> tuple[int, bool]:
    """Insert or update the event/venue/event_source rows for one RawEvent.

    Returns (event_id, created) — created is False when an existing event with
    the same fingerprint was updated instead of a new row being inserted.
    """
    now = datetime.now(UTC).isoformat()

    venue_id = upsert_venue(
        conn,
        raw.venue_name,
        raw.venue_city,
        raw.venue_postcode,
        raw.venue_latitude,
        raw.venue_longitude,
        raw.venue_type,
    )

    title_normalised = normalise_text(raw.title)
    fp = fingerprint(raw.title, raw.venue_name, raw.event_date)
    new_on_sale_date = _iso_or_none(raw.on_sale_date)

    row = conn.execute(
        "SELECT id, status, price_min, price_max, on_sale_date FROM event WHERE fingerprint = ?", (fp,)
    ).fetchone()
    if row:
        event_id, old_status, old_price_min, old_price_max, old_on_sale_date = row
        created = False
        conn.execute(
            """
            UPDATE event SET
                title = ?, title_normalised = ?, category = ?, venue_id = ?,
                event_date = ?, event_date_end = ?, status = ?,
                price_min = ?, price_max = ?, currency = ?, on_sale_date = ?, url = ?, blurb = ?,
                last_seen = ?
            WHERE id = ?
            """,
            (
                raw.title,
                title_normalised,
                raw.category,
                venue_id,
                _iso_or_none(raw.event_date),
                _iso_or_none(raw.event_date_end),
                raw.status,
                raw.price_min,
                raw.price_max,
                raw.currency,
                new_on_sale_date,
                raw.url,
                raw.blurb,
                now,
                event_id,
            ),
        )
        _record_changes(
            conn,
            event_id,
            old_status=old_status,
            old_price_min=old_price_min,
            old_price_max=old_price_max,
            old_on_sale_date=old_on_sale_date,
            raw=raw,
            new_on_sale_date=new_on_sale_date,
            now=now,
        )
    else:
        created = True
        cur = conn.execute(
            """
            INSERT INTO event (
                fingerprint, title, title_normalised, category, venue_id,
                event_date, event_date_end, status,
                price_min, price_max, currency, on_sale_date, url, blurb,
                first_seen, last_seen
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fp,
                raw.title,
                title_normalised,
                raw.category,
                venue_id,
                _iso_or_none(raw.event_date),
                _iso_or_none(raw.event_date_end),
                raw.status,
                raw.price_min,
                raw.price_max,
                raw.currency,
                new_on_sale_date,
                raw.url,
                raw.blurb,
                now,
                now,
            ),
        )
        event_id = cur.lastrowid
        # Only a brand-new event needs near-miss dedupe — one that matched an
        # existing fingerprint was already merged above, not a candidate.
        find_and_flag_candidates(conn, event_id)

    conn.execute(
        """
        INSERT INTO event_source (event_id, source_name, source_event_id, source_url, raw_json)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(source_name, source_event_id) DO UPDATE SET
            event_id = excluded.event_id,
            source_url = excluded.source_url,
            raw_json = excluded.raw_json
        """,
        (event_id, raw.source_name, raw.source_event_id, raw.url, _to_json(raw.raw)),
    )

    return event_id, created


def start_source_run(conn: sqlite3.Connection, source_name: str) -> int:
    cur = conn.execute(
        "INSERT INTO source_run (source_name, started_at, status) VALUES (?, ?, ?)",
        (source_name, datetime.now(UTC).isoformat(), "running"),
    )
    return cur.lastrowid


def finish_source_run(
    conn: sqlite3.Connection,
    run_id: int,
    status: str,
    rows_fetched: int,
    error: str | None = None,
) -> None:
    conn.execute(
        "UPDATE source_run SET finished_at = ?, status = ?, rows_fetched = ?, error = ? WHERE id = ?",
        (datetime.now(UTC).isoformat(), status, rows_fetched, error, run_id),
    )


def mark_past_events(conn: sqlite3.Connection, today: date) -> int:
    """Flip status to 'past' for dated events whose event_date has elapsed.

    Skiddle's fetch window always starts at "today", so once an event's date
    slips behind that window no further harvest ever touches its row again —
    without this, it sits mislabeled (usually still 'on_sale') forever. Marks
    rather than deletes, and logs each flip to event_change so it's visible
    alongside every other status transition. Cancelled events are left alone;
    "cancelled" is more informative than "past" and shouldn't be overwritten.
    """
    now = datetime.now(UTC).isoformat()
    rows = conn.execute(
        "SELECT id, status, event_date FROM event WHERE event_date IS NOT NULL AND status NOT IN ('past', 'cancelled')"
    ).fetchall()

    marked = 0
    for event_id, old_status, event_date in rows:
        if datetime.fromisoformat(event_date).date() < today:
            conn.execute("UPDATE event SET status = 'past' WHERE id = ?", (event_id,))
            conn.execute(
                "INSERT INTO event_change (event_id, field, old_value, new_value, detected_at) VALUES (?, 'status', ?, 'past', ?)",
                (event_id, old_status, now),
            )
            marked += 1
    return marked


def upsert_household(
    conn: sqlite3.Connection,
    household_id: int,
    label: str,
    home_latitude: float,
    home_longitude: float,
    radius_miles: float,
    near_days: int | None,
    month_days: int | None,
    max_drive_minutes: int | None,
    price_ceiling: float | None,
    blackout_dates: list[tuple[str, str]],
    taste_profile_path: str,
    digest_threshold: int | None,
    alert_threshold: int | None,
    email_to: str | None,
) -> int:
    """Idempotent upsert by explicit id — config.yaml stays the source of
    truth, this row is a queryable materialization of it, re-synced on every
    `events-agent init` (same pattern as upsert_venue, just keyed by an
    explicit id rather than a natural key, since a household has no other
    unique identifier)."""
    now = datetime.now(UTC).isoformat()
    conn.execute(
        """
        INSERT INTO household (
            id, label, home_latitude, home_longitude, radius_miles, near_days, month_days,
            max_drive_minutes, price_ceiling, blackout_dates, taste_profile_path,
            digest_threshold, alert_threshold, email_to,
            created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            label = excluded.label,
            home_latitude = excluded.home_latitude,
            home_longitude = excluded.home_longitude,
            radius_miles = excluded.radius_miles,
            near_days = excluded.near_days,
            month_days = excluded.month_days,
            max_drive_minutes = excluded.max_drive_minutes,
            price_ceiling = excluded.price_ceiling,
            blackout_dates = excluded.blackout_dates,
            taste_profile_path = excluded.taste_profile_path,
            digest_threshold = excluded.digest_threshold,
            alert_threshold = excluded.alert_threshold,
            email_to = excluded.email_to
        """,
        (
            household_id,
            label,
            home_latitude,
            home_longitude,
            radius_miles,
            near_days,
            month_days,
            max_drive_minutes,
            price_ceiling,
            _to_json(blackout_dates),
            taste_profile_path,
            digest_threshold,
            alert_threshold,
            email_to,
            now,
        ),
    )
    return household_id


HOUSEHOLD_COLUMNS = (
    "id",
    "label",
    "home_latitude",
    "home_longitude",
    "radius_miles",
    "near_days",
    "month_days",
    "max_drive_minutes",
    "price_ceiling",
    "blackout_dates",
    "taste_profile_path",
    "digest_threshold",
    "alert_threshold",
    "email_to",
)


def list_households(conn: sqlite3.Connection) -> list[tuple]:
    """Rows shaped per HOUSEHOLD_COLUMNS (blackout_dates still JSON text —
    callers that need it parsed should json.loads it)."""
    return conn.execute(f"SELECT {', '.join(HOUSEHOLD_COLUMNS)} FROM household ORDER BY id").fetchall()


def get_household(conn: sqlite3.Connection, household_id: int) -> tuple | None:
    return conn.execute(
        f"SELECT {', '.join(HOUSEHOLD_COLUMNS)} FROM household WHERE id = ?", (household_id,)
    ).fetchone()


def list_households_as_dicts(conn: sqlite3.Connection) -> list[dict]:
    """Same rows as list_households, as {column: value} dicts — every
    household-scoped call site (scoring, digest, alert) wants named access,
    not positional."""
    return [dict(zip(HOUSEHOLD_COLUMNS, row, strict=True)) for row in list_households(conn)]


def get_household_as_dict(conn: sqlite3.Connection, household_id: int) -> dict | None:
    row = get_household(conn, household_id)
    return dict(zip(HOUSEHOLD_COLUMNS, row, strict=True)) if row else None


def upsert_score(
    conn: sqlite3.Connection,
    household_id: int,
    event_id: int,
    score: int | None,
    audience: str | None,
    score_reason: str | None,
    urgency: str | None,
    scored_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO household_event_state (household_id, event_id, score, audience, score_reason, urgency, scored_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(household_id, event_id) DO UPDATE SET
            score = excluded.score,
            audience = excluded.audience,
            score_reason = excluded.score_reason,
            urgency = excluded.urgency,
            scored_at = excluded.scored_at
        """,
        (household_id, event_id, score, audience, score_reason, urgency, scored_at),
    )


def set_verdict(
    conn: sqlite3.Connection,
    household_id: int,
    event_id: int,
    verdict: str,
    verdict_at: str | None = None,
    snoozed_until: str | None = None,
) -> None:
    now = verdict_at or datetime.now(UTC).isoformat()
    conn.execute(
        """
        INSERT INTO household_event_state (household_id, event_id, verdict, verdict_at, snoozed_until)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(household_id, event_id) DO UPDATE SET
            verdict = excluded.verdict,
            verdict_at = excluded.verdict_at,
            snoozed_until = excluded.snoozed_until
        """,
        (household_id, event_id, verdict, now, snoozed_until),
    )


def mark_surfaced(conn: sqlite3.Connection, household_id: int, event_ids: list[int], surfaced_at: str) -> None:
    conn.executemany(
        "UPDATE household_event_state SET surfaced_at = ? WHERE household_id = ? AND event_id = ?",
        [(surfaced_at, household_id, event_id) for event_id in event_ids],
    )


def _record_changes(
    conn: sqlite3.Connection,
    event_id: int,
    *,
    old_status: str | None,
    old_price_min: float | None,
    old_price_max: float | None,
    old_on_sale_date: str | None,
    raw: RawEvent,
    new_on_sale_date: str | None,
    now: str,
) -> None:
    """Compare the stored row against the incoming RawEvent and log any of the
    fields CLAUDE.md calls out (status, price_range, on_sale_date) to event_change.

    Only called on the update path — a brand-new event has nothing to diff
    against, so first sight of an event never produces a change row.
    """
    changes: list[tuple[str, str | None, str | None]] = []

    if old_status != raw.status:
        changes.append(("status", old_status, raw.status))

    old_price_range = _price_range(old_price_min, old_price_max)
    new_price_range = _price_range(raw.price_min, raw.price_max)
    if old_price_range != new_price_range:
        changes.append(("price_range", old_price_range, new_price_range))

    if old_on_sale_date != new_on_sale_date:
        changes.append(("on_sale_date", old_on_sale_date, new_on_sale_date))

    conn.executemany(
        "INSERT INTO event_change (event_id, field, old_value, new_value, detected_at) VALUES (?, ?, ?, ?, ?)",
        [(event_id, field, old_value, new_value, now) for field, old_value, new_value in changes],
    )


def _price_range(price_min: float | None, price_max: float | None) -> str | None:
    if price_min is None and price_max is None:
        return None
    # Cast to float before formatting: SQLite round-trips REAL columns as
    # float (15.0) but fresh JSON gives whole-number prices as int (15) —
    # without this, "15.0-30.0" vs "15-30" reads as a change when it isn't.
    return f"{float(price_min)}-{float(price_max)}"


def _iso_or_none(value) -> str | None:
    return value.isoformat() if value else None


def _to_json(value: dict) -> str:
    return json.dumps(value)
