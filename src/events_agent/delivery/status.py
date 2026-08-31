"""Admin stats/cost email — not part of the Stage 4 household-facing set
(Shortlist/Understudy/Last Call). Answers "how is this scaling, what is it
costing, do we need to rearchitect" from data the pipeline already collects
(source_run) plus llm_usage, added 2026-08-31 specifically because nothing
persisted real LLM token/cost data before this — only per-run stdout counts.
Sent to Secrets.admin_email (falls back to smtp_user), never to a household.
"""

from __future__ import annotations

import html
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from events_agent.delivery.email_design import (
    ADMIN,
    ADMIN_BG,
    BORDER,
    INK,
    MUTED,
    SANS,
    SERIF,
    empty_row,
    shell,
)

# Anthropic first-party API rates, confirmed 2026-08-31 via the claude-api
# skill (cached pricing table dated 2026-06-24) — not guessed. cache_write/
# cache_read are the standard ~1.25x/~0.1x-of-input-price multipliers
# Anthropic publishes generically, not separately-quoted per-model figures.
# A model not listed here reports its token counts with no $ estimate rather
# than guessing at unknown pricing — update this table if pricing changes.
PRICING_PER_MTOK: dict[str, dict[str, float]] = {
    "claude-sonnet-5": {"input": 2.00, "output": 10.00, "cache_write": 2.50, "cache_read": 0.20},
}


@dataclass
class SourceStat:
    name: str
    ok_runs: int
    failed_runs: int
    rows_fetched: int


@dataclass
class ModelUsageStat:
    model: str
    calls: int
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    estimated_cost_usd: float | None


@dataclass
class PeriodTotals:
    """Headline totals over one window — This week (7d) / This month (30d,
    rolling, not calendar-month-to-date) / All-time (no cutoff). Deliberately
    just the two numbers that actually answer "how is this scaling, what
    does it cost" at a glance; the detailed per-source/per-model breakdown
    below stays at the 7-day window it's always been."""

    label: str
    llm_calls: int
    llm_cost_usd: float
    llm_any_unpriced: bool
    source_calls_ok: int
    source_calls_failed: int
    source_rows_fetched: int


@dataclass
class StatusReport:
    lookback_days: int
    generated_at: datetime
    periods: list[PeriodTotals] = field(default_factory=list)
    sources: list[SourceStat] = field(default_factory=list)
    usage_by_context: list[tuple[str, int]] = field(default_factory=list)
    models: list[ModelUsageStat] = field(default_factory=list)
    total_estimated_cost_usd: float = 0.0
    any_unpriced_model: bool = False
    event_count: int = 0
    venue_count: int = 0
    household_count: int = 0
    scored_count: int = 0
    db_size_bytes: int = 0


def _estimate_cost_usd(
    model: str, input_tokens: int, output_tokens: int, cache_creation_tokens: int, cache_read_tokens: int
) -> float | None:
    pricing = PRICING_PER_MTOK.get(model)
    if pricing is None:
        return None
    return (
        input_tokens / 1_000_000 * pricing["input"]
        + output_tokens / 1_000_000 * pricing["output"]
        + cache_creation_tokens / 1_000_000 * pricing["cache_write"]
        + cache_read_tokens / 1_000_000 * pricing["cache_read"]
    )


