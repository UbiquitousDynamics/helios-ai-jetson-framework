"""Measure a local llama.cpp chat endpoint without audio or project dependencies."""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8081/v1/chat/completions")
    parser.add_argument("--model", default="local-model")
    parser.add_argument("--prompt", default="Quanto fa due più due? Rispondi solo: quattro.")
    parser.add_argument("--max-tokens", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=180.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = json.dumps(
        {
            "model": args.model,
            "messages": [{"role": "user", "content": args.prompt}],
            "max_tokens": args.max_tokens,
            "temperature": 0,
            "stream": False,
        }
    ).encode()
    request = urllib.request.Request(
        args.url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=args.timeout) as response:
            result = json.load(response)
    except (OSError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
        print(f"benchmark failed: {exc}")
        return 1
    elapsed = time.monotonic() - started
    choices = result.get("choices") or []
    text = choices[0].get("message", {}).get("content", "") if choices else ""
    usage = result.get("usage") or {}
    timings = result.get("timings") or {}
    summary = {
        "elapsed_seconds": elapsed,
        "text": text,
        "usage": usage,
        "timings": timings,
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
