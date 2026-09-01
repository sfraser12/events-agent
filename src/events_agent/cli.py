"""Entry point: events-agent <command>."""

from __future__ import annotations

import argparse
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from events_agent.annual_anchors import due_reminders, load_annual_anchors
from events_agent.config import DEFAULT_DB_PATH, HOUSEHOLDS_DIR, REPO_ROOT, load_config, load_secrets
from events_agent.db import (
    RAW_JSON_RETENTION_DAYS,
    finish_source_run,
    get_connection,
    init_db,
    list_households_as_dicts,
    mark_delisted_events,
    mark_past_events,
    mark_surfaced,
    prune_stale_raw_json,
    record_llm_usage,
    set_verdict,
    start_source_run,
    upsert_household,
    upsert_raw_event,
)
from events_agent.delivery.alert import build_alert_html, build_alert_plain, find_alertable_changes, mark_notified
from events_agent.delivery.digest import build_digest, build_digest_html, build_digest_plain
from events_agent.delivery.email import send_email
from events_agent.delivery.ics import build_ics, select_calendar_events
from events_agent.delivery.lookahead import build_lookahead_html, build_lookahead_plain, select_lookahead_events
from events_agent.delivery.status import build_status_html, build_status_plain, build_status_report
from events_agent.scoring import AnthropicLLMClient, run_scoring
from events_agent.sources.google_alerts import GoogleAlertsAdapter
from events_agent.sources.skiddle import SkiddleAdapter
from events_agent.sources.ticketmaster import TicketmasterAdapter

# Stable per-household ids, keyed by households/<name>/ directory name.
# Deliberately an explicit map, not auto-numbered from directory listing
# order: upsert_household() upserts by this id, so if it ever shifted for
# an existing household (e.g. a new household sorting earlier), that
# household's whole scoring/verdict/snooze history in household_event_state
# would silently detach from a different id. Add one line here per new
# household; never reassign an existing one.
HOUSEHOLD_IDS: dict[str, int] = {
    "scott": 1,
    "brother": 2,
}
HOUSEHOLD_ID = HOUSEHOLD_IDS["scott"]  # CLI default for `verdict` when --household isn't passed


def cmd_init(args: argparse.Namespace) -> int:
    init_db(DEFAULT_DB_PATH)
    print(f"Database ready at {DEFAULT_DB_PATH}")

    conn = get_connection(DEFAULT_DB_PATH)
    try:
        for name, household_id in HOUSEHOLD_IDS.items():
            household_dir = HOUSEHOLDS_DIR / name
            config_path = household_dir / "config.yaml"
            taste_profile_path = household_dir / "taste-profile.md"
            if not config_path.exists() or not taste_profile_path.exists():
                # Not a failure — a household simply hasn't sent their config/
                # taste profile yet. Every other household still seeds and the
                # pipeline still runs for them; re-running init later picks
                # this one up once both files exist.
                print(
                    f"Note: households/{name}/config.yaml and taste-profile.md not both present yet — "
                    "skipping (re-run 'events-agent init' once they're added).",
                    file=sys.stderr,
                )
                continue

            config = load_config(config_path)
            upsert_household(
                conn,
                household_id,
                label=config.home.label,
                home_latitude=config.home.latitude,
                home_longitude=config.home.longitude,
                radius_miles=config.search.radius_miles,
                near_days=config.search.horizons.near_days,
                month_days=config.search.horizons.month_days,
                max_drive_minutes=config.constraints.max_drive_minutes,
                price_ceiling=config.constraints.price_ceiling,
                blackout_dates=[
                    (r.start.isoformat(), r.end.isoformat()) for r in config.constraints.blackout_dates
                ],
                taste_profile_path=str(taste_profile_path),
                digest_threshold=config.scoring.digest_threshold,
                alert_threshold=config.scoring.alert_threshold,
                email_to=config.delivery.email_to,
                far_radius_miles=config.search.far_radius_miles,
                far_threshold=config.scoring.far_threshold,
                far_min_latitude=config.search.far_min_latitude,
            )
            conn.commit()
            far_note = (
                f", far tier to {config.search.far_radius_miles}mi @ score>={config.scoring.far_threshold}"
                f"{f' (lat>={config.search.far_min_latitude})' if config.search.far_min_latitude else ''}"
                if config.search.far_radius_miles
                else ""
            )
            print(
                f"Household '{name}' seeded from households/{name}/config.yaml "
                f"(home={config.home.label}, radius={config.search.radius_miles}mi{far_note})."
            )
    finally:
        conn.close()
    return 0


