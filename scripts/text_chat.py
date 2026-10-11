"""Run Helios as a text-only local chat client."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from api.api_client import APIClient  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", nargs="*", help="Prompt; omit it for interactive mode")
    parser.add_argument("--think", action="store_true", help="Use the configured think route")
    return parser.parse_args()


def answer(client: APIClient, prompt: str, *, think: bool) -> str:
    if think:
        return client.think(prompt, tts=False)
    return client.talk(prompt, tts=False)


def main() -> int:
    args = parse_args()
    with APIClient() as client:
        if args.prompt:
            print(answer(client, " ".join(args.prompt), think=args.think), flush=True)
            return 0

        while True:
            try:
                prompt = input("tu> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if not prompt:
                continue
            if prompt.lower() in {"exit", "quit", "esci"}:
                return 0
            print(f"emilia> {answer(client, prompt, think=args.think)}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