def _compute_period_totals(conn: sqlite3.Connection, label: str, cutoff_iso: str | None) -> PeriodTotals:
    """cutoff_iso=None means all-time (no WHERE clause) — SQLite has no
    NULL-safe ">=" shortcut worth relying on here, so branch on it directly
    rather than passing NULL through and hoping the comparison does the
    right thing."""
    if cutoff_iso is None:
        src_row = conn.execute(
            """
            SELECT SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END),
                   COALESCE(SUM(rows_fetched), 0)
            FROM source_run
            """
        ).fetchone()
        llm_rows = conn.execute(
            """
            SELECT model, COUNT(*),
                   COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0),
                   COALESCE(SUM(cache_creation_input_tokens), 0), COALESCE(SUM(cache_read_input_tokens), 0)
            FROM llm_usage
            GROUP BY model
            """
        ).fetchall()
    else:
        src_row = conn.execute(
            """
            SELECT SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END),
                   COALESCE(SUM(rows_fetched), 0)
            FROM source_run WHERE started_at >= ?
            """,
            (cutoff_iso,),
        ).fetchone()
        llm_rows = conn.execute(
            """
            SELECT model, COUNT(*),
                   COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0),
                   COALESCE(SUM(cache_creation_input_tokens), 0), COALESCE(SUM(cache_read_input_tokens), 0)
            FROM llm_usage WHERE created_at >= ?
            GROUP BY model
            """,
            (cutoff_iso,),
        ).fetchall()

    ok, failed, rows_fetched = (src_row[0] or 0, src_row[1] or 0, src_row[2] or 0)

    total_calls = 0
    total_cost = 0.0
    any_unpriced = False
    for model, calls, input_tokens, output_tokens, cache_creation, cache_read in llm_rows:
        total_calls += calls
        cost = _estimate_cost_usd(model, input_tokens, output_tokens, cache_creation, cache_read)
        if cost is None:
            any_unpriced = True
        else:
            total_cost += cost

    return PeriodTotals(label, total_calls, total_cost, any_unpriced, ok, failed, rows_fetched)


def build_status_report(
    conn: sqlite3.Connection, db_path: Path, lookback_days: int = 7, now: datetime | None = None
) -> StatusReport:
    now = now or datetime.now(UTC)
    cutoff = (now - timedelta(days=lookback_days)).isoformat()

    periods = [
        _compute_period_totals(conn, "This week", (now - timedelta(days=7)).isoformat()),
        _compute_period_totals(conn, "This month", (now - timedelta(days=30)).isoformat()),
        _compute_period_totals(conn, "All-time", None),
    ]

    source_rows = conn.execute(
        """
        SELECT source_name,
               SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END),
               SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END),
               COALESCE(SUM(rows_fetched), 0)
        FROM source_run
        WHERE started_at >= ?
        GROUP BY source_name
        ORDER BY source_name
        """,
        (cutoff,),
    ).fetchall()
    sources = [SourceStat(name, ok or 0, failed or 0, rows) for name, ok, failed, rows in source_rows]

    model_rows = conn.execute(
        """
        SELECT model, COUNT(*),
               COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0),
               COALESCE(SUM(cache_creation_input_tokens), 0), COALESCE(SUM(cache_read_input_tokens), 0)
        FROM llm_usage
        WHERE created_at >= ?
        GROUP BY model
        ORDER BY model
        """,
        (cutoff,),
    ).fetchall()
    models: list[ModelUsageStat] = []
    total_cost = 0.0
    any_unpriced = False
    for model, calls, input_tokens, output_tokens, cache_creation, cache_read in model_rows:
        cost = _estimate_cost_usd(model, input_tokens, output_tokens, cache_creation, cache_read)
        if cost is None:
            any_unpriced = True
        else:
            total_cost += cost
        models.append(ModelUsageStat(model, calls, input_tokens, output_tokens, cache_creation, cache_read, cost))

    context_rows = conn.execute(
        """
        SELECT context, COUNT(*) AS calls FROM llm_usage
        WHERE created_at >= ?
        GROUP BY context ORDER BY calls DESC
        """,
        (cutoff,),
    ).fetchall()

    event_count = conn.execute("SELECT COUNT(*) FROM event").fetchone()[0]
    venue_count = conn.execute("SELECT COUNT(*) FROM venue").fetchone()[0]
    household_count = conn.execute("SELECT COUNT(*) FROM household").fetchone()[0]
    scored_count = conn.execute(
        "SELECT COUNT(*) FROM household_event_state WHERE score IS NOT NULL"
    ).fetchone()[0]
    db_size_bytes = db_path.stat().st_size if db_path.exists() else 0

    return StatusReport(
        lookback_days=lookback_days,
        generated_at=now,
        periods=periods,
        sources=sources,
        usage_by_context=[((c or "(unlabeled)"), n) for c, n in context_rows],
        models=models,
        total_estimated_cost_usd=total_cost,
        any_unpriced_model=any_unpriced,
        event_count=event_count,
        venue_count=venue_count,
        household_count=household_count,
        scored_count=scored_count,
        db_size_bytes=db_size_bytes,
    )


