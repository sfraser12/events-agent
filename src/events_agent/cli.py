"""Entry point: events-agent <command>."""

from __future__ import annotations

import argparse
import sys

from events_agent.config import DEFAULT_DB_PATH, load_config
from events_agent.db import init_db

STUB_COMMANDS = ("harvest", "score", "digest", "alert", "run")


def cmd_init(args: argparse.Namespace) -> int:
    init_db(DEFAULT_DB_PATH)
    print(f"Database ready at {DEFAULT_DB_PATH}")
    try:
        config = load_config()
        print(f"Config loaded: home={config.home.label}, radius={config.search.radius_miles}mi")
    except FileNotFoundError as exc:
        print(f"Note: {exc}", file=sys.stderr)
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
