from datetime import UTC, datetime, timedelta

import pytest

from events_agent.db import get_connection, init_db, upsert_raw_event
from events_agent.delivery.status import (
    GoogleAlertStat,
    ModelUsageStat,
    PeriodTotals,
    SourceStat,
    StatusReport,
    build_status_html,
    build_status_plain,
    build_status_report,
)
from events_agent.models import RawEvent


def make_report(**overrides) -> StatusReport:
    defaults = dict(
        lookback_days=7,
        generated_at=datetime(2026, 8, 31, 14, 0, tzinfo=UTC),
        periods=[
            PeriodTotals("This week", llm_calls=6, llm_cost_usd=0.18, llm_any_unpriced=False,
                          source_calls_ok=61, source_calls_failed=0, source_rows_fetched=99097, emails_sent=2),
            PeriodTotals("This month", llm_calls=6, llm_cost_usd=0.18, llm_any_unpriced=False,
                          source_calls_ok=61, source_calls_failed=0, source_rows_fetched=99097, emails_sent=2),
            PeriodTotals("All-time", llm_calls=6, llm_cost_usd=0.18, llm_any_unpriced=False,
                          source_calls_ok=61, source_calls_failed=0, source_rows_fetched=99097, emails_sent=2),
        ],
        sources=[SourceStat("skiddle", ok_runs=5, failed_runs=0, rows_fetched=1234)],
        usage_by_context=[("Milngavie", 5)],
        models=[
            ModelUsageStat(
                "claude-sonnet-5", calls=5, input_tokens=30000, output_tokens=11000,
                cache_creation_tokens=0, cache_read_tokens=16000, estimated_cost_usd=0.18,
            )
        ],
        total_estimated_cost_usd=0.18,
        any_unpriced_model=False,
        event_count=7602,
        events_by_status=[("on_sale", 6000), ("past", 1000), ("cancelled", 602)],
        total_household_slots=2,
        emails_by_type=[("shortlist", 1), ("last_call", 1)],
        google_alerts=[
            GoogleAlertStat("google_alerts_cameron_house", days_running=6, total_events=1, last_event_days_ago=3),
            GoogleAlertStat("google_alerts_crieff_hydro", days_running=6, total_events=0, last_event_days_ago=None),
        ],
        venue_count=598,
        household_count=1,
        scored_count=3247,
        db_size_bytes=110_400_000,
    )
    defaults.update(overrides)
    return StatusReport(**defaults)


def test_html_never_double_escapes_the_middot_separator():
    # Regression for the 2026-08-31 bug: _stat_row_html used to run
    # html.escape() over pre-built values that already contained the
    # literal "&middot;" entity, turning it into "&amp;middot;" -- which
    # every mail client then renders back out as the literal on-screen text
    # "&middot;" instead of a middot character.
    html_out = build_status_html(make_report())
    assert "&amp;middot;" not in html_out


def test_html_renders_source_and_model_stats():
    html_out = build_status_html(make_report())
    assert "skiddle" in html_out
    assert "1,234 rows" in html_out or "1234 rows" in html_out
    assert "claude-sonnet-5" in html_out
    assert "$0.18" in html_out


def test_html_handles_empty_sections_without_a_dangling_colon():
    html_out = build_status_html(make_report(models=[], total_estimated_cost_usd=0.0))
    assert "(none)" in html_out
    assert "(none):" not in html_out


def test_html_handles_no_source_runs_in_window():
    html_out = build_status_html(make_report(sources=[]))
    assert "No source runs in this window." in html_out


def test_plain_report_includes_cost_and_source_totals():
    plain = build_status_plain(make_report())
    assert "skiddle: 5 ok, 0 failed, 1234 rows" in plain
    assert "TOTAL estimated spend: ~$0.18" in plain


def test_html_renders_all_three_period_totals():
    html_out = build_status_html(make_report())
    assert "This week" in html_out
    assert "This month" in html_out
    assert "All-time" in html_out


def test_plain_renders_all_three_period_totals():
    plain = build_status_plain(make_report())
    assert "This week: LLM spend ~$0.18 across 6 calls" in plain
    assert "This month: LLM spend ~$0.18 across 6 calls" in plain
    assert "All-time: LLM spend ~$0.18 across 6 calls" in plain


def test_period_totals_survive_zero_calls_without_error():
    report = make_report(
        periods=[
            PeriodTotals("This week", llm_calls=0, llm_cost_usd=0.0, llm_any_unpriced=False,
                         source_calls_ok=0, source_calls_failed=0, source_rows_fetched=0),
        ]
    )
    html_out = build_status_html(report)
    plain = build_status_plain(report)
    assert "0 calls" in html_out
    assert "0 calls" in plain


def test_unpriced_model_reports_tokens_without_a_wrong_dollar_figure():
    report = make_report(
        models=[
            ModelUsageStat(
                "claude-opus-5", calls=2, input_tokens=1000, output_tokens=500,
                cache_creation_tokens=0, cache_read_tokens=0, estimated_cost_usd=None,
            )
        ],
        any_unpriced_model=True,
    )
    html_out = build_status_html(report)
    plain = build_status_plain(report)
    assert "cost unknown" in html_out
    assert "cost unknown" in plain
    assert "no pricing on file" in html_out


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "test.db"
    init_db(db_path)
    connection = get_connection(db_path)
    yield connection
    connection.close()


