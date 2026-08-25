"""Entry point: events-agent <command>."""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from events_agent.config import DEFAULT_DB_PATH, REPO_ROOT, load_config, load_secrets
from events_agent.db import (
    finish_source_run,
    get_connection,
    init_db,
    list_households_as_dicts,
    mark_past_events,
    mark_surfaced,
    set_verdict,
    start_source_run,
    upsert_household,
    upsert_raw_event,
)
from events_agent.delivery.alert import build_alert_html, build_alert_plain, find_alertable_changes, mark_notified
from events_agent.delivery.digest import build_digest, build_digest_html, build_digest_plain
from events_agent.delivery.email import send_email
from events_agent.delivery.ics import build_ics, select_calendar_events
from events_agent.scoring import AnthropicLLMClient, run_scoring
from events_agent.sources.skiddle import SkiddleAdapter
from events_agent.sources.ticketmaster import TicketmasterAdapter

STUB_COMMANDS = ("run",)

# Single-file today, deliberately (see CLAUDE.md build notes) — a households/
# directory with one config + taste profile per household is the cheap way
# to extend this later; not built until a second household actually exists.
TASTE_PROFILE_PATH = REPO_ROOT / "taste-profile.md"
HOUSEHOLD_ID = 1


def cmd_init(args: argparse.Namespace) -> int:
    init_db(DEFAULT_DB_PATH)
    print(f"Database ready at {DEFAULT_DB_PATH}")
    try:
        config = load_config()
        print(f"Config loaded: home={config.home.label}, radius={config.search.radius_miles}mi")

        conn = get_connection(DEFAULT_DB_PATH)
        try:
            upsert_household(
                conn,
                HOUSEHOLD_ID,
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
                taste_profile_path=str(TASTE_PROFILE_PATH),
                digest_threshold=config.scoring.digest_threshold,
                alert_threshold=config.scoring.alert_threshold,
                email_to=config.delivery.email_to,
            )
            conn.commit()
        finally:
            conn.close()
        print(f"Household seeded from config.yaml (taste profile: {TASTE_PROFILE_PATH.name}).")
    except FileNotFoundError as exc:
        print(f"Note: {exc}", file=sys.stderr)
    return 0


def cmd_harvest(args: argparse.Namespace) -> int:
    config = load_config()
    secrets = load_secrets()

    adapters: list = []
    if secrets.skiddle_api_key:
        adapters.append(
            SkiddleAdapter(
                api_key=secrets.skiddle_api_key,
                latitude=config.home.latitude,
                longitude=config.home.longitude,
                radius_miles=config.search.radius_miles,
                cache_dir=REPO_ROOT / ".cache" / "skiddle",
            )
        )
    else:
        print("SKIDDLE_API_KEY not set in .env — skipping Skiddle.", file=sys.stderr)

    if secrets.ticketmaster_api_key:
        adapters.append(
            TicketmasterAdapter(
                api_key=secrets.ticketmaster_api_key,
                latitude=config.home.latitude,
                longitude=config.home.longitude,
                radius_miles=config.search.radius_miles,
                cache_dir=REPO_ROOT / ".cache" / "ticketmaster",
            )
        )
    else:
        print("TICKETMASTER_API_KEY not set in .env — skipping Ticketmaster.", file=sys.stderr)

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
                subject=f"Marquee — {len(items)} thing{plural} need a decision today",
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

    client = AnthropicLLMClient(api_key=secrets.anthropic_api_key)
    conn = get_connection(DEFAULT_DB_PATH)
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
    conn = get_connection(DEFAULT_DB_PATH)
    try:
        households = list_households_as_dicts(conn)
        if not households:
            print("No household configured — run 'events-agent init' first.", file=sys.stderr)
            return 1

        for household in households:
            horizons = build_digest(conn, household)
            total = sum(len(events) for events in horizons.values())
            if total == 0:
                print(f"{household['label']}: nothing to send this week.")
                continue

            html_body = build_digest_html(household, horizons)
            plain_body = build_digest_plain(household, horizons)
            plural = "" if total == 1 else "s"
            subject = f"Marquee — {total} thing{plural} worth a look this week"
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


def cmd_calendar(args: argparse.Namespace) -> int:
    conn = get_connection(DEFAULT_DB_PATH)
    try:
        households = list_households_as_dicts(conn)
        if not households:
            print("No household configured — run 'events-agent init' first.", file=sys.stderr)
            return 1

        out_path = args.out or (REPO_ROOT / "marquee.ics")
        for household in households:
            events = select_calendar_events(conn, household)
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


def cmd_stub(name: str):
    def handler(args: argparse.Namespace) -> int:
        print(f"'{name}' is not implemented yet.")
        return 0

    return handler


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

    for name in STUB_COMMANDS:
        stub_parser = subparsers.add_parser(name, help=f"({name} — not implemented yet)")
        stub_parser.set_defaults(func=cmd_stub(name))

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
