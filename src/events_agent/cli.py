"""Entry point: events-agent <command>."""

from __future__ import annotations

import argparse
import sys

from events_agent.config import DEFAULT_DB_PATH, REPO_ROOT, load_config, load_secrets
from events_agent.db import finish_source_run, get_connection, init_db, start_source_run, upsert_raw_event
from events_agent.sources.skiddle import SkiddleAdapter

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
    if not secrets.skiddle_api_key:
        print("SKIDDLE_API_KEY not set in .env — copy .env.example and fill it in.", file=sys.stderr)
        return 1

    adapter = SkiddleAdapter(
        api_key=secrets.skiddle_api_key,
        latitude=config.home.latitude,
        longitude=config.home.longitude,
        radius_miles=config.search.radius_miles,
        cache_dir=REPO_ROOT / ".cache" / "skiddle",
    )

    conn = get_connection(DEFAULT_DB_PATH)
    run_id = start_source_run(conn, adapter.name)
    conn.commit()  # the run row must survive a rollback below if harvesting fails
    rows: list[tuple[int, bool, object]] = []
    try:
        for raw_event in adapter.fetch(since=None):
            event_id, created = upsert_raw_event(conn, raw_event)
            rows.append((event_id, created, raw_event))
        finish_source_run(conn, run_id, status="ok", rows_fetched=len(rows))
        conn.commit()
    except Exception as exc:
        conn.rollback()
        finish_source_run(conn, run_id, status="failed", rows_fetched=0, error=str(exc))
        conn.commit()
        print(f"skiddle harvest failed: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    print_event_table(rows)
    new_count = sum(1 for _, created, _ in rows if created)
    print(f"\n{len(rows)} events from skiddle ({new_count} new, {len(rows) - new_count} updated).")
    return 0


def print_event_table(rows: list[tuple[int, bool, object]]) -> None:
    if not rows:
        print("No events found.")
        return

    headers = ("date", "category", "title", "venue", "price")
    table_rows = []
    for _, _, raw in rows:
        date_str = raw.event_date.date().isoformat() if raw.event_date else "TBC"
        price = _format_price(raw.price_min, raw.price_max, raw.currency)
        table_rows.append((date_str, raw.category or "-", raw.title, raw.venue_name, price))

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

    harvest_parser = subparsers.add_parser("harvest", help="Fetch events from Skiddle and upsert to the database.")
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
