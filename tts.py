"""Compatibility exports for the Piper text-to-speech implementation.

New code should import from :mod:`audio.tts`. This module preserves the
historical top-level import path for external callers.
"""

from audio.tts import (
    AudioBackend,
    AudioPlaybackError,
    AudioSynthesisError,
    PiperTTS,
    SoundDeviceBackend,
    SpeechTiming,
    SynthesizedFragment,
    TTSError,
)

__all__ = [
    "AudioBackend",
    "AudioPlaybackError",
    "AudioSynthesisError",
    "PiperTTS",
    "SoundDeviceBackend",
    "SpeechTiming",
    "SynthesizedFragment",
    "TTSError",
]