def _stat_row_html(label: str, value: str) -> str:
    # value is pre-built HTML from this module's own callers (numbers and
    # internal strings, e.g. "5 runs &middot; 12 rows") -- never escape it
    # here, or the literal "&middot;" entity gets double-escaped into
    # "&amp;middot;", which every mail client then renders back out as the
    # literal on-screen text "&middot;" (confirmed live 2026-08-31, same
    # double-encoding failure mode as the Google Alerts titles fix). label
    # is always plain text (no intentional markup), so it's still escaped.
    # A single flowing line, not a rigid two-column table, so long
    # label/value pairs wrap naturally on a phone-width screen instead of
    # a fixed right-aligned column forcing an awkward multi-line break.
    return (
        f'<tr><td style="padding:5px 0; font-size:13px; color:{INK}; line-height:1.5;">'
        f'<span style="color:{MUTED};">{html.escape(label)}:</span> '
        f'<span style="font-weight:600;">{value}</span></td></tr>'
    )


def _section_html(title: str, rows_html: str) -> str:
    if not rows_html:
        rows_html = (
            f'<tr><td style="padding:5px 0; font-size:13px; color:{MUTED};">(none)</td></tr>'
        )
    return f"""\
    <tr>
      <td style="padding:20px 32px 4px;">
        <div style="font-family:{SERIF}; font-size:13px; font-weight:700; text-transform:uppercase; \
letter-spacing:0.06em; color:{ADMIN}; border-bottom:2px solid {ADMIN}; padding-bottom:6px;">{html.escape(title)}</div>
      </td>
    </tr>
    <tr>
      <td style="padding:4px 32px 8px;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="font-family:{SANS};">
          {rows_html}
        </table>
      </td>
    </tr>"""


