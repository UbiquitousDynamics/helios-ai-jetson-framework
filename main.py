"""Executable entry point for the Helios voice assistant."""

from __future__ import annotations

import logging
from pathlib import Path

import config
from assistant import VoiceAssistant


def configure_logging(settings: config.Settings = config.SETTINGS) -> None:
    handlers: list[logging.Handler]
    if settings.log_file:
        Path(settings.log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers = [logging.FileHandler(settings.log_file, encoding="utf-8")]
    else:
        handlers = [logging.StreamHandler()]

    logging.basicConfig(
        level=settings.log_level,
        format=settings.log_format,
        handlers=handlers,
        force=True,
    )


def main() -> int:
    configure_logging()
    with VoiceAssistant() as assistant:
        assistant.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
