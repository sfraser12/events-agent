"""events-agent harvest, end to end against a temp DB. Skiddle is faked from a fixture."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from events_agent import cli
from events_agent.config import Config, Constraints, Delivery, Home, Horizons, Scoring, Search, Secrets
from events_agent.db import get_connection, init_db, upsert_raw_event
from events_agent.models import RawEvent
from events_agent.sources.skiddle import SkiddleAdapter
from events_agent.sources.ticketmaster import TicketmasterAdapter

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


class FakeTicketmasterAdapter:
    """Stands in for TicketmasterAdapter — yields RawEvents parsed from a real saved fixture."""

    name = "ticketmaster"

    def __init__(self, **kwargs):
        pass

    def fetch(self, since=None):
        parser = TicketmasterAdapter(api_key="x", latitude=0, longitude=0, radius_miles=25)
        with (FIXTURES / "ticketmaster_search_variety.json").open() as f:
            raw_results = json.load(f)["_embedded"]["events"]
        for raw in raw_results:
            yield parser._parse_event(raw)


def make_failing_adapter(name: str):
    """A source class whose fetch() always raises, for the one-source-down tests."""

    class FailingAdapter:
        def __init__(self, **kwargs):
            pass

        def fetch(self, since=None):
            raise RuntimeError("simulated API outage")
            yield  # pragma: no cover — makes this a generator function

    FailingAdapter.name = name
    return FailingAdapter


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
            delivery=Delivery(email_to="test@example.com"),
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
    assert "skiddle: 5 events (5 new, 0 updated)" in out


def test_harvest_is_idempotent_on_rerun(wired_cli, capsys):
    cli.cmd_harvest(argparse_namespace())
    capsys.readouterr()  # discard first run's output
    cli.cmd_harvest(argparse_namespace())

    conn = get_connection(wired_cli)
    count = conn.execute("SELECT COUNT(*) FROM event").fetchone()[0]
    conn.close()
    assert count == 5

    out = capsys.readouterr().out
    assert "skiddle: 5 events (0 new, 5 updated)" in out


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


@pytest.fixture
def wired_cli_two_sources(wired_cli, monkeypatch):
    monkeypatch.setattr(cli, "TicketmasterAdapter", FakeTicketmasterAdapter)
    monkeypatch.setattr(
        cli, "load_secrets", lambda: Secrets(skiddle_api_key="test-key", ticketmaster_api_key="test-tm-key")
    )
    return wired_cli


def test_harvest_aggregates_both_sources(wired_cli_two_sources):
    exit_code = cli.cmd_harvest(argparse_namespace())

    assert exit_code == 0
    conn = get_connection(wired_cli_two_sources)
    source_names = {
        row[0] for row in conn.execute("SELECT DISTINCT source_name FROM event_source").fetchall()
    }
    conn.close()
    assert source_names == {"skiddle", "ticketmaster"}


def test_harvest_one_source_failing_does_not_block_the_other(wired_cli, monkeypatch, capsys):
    monkeypatch.setattr(cli, "TicketmasterAdapter", make_failing_adapter("ticketmaster"))
    monkeypatch.setattr(
        cli, "load_secrets", lambda: Secrets(skiddle_api_key="test-key", ticketmaster_api_key="test-tm-key")
    )

    exit_code = cli.cmd_harvest(argparse_namespace())

    assert exit_code == 0  # skiddle succeeded, so the command as a whole did not fail
    conn = get_connection(wired_cli)
    rows = conn.execute("SELECT source_name, status, error FROM source_run ORDER BY id").fetchall()
    conn.close()
    assert ("skiddle", "ok", None) in rows
    failed = [r for r in rows if r[0] == "ticketmaster"]
    assert failed[0][1] == "failed"
    assert "simulated API outage" in failed[0][2]

    err = capsys.readouterr().err
    assert "ticketmaster: FAILED" in err


def test_harvest_fails_only_if_every_source_fails(wired_cli, monkeypatch):
    monkeypatch.setattr(cli, "SkiddleAdapter", make_failing_adapter("skiddle"))
    monkeypatch.setattr(cli, "TicketmasterAdapter", make_failing_adapter("ticketmaster"))
    monkeypatch.setattr(
        cli, "load_secrets", lambda: Secrets(skiddle_api_key="test-key", ticketmaster_api_key="test-tm-key")
    )

    exit_code = cli.cmd_harvest(argparse_namespace())

    assert exit_code == 1


def test_init_seeds_household_from_config(wired_cli):
    cli.cmd_init(argparse_namespace())

    conn = get_connection(wired_cli)
    row = conn.execute(
        "SELECT label, home_latitude, radius_miles, digest_threshold, email_to FROM household WHERE id = 1"
    ).fetchone()
    conn.close()
    assert row == ("Milngavie", 55.9410, 25, 60, "test@example.com")


def test_alert_requires_a_household(wired_cli, capsys):
    # wired_cli's init_db creates the schema but doesn't seed a household —
    # only cmd_init does that, and this test deliberately hasn't run it.
    exit_code = cli.cmd_alert(argparse_namespace())

    assert exit_code == 1
    assert "No household configured" in capsys.readouterr().err


def test_score_fails_loudly_without_api_key(wired_cli, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_secrets", lambda: Secrets(skiddle_api_key="test-key", anthropic_api_key=""))

    exit_code = cli.cmd_score(argparse_namespace())

    assert exit_code == 1
    assert "ANTHROPIC_API_KEY not set" in capsys.readouterr().err


def test_calendar_command_writes_only_shortlisted_events(wired_cli, tmp_path):
    cli.cmd_init(argparse_namespace())  # seeds household 1
    conn = get_connection(wired_cli)
    keep_id, _ = upsert_raw_event(
        conn,
        RawEvent(
            source_name="skiddle",
            source_event_id="1",
            title="Shortlisted Gig",
            category="music",
            venue_name="Test Venue",
            event_date=datetime(2026, 9, 10, 19, 0, tzinfo=UTC),
        ),
    )
    skip_id, _ = upsert_raw_event(
        conn,
        RawEvent(
            source_name="skiddle",
            source_event_id="2",
            title="Undecided Gig",
            category="music",
            venue_name="Test Venue",
            event_date=datetime(2026, 9, 11, 19, 0, tzinfo=UTC),
        ),
    )
    conn.commit()
    conn.close()
    cli.cmd_verdict(verdict_namespace(keep_id, "interested"))

    out_path = tmp_path / "out.ics"
    exit_code = cli.cmd_calendar(calendar_namespace(out_path))

    assert exit_code == 0
    text = out_path.read_text()
    assert "SUMMARY:Shortlisted Gig" in text
    assert "Undecided Gig" not in text


def test_verdict_command_writes_to_household_event_state(wired_cli):
    cli.cmd_init(argparse_namespace())  # seeds household 1
    conn = get_connection(wired_cli)
    event_id, _ = upsert_raw_event(
        conn,
        RawEvent(
            source_name="skiddle",
            source_event_id="1",
            title="Test Gig",
            category="music",
            venue_name="Test Venue",
        ),
    )
    conn.commit()
    conn.close()

    exit_code = cli.cmd_verdict(verdict_namespace(event_id, "no"))

    assert exit_code == 0
    conn = get_connection(wired_cli)
    verdict = conn.execute(
        "SELECT verdict FROM household_event_state WHERE household_id = 1 AND event_id = ?", (event_id,)
    ).fetchone()[0]
    conn.close()
    assert verdict == "no"


def argparse_namespace():
    import argparse

    return argparse.Namespace()


def verdict_namespace(event_id: int, verdict: str, household: int = 1):
    import argparse

    return argparse.Namespace(event_id=event_id, verdict=verdict, household=household)


def calendar_namespace(out: Path | None = None):
    import argparse

    return argparse.Namespace(out=out)
