from datetime import UTC, datetime, timedelta

import pytest

from events_agent.db import get_connection, init_db
from events_agent.delivery.status import (
    ModelUsageStat,
    PeriodTotals,
    SourceStat,
    StatusReport,
    build_status_html,
    build_status_plain,
    build_status_report,
)


def make_report(**overrides) -> StatusReport:
    defaults = dict(
        lookback_days=7,
        generated_at=datetime(2026, 8, 31, 14, 0, tzinfo=UTC),
        periods=[
            PeriodTotals("This week", llm_calls=6, llm_cost_usd=0.18, llm_any_unpriced=False,
                          source_calls_ok=61, source_calls_failed=0, source_rows_fetched=99097),
            PeriodTotals("This month", llm_calls=6, llm_cost_usd=0.18, llm_any_unpriced=False,
                          source_calls_ok=61, source_calls_failed=0, source_rows_fetched=99097),
            PeriodTotals("All-time", llm_calls=6, llm_cost_usd=0.18, llm_any_unpriced=False,
                          source_calls_ok=61, source_calls_failed=0, source_rows_fetched=99097),
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
