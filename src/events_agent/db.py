"""SQLite schema, connection helper, and upsert logic. Plain sqlite3 — no ORM."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from events_agent.models import RawEvent
from events_agent.normalise import fingerprint, normalise_text

SCHEMA = """
CREATE TABLE IF NOT EXISTS venue (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    name_normalised TEXT NOT NULL UNIQUE,
    city            TEXT,
    postcode        TEXT,
    latitude        REAL,
    longitude       REAL,
    drive_minutes   INTEGER
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
    last_seen       TEXT NOT NULL,

    score           INTEGER,
    audience        TEXT,
    score_reason    TEXT,
    urgency         TEXT,
    scored_at       TEXT,

    verdict         TEXT,
    verdict_at      TEXT,
    snoozed_until   TEXT,
    surfaced_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_event_fingerprint ON event(fingerprint);
CREATE INDEX IF NOT EXISTS idx_event_date        ON event(event_date);
CREATE INDEX IF NOT EXISTS idx_event_onsale      ON event(on_sale_date);
CREATE INDEX IF NOT EXISTS idx_event_verdict     ON event(verdict);

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
    detected_at     TEXT NOT NULL
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
        conn.commit()
    finally:
        conn.close()


def upsert_venue(
    conn: sqlite3.Connection,
    name: str,
    city: str | None,
    postcode: str | None,
    latitude: float | None,
    longitude: float | None,
) -> int:
    name_normalised = normalise_text(name)
    row = conn.execute(
        "SELECT id FROM venue WHERE name_normalised = ?", (name_normalised,)
    ).fetchone()
    if row:
        venue_id = row[0]
        conn.execute(
            "UPDATE venue SET name = ?, city = ?, postcode = ?, latitude = ?, longitude = ? WHERE id = ?",
            (name, city, postcode, latitude, longitude, venue_id),
        )
        return venue_id

    cur = conn.execute(
        "INSERT INTO venue (name, name_normalised, city, postcode, latitude, longitude) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (name, name_normalised, city, postcode, latitude, longitude),
    )
    return cur.lastrowid


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
    )

    title_normalised = normalise_text(raw.title)
    fp = fingerprint(raw.title, raw.venue_name, raw.event_date)

    row = conn.execute("SELECT id FROM event WHERE fingerprint = ?", (fp,)).fetchone()
    if row:
        event_id = row[0]
        created = False
        conn.execute(
            """
            UPDATE event SET
                title = ?, title_normalised = ?, category = ?, venue_id = ?,
                event_date = ?, event_date_end = ?, status = ?,
                price_min = ?, price_max = ?, currency = ?, url = ?, blurb = ?,
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
                raw.url,
                raw.blurb,
                now,
                event_id,
            ),
        )
    else:
        created = True
        cur = conn.execute(
            """
            INSERT INTO event (
                fingerprint, title, title_normalised, category, venue_id,
                event_date, event_date_end, status,
                price_min, price_max, currency, url, blurb,
                first_seen, last_seen
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                raw.url,
                raw.blurb,
                now,
                now,
            ),
        )
        event_id = cur.lastrowid

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


def _iso_or_none(value) -> str | None:
    return value.isoformat() if value else None


def _to_json(value: dict) -> str:
    return json.dumps(value)
