"""events-agent harvest, end to end against a temp DB. Skiddle is faked from a fixture."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from events_agent import cli
from events_agent.config import Config, Constraints, Delivery, Home, Horizons, Scoring, Search, Secrets
from events_agent.db import get_connection, init_db, upsert_household, upsert_raw_event
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

    # Only "scott" gets real files on disk -- mirrors production, where
    # cmd_init must skip a household whose households/<name>/ files aren't
    # there yet rather than failing the whole run.
    households_dir = tmp_path / "households"
    scott_dir = households_dir / "scott"
    scott_dir.mkdir(parents=True)
    (scott_dir / "config.yaml").write_text("placeholder — load_config is monkeypatched below")
    (scott_dir / "taste-profile.md").write_text("placeholder")
    monkeypatch.setattr(cli, "HOUSEHOLDS_DIR", households_dir)

    monkeypatch.setattr(
        cli,
        "load_config",
        lambda *_args, **_kwargs: Config(
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


def test_harvest_loops_over_every_configured_household(wired_cli_two_sources, monkeypatch):
    # Second household: "brother" gets real files on disk too, alongside
    # "scott" (wired_cli already created that one). Confirms harvest no
    # longer reads only Scott's config -- both households' own radius/home
    # should each get a Skiddle + Ticketmaster pass, distinguishable by the
    # per-household adapter name suffix.
    households_dir = cli.HOUSEHOLDS_DIR
    brother_dir = households_dir / "brother"
    brother_dir.mkdir(parents=True)
    (brother_dir / "config.yaml").write_text("placeholder — load_config is monkeypatched below")
    (brother_dir / "taste-profile.md").write_text("placeholder")

    def fake_load_config(path):
        is_brother = "brother" in str(path)
        return Config(
            home=Home(latitude=55.9410, longitude=-4.3170, label="Milngavie"),
            search=Search(radius_miles=50 if is_brother else 25, horizons=Horizons(near_days=7, month_days=31, long_days=270)),
            constraints=Constraints(max_drive_minutes=60, price_ceiling=120),
            scoring=Scoring(digest_threshold=60, alert_threshold=45),
            delivery=Delivery(email_to="test@example.com"),
        )

    monkeypatch.setattr(cli, "load_config", fake_load_config)

    exit_code = cli.cmd_harvest(argparse_namespace())

    assert exit_code == 0
    conn = get_connection(wired_cli_two_sources)
    source_names = {row[0] for row in conn.execute("SELECT DISTINCT source_name FROM source_run").fetchall()}
    conn.close()
    assert source_names == {"skiddle_scott", "skiddle_brother", "ticketmaster_scott", "ticketmaster_brother"}


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


def test_init_skips_household_whose_files_are_not_present_yet(wired_cli, capsys):
    # wired_cli only puts real files under households/scott/ -- households/
    # brother/ doesn't exist at all, same as before he's sent his config.
    exit_code = cli.cmd_init(argparse_namespace())

    assert exit_code == 0  # not a failure -- the rest of the run still works
    conn = get_connection(wired_cli)
    count = conn.execute("SELECT COUNT(*) FROM household").fetchone()[0]
    conn.close()
    assert count == 1  # only scott seeded

    err = capsys.readouterr().err
    assert "households/brother/config.yaml and taste-profile.md not both present yet" in err


def test_init_seeds_second_household_once_its_files_exist(wired_cli):
    brother_dir = cli.HOUSEHOLDS_DIR / "brother"
    brother_dir.mkdir(parents=True)
    (brother_dir / "config.yaml").write_text("placeholder")
    (brother_dir / "taste-profile.md").write_text("placeholder")

    cli.cmd_init(argparse_namespace())

    conn = get_connection(wired_cli)
    ids = {row[0] for row in conn.execute("SELECT id FROM household").fetchall()}
    conn.close()
    assert ids == {1, 2}  # stable ids from HOUSEHOLD_IDS, not autoincrement order


def test_alert_requires_a_household(wired_cli, capsys):
    # wired_cli's init_db creates the schema but doesn't seed a household —
    # only cmd_init does that, and this test deliberately hasn't run it.
    exit_code = cli.cmd_alert(argparse_namespace())

    assert exit_code == 1
    assert "No household configured" in capsys.readouterr().err


def test_run_chains_harvest_score_alert_and_continues_on_failure(wired_cli, capsys):
    # wired_cli's secrets have no ANTHROPIC_API_KEY, so the score step fails —
    # run should still attempt alert rather than stopping short (fail loudly,
    # degrade gracefully: a broken step shouldn't suppress the others).
    cli.cmd_init(argparse_namespace())
    capsys.readouterr()  # discard init's output

    exit_code = cli.cmd_run(argparse_namespace())

    out, err = capsys.readouterr()
    assert exit_code == 1  # non-zero because the score step failed
    assert "ANTHROPIC_API_KEY not set" in err
    assert "run: 'score' step failed" in err
    assert "no urgent alerts" in out.lower()  # alert step still ran despite the score failure


def test_fortnight_requires_a_household(wired_cli, capsys):
    exit_code = cli.cmd_fortnight(argparse_namespace())

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


def test_calendar_command_writes_separate_files_per_household(wired_cli, monkeypatch, tmp_path):
    # Regression test: with args.out unset, writing every household's .ics
    # to the same default path would silently overwrite the first
    # household's file with the second's.
    cli.cmd_init(argparse_namespace())  # seeds household 1 ("Milngavie")
    conn = get_connection(wired_cli)
    upsert_household(
        conn,
        2,
        label="Edinburgh",
        home_latitude=55.9533,
        home_longitude=-3.1883,
        radius_miles=25,
        near_days=7,
        month_days=31,
        max_drive_minutes=60,
        price_ceiling=120,
        blackout_dates=[],
        taste_profile_path="unused",
        digest_threshold=60,
        alert_threshold=45,
        email_to="brother@example.com",
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    exit_code = cli.cmd_calendar(calendar_namespace())

    assert exit_code == 0
    written = sorted(p.name for p in tmp_path.glob("curtainup-*.ics"))
    assert written == ["curtainup-edinburgh.ics", "curtainup-milngavie.ics"]


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


def test_vacuum_runs_without_error_and_preserves_data(wired_cli, capsys):
    cli.cmd_init(argparse_namespace())
    cli.cmd_harvest(argparse_namespace())
    capsys.readouterr()

    conn = get_connection(wired_cli)
    events_before = conn.execute("SELECT COUNT(*) FROM event").fetchone()[0]
    conn.close()

    exit_code = cli.cmd_vacuum(argparse_namespace())

    out, _ = capsys.readouterr()
    assert exit_code == 0
    assert "Vacuumed events.db" in out
    conn = get_connection(wired_cli)
    events_after = conn.execute("SELECT COUNT(*) FROM event").fetchone()[0]
    conn.close()
    assert events_after == events_before