def test_period_windows_correctly_include_and_exclude_rows_by_age(conn, tmp_path):
    # One llm_usage row each: 2 days old (in every window), 20 days old (in
    # month + all-time, not week), 90 days old (all-time only). Real
    # regression target for the SQL windowing itself, not just rendering.
    now = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
    ages_days = {"recent": 2, "mid": 20, "old": 90}
    for label, age in ages_days.items():
        created_at = (now - timedelta(days=age)).isoformat()
        conn.execute(
            """
            INSERT INTO llm_usage (context, model, input_tokens, output_tokens,
                cache_creation_input_tokens, cache_read_input_tokens, created_at)
            VALUES (?, 'claude-sonnet-5', 1000, 100, 0, 0, ?)
            """,
            (label, created_at),
        )
        conn.execute(
            "INSERT INTO source_run (source_name, started_at, status, rows_fetched) VALUES (?, ?, 'ok', 10)",
            (label, created_at),
        )
    conn.commit()

    report = build_status_report(conn, tmp_path / "test.db", lookback_days=7, now=now)
    by_label = {p.label: p for p in report.periods}

    assert by_label["This week"].llm_calls == 1  # only "recent"
    assert by_label["This month"].llm_calls == 2  # "recent" + "mid"
    assert by_label["All-time"].llm_calls == 3  # all three

    assert by_label["This week"].source_calls_ok == 1
    assert by_label["This month"].source_calls_ok == 2
    assert by_label["All-time"].source_calls_ok == 3


def test_html_renders_emails_sent_and_household_slots():
    html_out = build_status_html(make_report())
    assert "Emails sent" in html_out
    assert "1 of 2 defined slots" in html_out
    assert "shortlist" in html_out
    assert "6,000 on_sale" in html_out or "6000 on_sale" in html_out


def test_plain_renders_emails_sent_and_household_slots():
    plain = build_status_plain(make_report())
    assert "emails sent" in plain
    assert "households configured: 1 of 2 defined slots" in plain
    assert "shortlist: 1" in plain


def test_email_totals_reflect_real_email_log_rows(conn, tmp_path):
    from events_agent.db import record_email_sent

    now = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    record_email_sent(conn, "Milngavie", "shortlist", "scott@example.com", 480, (now - timedelta(days=1)).isoformat())
    record_email_sent(conn, "Milngavie", "last_call", "scott@example.com", 3, (now - timedelta(days=1)).isoformat())
    record_email_sent(conn, None, "admin_stats", "admin@example.com", None, (now - timedelta(days=60)).isoformat())
    conn.commit()

    report = build_status_report(conn, tmp_path / "test.db", lookback_days=7, now=now)
    by_label = {p.label: p for p in report.periods}

    assert by_label["This week"].emails_sent == 2  # the 60-day-old admin_stats row is outside this window
    assert by_label["All-time"].emails_sent == 3
    assert dict(report.emails_by_type) == {"shortlist": 1, "last_call": 1}  # last-7-days breakdown only


def test_html_renders_google_alert_yield_dead_and_live_feeds():
    html_out = build_status_html(make_report())
    assert "cameron_house" in html_out
    assert "1 event ever" in html_out
    assert "crieff_hydro" in html_out
    assert "0 events ever" in html_out
    assert "&amp;middot;" not in html_out  # same double-escaping regression as _stat_row_html generally


def test_plain_renders_google_alert_yield_dead_and_live_feeds():
    plain = build_status_plain(make_report())
    assert "cameron_house: 1 event(s) ever, running 6d, last 3d ago" in plain
    assert "crieff_hydro: 0 events ever, running 6d" in plain


def test_google_alert_stats_computed_from_real_source_run_and_event_source_rows(conn, tmp_path):
    now = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    first_run = (now - timedelta(days=6)).isoformat()
    conn.execute(
        "INSERT INTO source_run (source_name, started_at, status, rows_fetched) VALUES (?, ?, 'ok', 0)",
        ("google_alerts_dead_feed", first_run),
    )
    conn.execute(
        "INSERT INTO source_run (source_name, started_at, status, rows_fetched) VALUES (?, ?, 'ok', 1)",
        ("google_alerts_live_feed", first_run),
    )
    conn.commit()

    event_id, _ = upsert_raw_event(
        conn,
        RawEvent(
            source_name="google_alerts_live_feed", source_event_id="1",
            title="Spa Deal", category=None, venue_name="Various — Wowcher Spa Deals",
            status="announced", event_date=None,
        ),
    )
    # first_seen is set to the real wall-clock time by upsert_raw_event, not
    # controllable at insert time -- pin it explicitly so this test doesn't
    # depend on what time of day it happens to run.
    conn.execute(
        "UPDATE event SET first_seen = ? WHERE id = ?", ((now - timedelta(days=2)).isoformat(), event_id)
    )
    conn.commit()

    report = build_status_report(conn, tmp_path / "test.db", lookback_days=7, now=now)
    by_name = {a.name: a for a in report.google_alerts}

    assert by_name["google_alerts_dead_feed"].total_events == 0
    assert by_name["google_alerts_dead_feed"].last_event_days_ago is None
    assert by_name["google_alerts_live_feed"].total_events == 1
    assert by_name["google_alerts_live_feed"].last_event_days_ago == 2
    assert by_name["google_alerts_live_feed"].days_running == 6
