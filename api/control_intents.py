"""Exact local control grammar; no model, text persistence or task execution."""

from __future__ import annotations

from enum import Enum
import re
import threading
from collections.abc import Callable
from typing import Any

from api.transcripts import authoritative_text


class ControlIntent(str, Enum):
    STOP_SPEAKING = "stop_speaking"
    MUTE = "mute"
    UNMUTE = "unmute"
    SUSPEND_SESSION = "suspend_session"
    RESUME_SESSION = "resume_session"
    END_SESSION = "end_session"
    CANCEL_TASK = "cancel_task"


_I = ControlIntent
_PHRASES = {
    "it": {
        _I.STOP_SPEAKING: ("stop", "basta", "silenzio", "fermati", "interrompi", "smetti di parlare", "cancella"),
        _I.MUTE: ("mute", "silenzia audio", "disattiva audio"),
        _I.UNMUTE: ("unmute", "riattiva audio"),
        _I.SUSPEND_SESSION: ("sospendi sessione", "sospendi conversazione", "pausa conversazione"),
        _I.RESUME_SESSION: ("riprendi sessione", "riprendi conversazione"),
        _I.END_SESSION: ("termina sessione", "chiudi sessione", "termina conversazione", "fine conversazione"),
        _I.CANCEL_TASK: ("annulla attività", "annulla compito", "cancella compito"),
    },
    "en": {
        _I.STOP_SPEAKING: ("stop", "stop speaking", "stop talking", "be quiet", "enough", "cancel", "pause"),
        _I.MUTE: ("mute", "mute audio"),
        _I.UNMUTE: ("unmute", "unmute audio"),
        _I.SUSPEND_SESSION: ("suspend session", "pause session", "pause conversation"),
        _I.RESUME_SESSION: ("resume session", "resume conversation"),
        _I.END_SESSION: ("end session", "end conversation", "close session"),
        _I.CANCEL_TASK: ("cancel task", "cancel the task"),
    },
}


def parse_control(text: str, *, language: str, wake_words: tuple[str, ...] = ()) -> ControlIntent | None:
    """Match a whole authoritative utterance, optionally prefixed by a wake word."""

    text = authoritative_text(text)
    if language not in _PHRASES:
        raise ValueError("unsupported control language")
    words = re.findall(r"\w+", text.casefold())
    for wake in wake_words:
        prefix = re.findall(r"\w+", wake.casefold())
        if prefix and words[:len(prefix)] == prefix:
            words = words[len(prefix):]
            break
    normalized = " ".join(words)
    for intent, phrases in _PHRASES[language].items():
        if normalized in phrases:
            return intent
    return None


class SpeechOutputControl:
    """Content-free output state, independent of model/task cancellation."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._muted = False
        self._stopped = False

    def begin_response(self) -> None:
        with self._lock:
            self._stopped = False

    def stop_speaking(self) -> None:
        with self._lock:
            self._stopped = True

    def set_muted(self, muted: bool) -> None:
        if not isinstance(muted, bool):
            raise TypeError("muted must be a boolean")
        with self._lock:
            self._muted = muted

    @property
    def muted(self) -> bool:
        with self._lock:
            return self._muted

    def is_set(self) -> bool:
        with self._lock:
            return self._muted or self._stopped


class ControlledSpeech:
    """Preserve drain/cancel while suppressing future speech dispatch locally."""

    def __init__(self, speak: Callable[[str], Any], control: SpeechOutputControl):
        self._speak, self._control = speak, control

    def __call__(self, text: str) -> Any:
        if not self._control.is_set():
            return self._speak(text)
        return None

    def flush(self) -> Any:
        flush = getattr(self._speak, "flush", None)
        return flush() if callable(flush) else ()

    def cancel(self) -> None:
        cancel = getattr(self._speak, "cancel", None)
        if callable(cancel):
            cancel()