def cmd_harvest(args: argparse.Namespace) -> int:
    # Harvest is a single shared fetch, not one per household — every
    # household's own radius/home just filters the same catalog later, at
    # scoring/constraint time. Was previously anchored on Scott's config
    # alone (comment removed 2026-08-31): confirmed that meant a second
    # household's own radius, far tier, and Google Alerts feeds were never
    # fetched at all — silently, no error, just missing from the shared
    # catalog. Now loops every household with both config.yaml and
    # taste-profile.md present (same check as cmd_init). Same real event
    # upserts idempotently regardless of which household's pass found it
    # first (dedupe is by fingerprint), so overlapping radii between
    # households just cost extra API calls, not incorrect data.
    secrets = load_secrets()

    household_configs: list[tuple[str, Any]] = []
    for name in HOUSEHOLD_IDS:
        config_path = HOUSEHOLDS_DIR / name / "config.yaml"
        taste_path = HOUSEHOLDS_DIR / name / "taste-profile.md"
        if config_path.exists() and taste_path.exists():
            household_configs.append((name, load_config(config_path)))
        else:
            print(
                f"Note: households/{name}/config.yaml and taste-profile.md not both present yet — "
                "skipping harvest for this household.",
                file=sys.stderr,
            )

    if not household_configs:
        print("No household configured — run 'events-agent init' first.", file=sys.stderr)
        return 1

    if not secrets.skiddle_api_key:
        print("SKIDDLE_API_KEY not set in .env — skipping Skiddle.", file=sys.stderr)
    if not secrets.ticketmaster_api_key:
        print("TICKETMASTER_API_KEY not set in .env — skipping Ticketmaster.", file=sys.stderr)

    # Adapter names only get a household suffix once there's more than one
    # household -- keeps source_run history/log output identical to the
    # single-household case (same pattern as calendar's curtainup.ics vs
    # curtainup-<label-slug>.ics once there's more than one household).
    suffix_names = len(household_configs) > 1

    adapters: list = []
    for name, config in household_configs:
        suffix = f"_{name}" if suffix_names else ""

        if secrets.skiddle_api_key:
            skiddle_adapter = SkiddleAdapter(
                api_key=secrets.skiddle_api_key,
                latitude=config.home.latitude,
                longitude=config.home.longitude,
                radius_miles=config.search.radius_miles,
                cache_dir=REPO_ROOT / ".cache" / "skiddle" / name,
            )
            skiddle_adapter.name = f"skiddle{suffix}"
            adapters.append(skiddle_adapter)

        if secrets.ticketmaster_api_key:
            tm_adapter = TicketmasterAdapter(
                api_key=secrets.ticketmaster_api_key,
                latitude=config.home.latitude,
                longitude=config.home.longitude,
                radius_miles=config.search.radius_miles,
                cache_dir=REPO_ROOT / ".cache" / "ticketmaster" / name,
            )
            tm_adapter.name = f"ticketmaster{suffix}"
            adapters.append(tm_adapter)

            if config.search.far_radius_miles:
                # Second, wider pass for the "worth a special trip" tier (see
                # constraints.py). Ticketmaster only, not Skiddle: Ticketmaster
                # skews toward bigger, more "worth a special trip" caliber acts,
                # while Skiddle skews toward small club nights/local promoters —
                # widening Skiddle's net too would mostly add noise, not signal,
                # for a tier whose whole point is a much higher score bar.
                # Same real-world events already caught by the normal-radius
                # pass just upsert again here (idempotent, harmless, dedupe is
                # by fingerprint) — this pass only adds the ones beyond
                # radius_miles that it exists to reach.
                far_adapter = TicketmasterAdapter(
                    api_key=secrets.ticketmaster_api_key,
                    latitude=config.home.latitude,
                    longitude=config.home.longitude,
                    radius_miles=config.search.far_radius_miles,
                    cache_dir=REPO_ROOT / ".cache" / "ticketmaster_wide" / name,
                )
                far_adapter.name = f"ticketmaster_wide{suffix}"
                adapters.append(far_adapter)

        for alert in config.google_alerts:
            if not alert.feed_url:
                # Not a failure — the Google Alert just hasn't been created yet
                # (no public API to do that automatically). See
                # sources/google_alerts.py for setup instructions.
                print(
                    f"Note: no feed_url configured for Google Alert '{alert.venue_name}' (household {name}) — skipping.",
                    file=sys.stderr,
                )
                continue
            ga_adapter = GoogleAlertsAdapter(
                feed_url=alert.feed_url,
                venue_name=alert.venue_name,
                venue_latitude=alert.latitude,
                venue_longitude=alert.longitude,
            )
            if suffix_names:
                ga_adapter.name = f"{ga_adapter.name}{suffix}"
            adapters.append(ga_adapter)

    if not adapters:
        print("No source API keys configured — copy .env.example and fill in at least one.", file=sys.stderr)
        return 1

    conn = get_connection(DEFAULT_DB_PATH)
    all_rows: list[tuple[int, bool, object]] = []
    # (source_name, new_count, updated_count, error) — one entry per adapter,
    # regardless of success, so one source going down never hides the others.
    summaries: list[tuple[str, int, int, str | None]] = []
    try:
        for adapter in adapters:
            run_id = start_source_run(conn, adapter.name)
            conn.commit()  # the run row must survive a rollback below if this adapter fails
            source_rows: list[tuple[int, bool, object]] = []
            try:
                for raw_event in adapter.fetch(since=None):
                    event_id, created = upsert_raw_event(conn, raw_event)
                    source_rows.append((event_id, created, raw_event))
                finish_source_run(conn, run_id, status="ok", rows_fetched=len(source_rows))
                conn.commit()
                all_rows.extend(source_rows)
                new_count = sum(1 for _, created, _ in source_rows if created)
                summaries.append((adapter.name, new_count, len(source_rows) - new_count, None))
            except Exception as exc:
                conn.rollback()
                finish_source_run(conn, run_id, status="failed", rows_fetched=0, error=str(exc))
                conn.commit()
                summaries.append((adapter.name, 0, 0, str(exc)))

        past_count = mark_past_events(conn, datetime.now(UTC).date())
        conn.commit()

        # Only trust "not re-confirmed" as "delisted" when at least one
        # full-catalog source actually ran successfully this time — a total
        # outage across ticketmaster/skiddle would otherwise look identical
        # to every upcoming event having been pulled at once. startswith,
        # not an exact-name set, since adapter names now carry a
        # per-household suffix once there's more than one household
        # (ticketmaster_brother, ticketmaster_wide_brother, ...).
        catalog_ok = any(
            (name.startswith("ticketmaster") or name.startswith("skiddle")) and error is None
            for name, _, _, error in summaries
        )
        delisted_count = mark_delisted_events(conn, datetime.now(UTC).date()) if catalog_ok else 0
        conn.commit()

        pruned_count = prune_stale_raw_json(conn, datetime.now(UTC).date())
        conn.commit()
    finally:
        conn.close()

    print_event_table(all_rows)
    print()
    for name, new_count, updated_count, error in summaries:
        if error:
            print(f"{name}: FAILED — {error}", file=sys.stderr)
        else:
            print(f"{name}: {new_count + updated_count} events ({new_count} new, {updated_count} updated)")
    if past_count:
        print(f"{past_count} event(s) marked past.")
    if delisted_count:
        print(f"{delisted_count} event(s) marked cancelled — no longer confirmed by any source (likely sold out/pulled).")
    if pruned_count:
        print(f"{pruned_count} stale raw_json blob(s) cleared (past/cancelled events older than {RAW_JSON_RETENTION_DAYS} days).")

    if all(error for _, _, _, error in summaries):
        return 1
    return 0


