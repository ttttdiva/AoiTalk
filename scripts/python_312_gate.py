"""Validate that the current interpreter is Python 3.12 or newer."""

from __future__ import annotations

import argparse
import sys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--print-executable", action="store_true")
    args = parser.parse_args()

    if sys.version_info < (3, 12):
        return 1

    if args.print_executable:
        print(sys.executable)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
