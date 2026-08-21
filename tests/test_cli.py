"""events-agent harvest, end to end against a temp DB. Skiddle is faked from a fixture."""

import json
from pathlib import Path

import pytest

from events_agent import cli
from events_agent.config import Config, Constraints, Delivery, Home, Horizons, Scoring, Search, Secrets
from events_agent.db import get_connection, init_db
from events_agent.sources.skiddle import SkiddleAdapter

FIXTURES = Path(__file__).parent / "fixtures"


class FakeSkiddleAdapter:
    """Stands in for SkiddleAdapter — yields RawEvents parsed from a real saved fixture."""

    name = "skiddle"

    def __init__(self, **kwargs):
        pass

    def fetch(self, since=None):
        parser = SkiddleAdapter(api_key="x", latitude=0, longitude=0, radius_miles=25)
        with (FIXTURES / "skiddle_search_theatre_offset0.json").open() as f:
            raw_results = json.load(f)["results"]
        for raw in raw_results:
            yield parser._parse_event(raw, "THEATRE")


@pytest.fixture
def wired_cli(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    init_db(db_path)
    monkeypatch.setattr(cli, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(cli, "SkiddleAdapter", FakeSkiddleAdapter)
    monkeypatch.setattr(
        cli,
        "load_config",
        lambda: Config(
            home=Home(latitude=55.9410, longitude=-4.3170, label="Milngavie"),
            search=Search(radius_miles=25, horizons=Horizons(near_days=7, month_days=31, long_days=270)),
            constraints=Constraints(max_drive_minutes=60, price_ceiling=120),
            scoring=Scoring(digest_threshold=60, alert_threshold=45),
            delivery=Delivery(digest_day="sunday", digest_hour=8, email_to="test@example.com"),
        ),
    )
    monkeypatch.setattr(cli, "load_secrets", lambda: Secrets(skiddle_api_key="test-key"))
    return db_path


def test_harvest_upserts_events_and_prints_table(wired_cli, capsys):
    exit_code = cli.cmd_harvest(argparse_namespace())

    assert exit_code == 0
    conn = get_connection(wired_cli)
    count = conn.execute("SELECT COUNT(*) FROM event").fetchone()[0]
    conn.close()
    assert count == 5

    out = capsys.readouterr().out
    assert "The Hush Club" in out
    assert "5 events from skiddle (5 new, 0 updated)." in out


def test_harvest_is_idempotent_on_rerun(wired_cli, capsys):
    cli.cmd_harvest(argparse_namespace())
    capsys.readouterr()  # discard first run's output
    cli.cmd_harvest(argparse_namespace())

    conn = get_connection(wired_cli)
    count = conn.execute("SELECT COUNT(*) FROM event").fetchone()[0]
    conn.close()
    assert count == 5

    out = capsys.readouterr().out
    assert "5 events from skiddle (0 new, 5 updated)." in out


def test_harvest_logs_source_run(wired_cli):
    cli.cmd_harvest(argparse_namespace())

    conn = get_connection(wired_cli)
    row = conn.execute("SELECT source_name, status, rows_fetched FROM source_run").fetchone()
    conn.close()
    assert row == ("skiddle", "ok", 5)


def test_harvest_fails_loudly_without_api_key(wired_cli, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_secrets", lambda: Secrets(skiddle_api_key=""))

    exit_code = cli.cmd_harvest(argparse_namespace())

    assert exit_code == 1
    assert "SKIDDLE_API_KEY not set" in capsys.readouterr().err


def argparse_namespace():
    import argparse

    return argparse.Namespace()