def print_event_table(rows: list[tuple[int, bool, object]]) -> None:
    if not rows:
        print("No events found.")
        return

    headers = ("date", "source", "category", "title", "venue", "price")
    table_rows = []
    for _, _, raw in rows:
        date_str = raw.event_date.date().isoformat() if raw.event_date else "TBC"
        price = _format_price(raw.price_min, raw.price_max, raw.currency)
        table_rows.append((date_str, raw.source_name, raw.category or "-", raw.title, raw.venue_name, price))

    widths = [
        max(len(headers[i]), max((len(r[i]) for r in table_rows), default=0)) for i in range(len(headers))
    ]
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    print(line)
    print("  ".join("-" * w for w in widths))
    for row in sorted(table_rows, key=lambda r: r[0]):
        print("  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)))


def _format_price(price_min: float | None, price_max: float | None, currency: str) -> str:
    if price_min is None and price_max is None:
        return "-"
    if not price_min and not price_max:
        return "free/TBC"
    if price_min == price_max:
        return f"{currency} {price_min:.2f}"
    return f"{currency} {price_min:.2f}-{price_max:.2f}"


def cmd_alert(args: argparse.Namespace) -> int:
    secrets = load_secrets()
    conn = get_connection(DEFAULT_DB_PATH)
    try:
        now = datetime.now(UTC)
        households = list_households_as_dicts(conn)
        if not households:
            print("No household configured — run 'events-agent init' first.", file=sys.stderr)
            return 1

        all_change_ids: set[int] = set()
        any_items = False
        for household in households:
            items = find_alertable_changes(conn, now, household)
            if not items:
                print(f"{household['label']}: no urgent alerts.")
                continue
            any_items = True
            print(f"URGENT for {household['label']} — {len(items)} event(s) need attention:\n")
            for item in items:
                venue = f" @ {item.venue_name}" if item.venue_name else ""
                print(f"- {item.title}{venue}: {item.reason}")
                if item.url:
                    print(f"  {item.url}")

            plural = "" if len(items) == 1 else "s"
            sent = send_email(
                smtp_host=secrets.smtp_host,
                smtp_port=secrets.smtp_port,
                smtp_user=secrets.smtp_user,
                smtp_password=secrets.smtp_password,
                from_email=secrets.smtp_user,
                to_email=household["email_to"],
                subject=f"Curtain Up – Last Call – {len(items)} thing{plural} need a decision today",
                html_body=build_alert_html(household, items, now),
                plain_body=build_alert_plain(household, items, now),
            )
            if sent:
                all_change_ids.update(item.change_id for item in items)
                print(f"  (alert emailed to {household['email_to']})")
            else:
                print(f"  (alert email NOT sent — see error above; not marking notified, will retry next run)")

        if not any_items:
            print("No urgent alerts.")
        if all_change_ids:
            mark_notified(conn, list(all_change_ids), now)
        conn.commit()
    finally:
        conn.close()
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    secrets = load_secrets()
    if not secrets.anthropic_api_key:
        print("ANTHROPIC_API_KEY not set in .env — copy .env.example and fill it in.", file=sys.stderr)
        return 1

    conn = get_connection(DEFAULT_DB_PATH)

    def _on_usage(context, model, input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens):
        record_llm_usage(
            conn, context, model, input_tokens, output_tokens,
            cache_creation_tokens, cache_read_tokens, datetime.now(UTC).isoformat(),
        )

    client = AnthropicLLMClient(api_key=secrets.anthropic_api_key, on_usage=_on_usage)
    try:
        households = list_households_as_dicts(conn)
        if not households:
            print("No household configured — run 'events-agent init' first.", file=sys.stderr)
            return 1
        summary = run_scoring(conn, client)
        conn.commit()
    finally:
        conn.close()

    for label, stats in summary["households"].items():
        print(f"{label}: {stats['scored']} scored, {stats['excluded']} excluded (constraints), {stats['failed']} failed")
    print(f"{summary['duplicates_adjudicated']} duplicate pair(s) adjudicated.")
    return 0


def cmd_digest(args: argparse.Namespace) -> int:
    secrets = load_secrets()
    anchors = load_annual_anchors(REPO_ROOT / "annual-anchors.yaml")
    reminders = due_reminders(anchors, datetime.now(UTC).date())
    conn = get_connection(DEFAULT_DB_PATH)
    try:
        households = list_households_as_dicts(conn)
        if not households:
            print("No household configured — run 'events-agent init' first.", file=sys.stderr)
            return 1

        for household in households:
            horizons = build_digest(conn, household)
            total = sum(len(events) for events in horizons.values())
            if total == 0 and not reminders:
                print(f"{household['label']}: nothing to send this week.")
                continue

            html_body = build_digest_html(household, horizons, reminders)
            plain_body = build_digest_plain(household, horizons, reminders)
            plural = "" if total == 1 else "s"
            subject = f"Curtain Up – Shortlist – {total} thing{plural} worth a look this week"
            sent = send_email(
                smtp_host=secrets.smtp_host,
                smtp_port=secrets.smtp_port,
                smtp_user=secrets.smtp_user,
                smtp_password=secrets.smtp_password,
                from_email=secrets.smtp_user,
                to_email=household["email_to"],
                subject=subject,
                html_body=html_body,
                plain_body=plain_body,
            )
            if sent:
                event_ids = [event.event_id for events in horizons.values() for event in events]
                mark_surfaced(conn, household["id"], event_ids, datetime.now(UTC).isoformat())
                conn.commit()
                print(f"{household['label']}: digest sent to {household['email_to']} ({total} events).")
            else:
                print(f"{household['label']}: digest NOT sent ({total} events ready — see error above).", file=sys.stderr)
    finally:
        conn.close()
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Admin-only stats/cost email — never sent to a household. Added
    2026-08-31 to actually answer "how is this scaling, what does it cost"
    from real data (source_run + llm_usage) instead of scrollback."""
    secrets = load_secrets()
    admin_email = secrets.admin_email or secrets.smtp_user
    conn = get_connection(DEFAULT_DB_PATH)
    try:
        report = build_status_report(conn, DEFAULT_DB_PATH, lookback_days=7)
    finally:
        conn.close()

    html_body = build_status_html(report)
    plain_body = build_status_plain(report)
    sent = send_email(
        smtp_host=secrets.smtp_host,
        smtp_port=secrets.smtp_port,
        smtp_user=secrets.smtp_user,
        smtp_password=secrets.smtp_password,
        from_email=secrets.smtp_user,
        to_email=admin_email,
        subject=f"Curtain Up – Admin Stats – ~${report.total_estimated_cost_usd:.2f} LLM spend, last 7 days",
        html_body=html_body,
        plain_body=plain_body,
    )
    print(plain_body)
    print()
    if sent:
        print(f"Status report emailed to {admin_email}.")
        return 0
    print("Status report NOT emailed — see error above.", file=sys.stderr)
    return 1


def cmd_fortnight(args: argparse.Namespace) -> int:
    secrets = load_secrets()
    conn = get_connection(DEFAULT_DB_PATH)
    try:
        households = list_households_as_dicts(conn)
        if not households:
            print("No household configured — run 'events-agent init' first.", file=sys.stderr)
            return 1

        for household in households:
            events = select_lookahead_events(conn, household)
            if not events:
                print(f"{household['label']}: nothing new for the fortnight check.")
                continue

            html_body = build_lookahead_html(household, events)
            plain_body = build_lookahead_plain(household, events)
            plural = "" if len(events) == 1 else "s"
            subject = f"Curtain Up – Understudy – {len(events)} thing{plural} in the next fortnight worth a second look"
            sent = send_email(
                smtp_host=secrets.smtp_host,
                smtp_port=secrets.smtp_port,
                smtp_user=secrets.smtp_user,
                smtp_password=secrets.smtp_password,
                from_email=secrets.smtp_user,
                to_email=household["email_to"],
                subject=subject,
                html_body=html_body,
                plain_body=plain_body,
            )
            if sent:
                event_ids = [e.event_id for e in events]
                mark_surfaced(conn, household["id"], event_ids, datetime.now(UTC).isoformat())
                conn.commit()
                print(f"{household['label']}: fortnight check sent to {household['email_to']} ({len(events)} events).")
            else:
                print(
                    f"{household['label']}: fortnight check NOT sent ({len(events)} events ready — see error above).",
                    file=sys.stderr,
                )
    finally:
        conn.close()
    return 0


def cmd_calendar(args: argparse.Namespace) -> int:
    conn = get_connection(DEFAULT_DB_PATH)
    try:
        households = list_households_as_dicts(conn)
        if not households:
            print("No household configured — run 'events-agent init' first.", file=sys.stderr)
            return 1

        for household in households:
            events = select_calendar_events(conn, household)
            if args.out:
                out_path = args.out
            elif len(households) == 1:
                out_path = REPO_ROOT / "curtainup.ics"
            else:
                # One household per file once there's more than one --
                # otherwise the second household's write would silently
                # overwrite the first's.
                slug = re.sub(r"[^a-z0-9]+", "-", household["label"].lower()).strip("-")
                out_path = REPO_ROOT / f"curtainup-{slug}.ics"
            out_path.write_text(build_ics(household, events), encoding="utf-8")
            print(f"{household['label']}: {len(events)} shortlisted event(s) written to {out_path}")
    finally:
        conn.close()
    return 0


def cmd_verdict(args: argparse.Namespace) -> int:
    conn = get_connection(DEFAULT_DB_PATH)
    try:
        set_verdict(conn, args.household, args.event_id, args.verdict)
        conn.commit()
    finally:
        conn.close()
    print(f"Event {args.event_id} marked '{args.verdict}' for household {args.household}.")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """The daily pipeline: harvest, score, alert — meant to be the single
    launchd-scheduled entry point for the early-morning job. Each step is
    attempted even if an earlier one failed (fail loudly, degrade
    gracefully): a harvest outage shouldn't suppress an alert on a change
    already detected in a previous run. Weekly delivery (digest, fortnight)
    is deliberately not part of this — it's a separate, lightweight,
    deliver-only pass on its own Sunday-evening schedule against whatever
    this job already refreshed that morning, not a second harvest+score."""
    worst = 0
    for step_name, step_fn in (("harvest", cmd_harvest), ("score", cmd_score), ("alert", cmd_alert)):
        code = step_fn(args)
        if code != 0:
            print(f"run: '{step_name}' step failed (exit {code}) — continuing with remaining steps.", file=sys.stderr)
            worst = code
    return worst


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="events-agent", description="Personal event-discovery agent.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Create the SQLite database and check config.")
    init_parser.set_defaults(func=cmd_init)

    harvest_parser = subparsers.add_parser(
        "harvest", help="Fetch events from all configured sources and upsert to the database."
    )
    harvest_parser.set_defaults(func=cmd_harvest)

    alert_parser = subparsers.add_parser("alert", help="Print urgent alerts: imminent on-sale dates and low availability.")
    alert_parser.set_defaults(func=cmd_alert)

    score_parser = subparsers.add_parser("score", help="Run the LLM scoring pass over new/changed events.")
    score_parser.set_defaults(func=cmd_score)

    digest_parser = subparsers.add_parser("digest", help="Email the weekly digest of scored events.")
    digest_parser.set_defaults(func=cmd_digest)

    status_parser = subparsers.add_parser(
        "status", help="Email an admin stats/cost report (API calls, LLM spend, catalog size) — never sent to a household."
    )
    status_parser.set_defaults(func=cmd_status)

    fortnight_parser = subparsers.add_parser(
        "fortnight", help="Email near-term events (next 14 days) that scored below the digest bar."
    )
    fortnight_parser.set_defaults(func=cmd_fortnight)

    calendar_parser = subparsers.add_parser(
        "calendar", help="Write shortlisted events (verdict interested/booked) to an .ics file."
    )
    calendar_parser.add_argument("--out", type=Path, default=None)
    calendar_parser.set_defaults(func=cmd_calendar)

    verdict_parser = subparsers.add_parser(
        "verdict", help="Record a decision on an event: interested, booked, or no."
    )
    verdict_parser.add_argument("event_id", type=int)
    verdict_parser.add_argument("verdict", choices=["interested", "booked", "no"])
    verdict_parser.add_argument("--household", type=int, default=HOUSEHOLD_ID)
    verdict_parser.set_defaults(func=cmd_verdict)

    run_parser = subparsers.add_parser("run", help="Daily pipeline: harvest, score, alert.")
    run_parser.set_defaults(func=cmd_run)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
