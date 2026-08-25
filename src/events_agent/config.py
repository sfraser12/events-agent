"""Config loading: config.yaml (checked in) + .env (secrets, gitignored)."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config.yaml"
DEFAULT_DB_PATH = REPO_ROOT / "events.db"


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


class Delivery(BaseModel):
    digest_day: str
    digest_hour: int
    email_to: str


class Config(BaseModel):
    home: Home
    search: Search
    constraints: Constraints
    scoring: Scoring
    delivery: Delivery


class Secrets(BaseModel):
    ticketmaster_api_key: str = ""
    skiddle_api_key: str = ""
    tmdb_api_key: str = ""
    anthropic_api_key: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""


def load_config(path: Path | None = None) -> Config:
    config_path = path or DEFAULT_CONFIG_PATH
    if not config_path.exists():
        raise FileNotFoundError(
            f"{config_path} not found. Copy config.example.yaml to config.yaml and fill it in."
        )
    with config_path.open() as f:
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
    )
