"""Wait until a local HTTP health endpoint reports success."""

from __future__ import annotations

import argparse
import time
import urllib.error
import urllib.request


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--interval", type=float, default=1.0)
    return parser.parse_args()


def wait_until_ready(url: str, *, timeout: float, interval: float) -> bool:
    if timeout <= 0 or interval <= 0:
        raise ValueError("timeout and interval must be positive")
    deadline = time.monotonic() + timeout
    while True:
        try:
            with urllib.request.urlopen(url, timeout=min(interval, 5.0)) as response:
                if 200 <= response.status < 300:
                    return True
        except (OSError, urllib.error.HTTPError):
            pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(interval, remaining))


def main() -> int:
    args = parse_args()
    ready = wait_until_ready(args.url, timeout=args.timeout, interval=args.interval)
    if ready:
        print(f"ready: {args.url}")
        return 0
    print(f"timed out waiting for: {args.url}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