def _bytes_to_human(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def _period_totals_rows_html(p: PeriodTotals) -> str:
    heading = (
        f'<tr><td style="padding:10px 0 2px; font-size:12px; font-weight:700; color:{ADMIN}; '
        f'text-transform:uppercase; letter-spacing:0.04em;">{html.escape(p.label)}</td></tr>'
    )
    cost_text = f"~${p.llm_cost_usd:.2f}" + (" (some models unpriced)" if p.llm_any_unpriced else "")
    rows = (
        _stat_row_html("LLM spend", f"{cost_text} across {p.llm_calls} call{'s' if p.llm_calls != 1 else ''}")
        + _stat_row_html(
            "Source API calls",
            f"{p.source_calls_ok} ok / {p.source_calls_failed} failed &middot; {p.source_rows_fetched:,} rows",
        )
    )
    return heading + rows


def build_status_html(report: StatusReport) -> str:
    subtitle = f"Last {report.lookback_days} days &middot; generated {report.generated_at.strftime('%a %d %b %Y, %H:%M UTC')}"

    totals_rows = "".join(_period_totals_rows_html(p) for p in report.periods)

    source_rows = "".join(
        _stat_row_html(
            s.name,
            f"{s.ok_runs} ok / {s.failed_runs} failed &middot; {s.rows_fetched} rows"
            if s.failed_runs
            else f"{s.ok_runs} run{'s' if s.ok_runs != 1 else ''} &middot; {s.rows_fetched} rows",
        )
        for s in report.sources
    )

    model_rows = "".join(
        _stat_row_html(
            f"{m.model} ({m.calls} calls)",
            f"{m.input_tokens:,} in / {m.output_tokens:,} out / {m.cache_read_tokens:,} cache-read"
            + (f" &middot; ~${m.estimated_cost_usd:.2f}" if m.estimated_cost_usd is not None else " &middot; cost unknown"),
        )
        for m in report.models
    )
    cost_note = (
        f"Estimated LLM spend, last {report.lookback_days} days: ~${report.total_estimated_cost_usd:.2f}"
        + (" (one or more models have no pricing on file — total is a floor, not exact)" if report.any_unpriced_model else "")
    )

    context_rows = "".join(_stat_row_html(context, f"{calls} call{'s' if calls != 1 else ''}") for context, calls in report.usage_by_context)

    catalog_rows = "".join(
        [
            _stat_row_html("Events in catalog", f"{report.event_count:,}"),
            _stat_row_html("Venues", f"{report.venue_count:,}"),
            _stat_row_html("Households configured", str(report.household_count)),
            _stat_row_html("Household-event rows scored", f"{report.scored_count:,}"),
            _stat_row_html("Database file size", _bytes_to_human(report.db_size_bytes)),
        ]
    )

    sections = (
        _section_html("Totals — this week / this month / all-time", totals_rows)
        + _section_html("Source API calls (last 7 days)", source_rows or empty_row("No source runs in this window."))
        + _section_html(f"LLM usage — {cost_note}", model_rows)
        + _section_html("LLM calls by household/context (last 7 days)", context_rows)
        + _section_html("Catalog snapshot (all-time)", catalog_rows)
    )

    footer = "Admin-only report — never sent to a household. Run `events-agent status` any time for a fresh one."
    return shell(
        mark_suffix="Admin Stats",
        mark_color=ADMIN,
        strapline="How this is scaling, and what it costs",
        subtitle=subtitle,
        body_rows=sections,
        footer=footer,
    )


def build_status_plain(report: StatusReport) -> str:
    lines = [
        f"CURTAIN UP – ADMIN STATS – last {report.lookback_days} days",
        f"generated {report.generated_at.strftime('%a %d %b %Y, %H:%M UTC')}",
        "",
        "TOTALS",
    ]
    for p in report.periods:
        cost = f"~${p.llm_cost_usd:.2f}" + (" (some models unpriced)" if p.llm_any_unpriced else "")
        lines.append(f"- {p.label}: LLM spend {cost} across {p.llm_calls} calls; "
                     f"source API calls {p.source_calls_ok} ok / {p.source_calls_failed} failed, "
                     f"{p.source_rows_fetched} rows fetched")

    lines += ["", "SOURCE API CALLS (last 7 days)"]
    if report.sources:
        for s in report.sources:
            lines.append(f"- {s.name}: {s.ok_runs} ok, {s.failed_runs} failed, {s.rows_fetched} rows")
    else:
        lines.append("- (none)")

    lines += ["", "LLM USAGE"]
    if report.models:
        for m in report.models:
            cost = f"~${m.estimated_cost_usd:.2f}" if m.estimated_cost_usd is not None else "cost unknown"
            lines.append(
                f"- {m.model}: {m.calls} calls, {m.input_tokens} in / {m.output_tokens} out / "
                f"{m.cache_creation_tokens} cache-write / {m.cache_read_tokens} cache-read tokens, {cost}"
            )
        lines.append(f"TOTAL estimated spend: ~${report.total_estimated_cost_usd:.2f}")
        if report.any_unpriced_model:
            lines.append("(one or more models have no pricing on file — total is a floor, not exact)")
    else:
        lines.append("- (none)")

    lines += ["", "LLM CALLS BY HOUSEHOLD/CONTEXT (last 7 days)"]
    if report.usage_by_context:
        for context, calls in report.usage_by_context:
            lines.append(f"- {context}: {calls}")
    else:
        lines.append("- (none)")

    lines += [
        "",
        "CATALOG SNAPSHOT (all-time)",
        f"- events: {report.event_count}",
        f"- venues: {report.venue_count}",
        f"- households configured: {report.household_count}",
        f"- household-event rows scored: {report.scored_count}",
        f"- database file size: {_bytes_to_human(report.db_size_bytes)}",
    ]
    return "\n".join(lines)
