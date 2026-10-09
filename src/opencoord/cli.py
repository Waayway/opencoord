"""Headless command line interface (``opencoord-cli``)."""

import argparse
import sys
from collections.abc import Sequence

from opencoord import __version__

COMMANDS = ("info", "sweep", "scan")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="opencoord-cli", description=__doc__)
    parser.add_argument("--version", action="version", version=f"opencoord {__version__}")
    sub = parser.add_subparsers(dest="command")
    for name in COMMANDS:
        sub.add_parser(name, help=f"{name} (not implemented yet)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 2
    print(f"opencoord-cli {args.command}: not implemented yet", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
