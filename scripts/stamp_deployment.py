"""Stamp an already synchronized deployment with the source Git identity."""

from __future__ import annotations

import argparse
from pathlib import Path

from runtime_identity import write_stamp


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("deployment", type=Path)
    args = parser.parse_args()
    print(write_stamp(args.source, args.deployment))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
