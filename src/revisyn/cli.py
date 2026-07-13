"""Run the fixed, disposable memory-change laboratory."""

import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence
from importlib.metadata import version

from .errors import RevisynError
from .lab import run_lab


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="revisyn", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {version('revisyn')}")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("lab", help="compare plan reuse after a controlled fact change")
    parser.parse_args(argv)
    try:
        print(json.dumps(run_lab().as_dict(), indent=2, sort_keys=True))
    except (RevisynError, sqlite3.Error, OSError) as exc:
        print(f"revisyn: error: {exc}", file=sys.stderr)
        return 1
    return 0
