"""Compatibility exports for the consolidated TTS implementation.

Deprecated: import from :mod:`audio.tts` instead. Nothing in this repository
imports this module; it exists only so external callers written against the
pre-consolidation layout keep working.
"""

import warnings

from audio.tts import (
    AudioPlaybackError,
    AudioSynthesisError,
    PiperTTS,
    Pyttsx3TTS,
    SoundDeviceBackend,
    TTSError,
)

__all__ = [
    "AudioPlaybackError",
    "AudioSynthesisError",
    "PiperTTS",
    "Pyttsx3TTS",
    "SoundDeviceBackend",
    "TTSError",
]

warnings.warn(
    "audio.playback is deprecated; import from audio.tts instead",
    DeprecationWarning,
    stacklevel=2,
)
