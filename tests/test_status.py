from datetime import UTC, datetime

from events_agent.delivery.status import (
    ModelUsageStat,
    SourceStat,
    StatusReport,
    build_status_html,
    build_status_plain,
)


def make_report(**overrides) -> StatusReport:
    defaults = dict(
        lookback_days=7,
        generated_at=datetime(2026, 8, 31, 14, 0, tzinfo=UTC),
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
