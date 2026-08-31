"""Config loading: config.yaml (checked in) + .env (secrets, gitignored)."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = REPO_ROOT / "events.db"

# One subdirectory per household (households/scott/, households/brother/),
# each with its own config.yaml + taste-profile.md. Replaces the old
# single-file config.yaml/taste-profile.md at the repo root now that a
# second household actually exists.
HOUSEHOLDS_DIR = REPO_ROOT / "households"


class Horizons(BaseModel):
    near_days: int
    month_days: int
    long_days: int


class Home(BaseModel):
    latitude: float
    longitude: float
    label: str


class Search(BaseModel):
    radius_miles: float
    horizons: Horizons
    # "Worth a special trip" tier — optional, NULL/absent means off. An event
    # beyond radius_miles but within far_radius_miles isn't hard-rejected; it
    # still reaches scoring, but only surfaces in the digest if it also
    # clears Scoring.far_threshold. See constraints.py.
    far_radius_miles: float | None = None
    # A plain circle is the wrong shape for "the rest of Scotland" (Skye is
    # roughly as far from Glasgow as North West England is) -- this is a
    # rough "north of about here" latitude floor applied only within the far
    # tier, so far_radius_miles points at Scotland rather than Blackpool.
    # See constraints.py for the full reasoning.
    far_min_latitude: float | None = None


class BlackoutRange(BaseModel):
    start: date
    end: date


class Constraints(BaseModel):
    max_drive_minutes: int
    price_ceiling: float
    # A single date can't express "12-26 October" — blackout periods in
    # taste-profile.md are always ranges, so this is too (was list[str] of
    # single dates, but nothing had ever populated it, so no migration risk
    # in fixing the shape now, before Phase 4's constraint filter needs it).
    blackout_dates: list[BlackoutRange] = []


class Scoring(BaseModel):
    digest_threshold: int
    alert_threshold: int
    # The score bar a far-flung event (see Search.far_radius_miles) must
    # clear before it can surface at all — deliberately higher than
    # digest_threshold, since "great, but 3 hours away" shouldn't clutter
    # the digest the way "great, 20 minutes away" does.
    far_threshold: int | None = None


class Delivery(BaseModel):
    # Cadence (when digest/alert actually run) lives in the launchd plist,
    # not here — duplicating it in config.yaml would just let the two drift
    # out of sync. This is only who the digest goes to.
    email_to: str


class GoogleAlertFeed(BaseModel):
    """One Google Alert, set up manually (no public API to create one — see
    sources/google_alerts.py) and configured with the venue it covers.
    latitude/longitude matter: db.upsert_venue() unconditionally overwrites
    an existing venue's coordinates on an exact name match, so an entry
    missing them could silently null out a venue's real coordinates from
    Ticketmaster/Skiddle. feed_url left blank means "alert not created yet"
    — cmd_harvest skips it rather than failing the whole harvest."""

    venue_name: str
    feed_url: str = ""
    latitude: float | None = None
    longitude: float | None = None


class Config(BaseModel):
    home: Home
    search: Search
    constraints: Constraints
    scoring: Scoring
    delivery: Delivery
    google_alerts: list[GoogleAlertFeed] = []


class Secrets(BaseModel):
    ticketmaster_api_key: str = ""
    skiddle_api_key: str = ""
    tmdb_api_key: str = ""
    anthropic_api_key: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    # Where the admin stats/cost email goes — deliberately an operator-level
    # setting, not tied to any one household's config, since it should never
    # multiply the moment a second household exists. Falls back to smtp_user
    # (the sending account) so it works without a fresh .env edit.
    admin_email: str = ""


def load_config(path: Path) -> Config:
    if not path.exists():
        raise FileNotFoundError(f"{path} not found. Copy config.example.yaml to {path} and fill it in.")
    with path.open() as f:
        raw = yaml.safe_load(f)
    return Config.model_validate(raw)


def load_secrets(env_path: Path | None = None) -> Secrets:
    load_dotenv(env_path or REPO_ROOT / ".env")
    return Secrets(
        ticketmaster_api_key=os.environ.get("TICKETMASTER_API_KEY", ""),
        skiddle_api_key=os.environ.get("SKIDDLE_API_KEY", ""),
        tmdb_api_key=os.environ.get("TMDB_API_KEY", ""),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        smtp_host=os.environ.get("SMTP_HOST", ""),
        smtp_port=int(os.environ.get("SMTP_PORT", "587")),
        smtp_user=os.environ.get("SMTP_USER", ""),
        smtp_password=os.environ.get("SMTP_PASSWORD", ""),
        admin_email=os.environ.get("ADMIN_EMAIL", ""),
    )
