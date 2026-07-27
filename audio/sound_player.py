"""Playback of short notification sounds through ``aplay``."""

from __future__ import annotations

import logging
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class SoundPlaybackError(RuntimeError):
    """Raised when a notification sound cannot be played."""


class SoundPlayer:
    def __init__(
        self,
        *,
        executable: str = "aplay",
        runner: Callable[..., Any] = subprocess.run,
        executable_resolver: Callable[[str], str | None] = shutil.which,
        timeout: float = 10.0,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        self.executable = executable
        self._runner = runner
        self._executable_resolver = executable_resolver
        self.timeout = timeout
        self._resolved_executable: str | None = None
        self._capability_checked = False

    def _player_executable(self) -> str:
        if not self._capability_checked:
            self._resolved_executable = self._executable_resolver(self.executable)
            self._capability_checked = True
        if not self._resolved_executable:
            raise SoundPlaybackError(
                f"Sound player executable {self.executable!r} is not available"
            )
        return self._resolved_executable

    @property
    def available(self) -> bool:
        try:
            self._player_executable()
        except SoundPlaybackError:
            return False
        return True

    def play_sound(self, sound_file: str | Path) -> None:
        path = Path(sound_file)
        if not path.is_file():
            raise SoundPlaybackError(f"Sound file not found: {path}")

        try:
            self._runner(
                [self._player_executable(), str(path)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=self.timeout,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SoundPlaybackError(f"Unable to play sound: {path}") from exc
        logger.debug("Played notification sound %s", path)
