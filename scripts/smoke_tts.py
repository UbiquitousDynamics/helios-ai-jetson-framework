"""Speak one phrase through the configured Piper model.

This is a manual hardware smoke check; importing the module has no audio side
effects.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402
from audio.tts import PiperTTS  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "text",
        nargs="?",
        default=config.PROFILE.welcome_message.format(wake_word=config.PROFILE.wake_word),
    )
    parser.add_argument("--model", default=config.TTS_MODEL)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    with PiperTTS(args.model) as tts:
        tts.speak(args.text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
