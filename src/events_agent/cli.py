"""Entry point: events-agent <command>."""

from __future__ import annotations

import argparse
import sys

from events_agent.config import DEFAULT_DB_PATH, REPO_ROOT, load_config, load_secrets
from events_agent.db import finish_source_run, get_connection, init_db, start_source_run, upsert_raw_event
from events_agent.sources.skiddle import SkiddleAdapter
from events_agent.sources.ticketmaster import TicketmasterAdapter

STUB_COMMANDS = ("score", "digest", "alert", "run")


def cmd_init(args: argparse.Namespace) -> int:
    init_db(DEFAULT_DB_PATH)
    print(f"Database ready at {DEFAULT_DB_PATH}")
    try:
        config = load_config()
        print(f"Config loaded: home={config.home.label}, radius={config.search.radius_miles}mi")
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
    finally:
        conn.close()

    print_event_table(all_rows)
    print()
    for name, new_count, updated_count, error in summaries:
        if error:
            print(f"{name}: FAILED — {error}", file=sys.stderr)
        else:
            print(f"{name}: {new_count + updated_count} events ({new_count} new, {updated_count} updated)")

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
