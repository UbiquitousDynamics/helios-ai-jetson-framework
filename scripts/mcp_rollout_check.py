"""Review local rollout evidence without enabling automation or accessing devices."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from automation.rollout import STAGES, check_rollout


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--stage", choices=STAGES, required=True)
    args = parser.parse_args(argv)
    result = check_rollout(args.manifest, args.stage)
    print(json.dumps(asdict(result), sort_keys=True))
    return 0 if result.status == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
