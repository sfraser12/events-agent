"""SQLite schema and connection helper. Plain sqlite3 — no ORM."""

from __future__ import annotations

import sqlite3
from pathlib import Path

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
